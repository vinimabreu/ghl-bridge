"""The client-side discipline: computed waits, honest backoff, no sleeps."""

from __future__ import annotations

import pytest
from tests.conftest import LOCATION_ID, build_location

from ghl_bridge import (
    AuditLedger,
    ContactUpsert,
    LocationPacer,
    PacedCaller,
    PacedPort,
    RateLimited,
    RetryBudgetExhausted,
)
from ghl_bridge.fakes import FakeClock, FakeHighLevel


def make_pacer(clock: FakeClock, *, burst: int = 3, daily: int = 200_000) -> LocationPacer:
    return LocationPacer(clock=clock, burst_limit=burst, daily_limit=daily)


# --------------------------------------------------------------------- pacer


def test_the_first_calls_wait_zero(clock) -> None:
    pacer = make_pacer(clock)
    for _ in range(3):
        assert pacer.wait_before_call(LOCATION_ID) == 0.0
        pacer.record_call(LOCATION_ID)


def test_the_call_over_the_burst_computes_the_exact_wait(clock) -> None:
    pacer = make_pacer(clock, burst=3)
    for _ in range(3):
        pacer.record_call(LOCATION_ID)
    assert pacer.wait_before_call(LOCATION_ID) == pytest.approx(10.0)


def test_the_wait_shrinks_as_time_passes(clock) -> None:
    pacer = make_pacer(clock, burst=1)
    pacer.record_call(LOCATION_ID)
    clock.advance(4)
    assert pacer.wait_before_call(LOCATION_ID) == pytest.approx(6.0)


def test_after_the_window_the_wait_is_zero_again(clock) -> None:
    pacer = make_pacer(clock, burst=1)
    pacer.record_call(LOCATION_ID)
    clock.advance(10)
    assert pacer.wait_before_call(LOCATION_ID) == 0.0


def test_the_window_slides_over_the_oldest_call(clock) -> None:
    """Calls at t=0 and t=6 with burst 2: at t=8 the wait is 2 seconds,
    until the t=0 call leaves the window."""
    pacer = make_pacer(clock, burst=2)
    pacer.record_call(LOCATION_ID)
    clock.advance(6)
    pacer.record_call(LOCATION_ID)
    clock.advance(2)
    assert pacer.wait_before_call(LOCATION_ID) == pytest.approx(2.0)


def test_locations_pace_independently(clock) -> None:
    pacer = make_pacer(clock, burst=1)
    pacer.record_call("loc-a")
    assert pacer.wait_before_call("loc-a") > 0
    assert pacer.wait_before_call("loc-b") == 0.0


def test_the_daily_ceiling_waits_until_utc_midnight(clock) -> None:
    pacer = make_pacer(clock, burst=100, daily=2)
    pacer.record_call(LOCATION_ID)
    clock.advance(11)
    pacer.record_call(LOCATION_ID)
    clock.advance(11)
    wait = pacer.wait_before_call(LOCATION_ID)
    assert wait > 3600  # hours, not seconds: the day is spent
    now = clock()
    assert wait == pytest.approx((24 * 3600) - (now.hour * 3600 + now.minute * 60 + now.second))


def test_nonpositive_limits_are_refused(clock) -> None:
    with pytest.raises(ValueError, match="positive"):
        LocationPacer(clock=clock, burst_limit=0)


# -------------------------------------------------------------- paced caller


class RecordingSleeper:
    """The injected waiter for tests: advances the fake clock by exactly
    the requested amount and remembers every request."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(seconds)


def make_stack(clock: FakeClock, *, burst: int = 2):
    server = FakeHighLevel(clock=clock, burst_limit=burst)
    server.add_location(build_location())
    port = server.port_for(server.issue_private_token(LOCATION_ID))
    ledger = AuditLedger()
    sleeper = RecordingSleeper(clock)
    caller = PacedCaller(
        pacer=LocationPacer(clock=clock, burst_limit=burst),
        sleeper=sleeper,
        ledger=ledger,
        clock=clock,
    )
    return server, port, ledger, sleeper, caller


def test_a_burst_of_calls_waits_instead_of_erroring(clock) -> None:
    """The spec's burst case: with a burst of 2, six paced calls succeed
    with computed waits and the caller never sees a 429."""
    _, port, _, sleeper, caller = make_stack(clock, burst=2)
    for _ in range(6):
        result = caller.call(
            LOCATION_ID, lambda: port.list_pipelines(LOCATION_ID), label="list"
        )
        assert isinstance(result, tuple)
    assert len(sleeper.waits) == 2  # calls 3-4 shared one window wait, 5-6 another
    assert all(w == pytest.approx(10.0, abs=0.001) for w in sleeper.waits)


def test_waits_land_in_the_ledger_with_the_label(clock) -> None:
    _, port, ledger, _, caller = make_stack(clock, burst=1)
    caller.call(LOCATION_ID, lambda: port.list_pipelines(LOCATION_ID), label="list")
    caller.call(LOCATION_ID, lambda: port.list_pipelines(LOCATION_ID), label="list")
    waits = ledger.of_kind("rate_wait")
    assert len(waits) == 1
    assert waits[0].detail["label"] == "list"
    assert waits[0].detail["seconds"] == pytest.approx(10.0)


def test_a_surprise_429_backs_off_by_the_platforms_number(clock) -> None:
    """The pacer thinks the budget is free (another client spent it); the
    server answers 429; the caller waits the server's own retry hint and
    succeeds on the retry."""
    server, port, ledger, sleeper, caller = make_stack(clock, burst=2)
    # burn the server-side budget behind the pacer's back
    direct = server.port_for(server.issue_private_token(LOCATION_ID))
    direct.list_pipelines(LOCATION_ID)
    direct.list_pipelines(LOCATION_ID)
    result = caller.call(
        LOCATION_ID, lambda: port.list_pipelines(LOCATION_ID), label="list"
    )
    assert isinstance(result, tuple)
    retries = ledger.of_kind("api_retry")
    assert len(retries) == 1
    assert retries[0].detail["retry_after_seconds"] == pytest.approx(10.0)
    assert sleeper.waits[-1] == pytest.approx(10.0)


def test_the_retry_budget_is_finite_and_the_last_answer_is_kept(clock) -> None:
    ledger = AuditLedger()
    sleeper = RecordingSleeper(clock)
    caller = PacedCaller(
        pacer=LocationPacer(clock=clock, burst_limit=100),
        sleeper=sleeper,
        ledger=ledger,
        clock=clock,
        max_retries=2,
    )

    def always_limited() -> None:
        raise RateLimited(retry_after_seconds=1.0, headers={})

    with pytest.raises(RetryBudgetExhausted) as excinfo:
        caller.call(LOCATION_ID, always_limited, label="doomed")
    assert excinfo.value.attempts == 3
    assert len(ledger.of_kind("api_retry")) == 2


def test_zero_retries_is_a_valid_budget(clock) -> None:
    caller = PacedCaller(
        pacer=LocationPacer(clock=clock),
        sleeper=RecordingSleeper(clock),
        ledger=AuditLedger(),
        clock=clock,
        max_retries=0,
    )

    def limited() -> None:
        raise RateLimited(retry_after_seconds=1.0, headers={})

    with pytest.raises(RetryBudgetExhausted):
        caller.call(LOCATION_ID, limited, label="x")


def test_a_negative_retry_budget_is_refused(clock) -> None:
    with pytest.raises(ValueError, match="negative"):
        PacedCaller(
            pacer=LocationPacer(clock=clock),
            sleeper=RecordingSleeper(clock),
            ledger=AuditLedger(),
            clock=clock,
            max_retries=-1,
        )


def test_non_rate_errors_pass_through_untouched(clock) -> None:
    caller = PacedCaller(
        pacer=LocationPacer(clock=clock),
        sleeper=RecordingSleeper(clock),
        ledger=AuditLedger(),
        clock=clock,
    )

    def broken() -> None:
        raise KeyError("not a rate problem")

    with pytest.raises(KeyError):
        caller.call(LOCATION_ID, broken, label="x")


# ---------------------------------------------------------------- paced port


def test_the_paced_port_is_a_drop_in_port(clock, dana, server, token, ledger) -> None:
    sleeper = RecordingSleeper(clock)
    paced = PacedPort(
        inner=server.port_for(token),
        caller=PacedCaller(
            pacer=LocationPacer(clock=clock),
            sleeper=sleeper,
            ledger=ledger,
            clock=clock,
        ),
    )
    contact = paced.upsert_contact(
        LOCATION_ID, ContactUpsert(email="p@riverbend.example")
    )
    assert contact.contact_id.startswith("con-")
    assert paced.get_contact(LOCATION_ID, contact.contact_id).email == "p@riverbend.example"
    assert paced.search_contacts_by_email(LOCATION_ID, "p@riverbend.example") != ()


def test_the_paced_port_never_surfaces_the_429(clock) -> None:
    """Six sends through a paced port against a burst of 2: all succeed,
    and the ledger shows the waits that made it possible."""
    server = FakeHighLevel(clock=clock, burst_limit=2)
    server.add_location(build_location())
    token = server.issue_private_token(LOCATION_ID)
    ledger = AuditLedger()
    sleeper = RecordingSleeper(clock)
    paced = PacedPort(
        inner=server.port_for(token),
        caller=PacedCaller(
            pacer=LocationPacer(clock=clock, burst_limit=2),
            sleeper=sleeper,
            ledger=ledger,
            clock=clock,
        ),
    )
    for _ in range(6):
        paced.list_pipelines(LOCATION_ID)
    assert len(ledger.of_kind("rate_wait")) == 2
    assert ledger.of_kind("api_retry") == ()
