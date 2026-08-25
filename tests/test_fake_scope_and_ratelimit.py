"""The two platform disciplines: token scope and published rate limits."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import LOCATION_ID, OTHER_LOCATION_ID, T0, build_location

from ghl_bridge import (
    ContactUpsert,
    CrossLocationDenied,
    RateLimited,
    Unauthorized,
)
from ghl_bridge.fakes import FakeClock, FakeHighLevel

# --------------------------------------------------------------------- scope


def test_a_token_reaches_its_own_location(port) -> None:
    contact = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="a@riverbend.example")
    )
    assert contact.location_id == LOCATION_ID


def test_cross_location_upsert_is_denied(port) -> None:
    """A Private Integration token is scoped to one sub-account. Reaching
    for a sibling location is a typed denial naming both sides, not an
    empty result."""
    with pytest.raises(CrossLocationDenied) as excinfo:
        port.upsert_contact(
            OTHER_LOCATION_ID, ContactUpsert(email="a@riverbend.example")
        )
    assert excinfo.value.token_location == LOCATION_ID
    assert excinfo.value.requested_location == OTHER_LOCATION_ID


@pytest.mark.parametrize(
    "operation",
    [
        lambda port: port.get_contact(OTHER_LOCATION_ID, "con-0001"),
        lambda port: port.search_contacts_by_email(OTHER_LOCATION_ID, "a@b.example"),
        lambda port: port.search_contacts_by_phone(OTHER_LOCATION_ID, "+15005550100"),
        lambda port: port.list_pipelines(OTHER_LOCATION_ID),
        lambda port: port.get_conversation(OTHER_LOCATION_ID, "cnv-0001"),
    ],
    ids=["get_contact", "search_email", "search_phone", "list_pipelines", "get_conversation"],
)
def test_every_read_is_scope_checked_before_anything_else(port, operation) -> None:
    with pytest.raises(CrossLocationDenied):
        operation(port)


def test_a_cross_location_denial_burns_no_quota() -> None:
    """Scope is checked before quota: a denied probe leaves the window
    untouched for legitimate traffic."""
    tight = FakeHighLevel(clock=FakeClock(T0), burst_limit=1)
    tight.add_location(build_location())
    tight_port = tight.port_for(tight.issue_private_token(LOCATION_ID))
    with pytest.raises(CrossLocationDenied):
        tight_port.list_pipelines(OTHER_LOCATION_ID)
    assert tight_port.list_pipelines(LOCATION_ID) == ()


def test_an_unknown_token_is_refused_at_port_construction(server) -> None:
    with pytest.raises(Unauthorized, match="unknown or revoked"):
        server.port_for("pit-forged")


def test_two_tokens_for_two_locations_stay_in_their_lanes(server) -> None:
    port_a = server.port_for(server.issue_private_token(LOCATION_ID))
    port_b = server.port_for(server.issue_private_token(OTHER_LOCATION_ID))
    assert port_a.list_pipelines(LOCATION_ID) != ()
    assert port_b.list_pipelines(OTHER_LOCATION_ID) == ()
    with pytest.raises(CrossLocationDenied):
        port_b.list_pipelines(LOCATION_ID)


# --------------------------------------------------------------- rate limits


def tight_server(clock: FakeClock, *, burst: int = 3, daily: int = 200_000) -> tuple:
    server = FakeHighLevel(clock=clock, burst_limit=burst, daily_limit=daily)
    server.add_location(build_location())
    port = server.port_for(server.issue_private_token(LOCATION_ID))
    return server, port


def test_calls_inside_the_burst_succeed(clock) -> None:
    _, port = tight_server(clock, burst=3)
    for _ in range(3):
        port.list_pipelines(LOCATION_ID)


def test_the_call_over_the_burst_gets_a_429(clock) -> None:
    _, port = tight_server(clock, burst=3)
    for _ in range(3):
        port.list_pipelines(LOCATION_ID)
    with pytest.raises(RateLimited):
        port.list_pipelines(LOCATION_ID)


def test_the_429_carries_the_documented_header_names(clock) -> None:
    _, port = tight_server(clock, burst=1)
    port.list_pipelines(LOCATION_ID)
    with pytest.raises(RateLimited) as excinfo:
        port.list_pipelines(LOCATION_ID)
    headers = excinfo.value.headers
    assert headers["X-RateLimit-Max"] == "1"
    assert headers["X-RateLimit-Remaining"] == "0"
    assert headers["X-RateLimit-Interval-Milliseconds"] == "10000"
    assert "X-RateLimit-Limit-Daily" in headers
    assert "X-RateLimit-Daily-Remaining" in headers


def test_the_retry_hint_is_the_exact_window_remainder(clock) -> None:
    _, port = tight_server(clock, burst=1)
    port.list_pipelines(LOCATION_ID)  # opens the window at T0
    clock.advance(4)
    with pytest.raises(RateLimited) as excinfo:
        port.list_pipelines(LOCATION_ID)
    assert excinfo.value.retry_after_seconds == pytest.approx(6.0)


def test_the_window_resets_after_ten_seconds(clock) -> None:
    _, port = tight_server(clock, burst=1)
    port.list_pipelines(LOCATION_ID)
    clock.advance(10)
    port.list_pipelines(LOCATION_ID)  # a fresh window, no error


def test_one_second_short_of_the_reset_still_429s(clock) -> None:
    _, port = tight_server(clock, burst=1)
    port.list_pipelines(LOCATION_ID)
    clock.advance(9)
    with pytest.raises(RateLimited):
        port.list_pipelines(LOCATION_ID)


def test_a_429_attempt_does_not_extend_the_window(clock) -> None:
    """Being told to wait must not move the goalposts: the denied attempt
    does not restart the ten seconds."""
    _, port = tight_server(clock, burst=1)
    port.list_pipelines(LOCATION_ID)
    clock.advance(9)
    with pytest.raises(RateLimited):
        port.list_pipelines(LOCATION_ID)
    clock.advance(1)
    port.list_pipelines(LOCATION_ID)


def test_rate_limits_are_per_location_not_global(clock) -> None:
    tight = FakeHighLevel(clock=clock, burst_limit=1)
    for loc_id, name in ((LOCATION_ID, "Riverbend"), (OTHER_LOCATION_ID, "Lakeshore")):
        tight.add_location(build_location(loc_id, name))
    port_a = tight.port_for(tight.issue_private_token(LOCATION_ID))
    port_b = tight.port_for(tight.issue_private_token(OTHER_LOCATION_ID))
    port_a.list_pipelines(LOCATION_ID)
    port_b.list_pipelines(OTHER_LOCATION_ID)  # b's window is its own
    with pytest.raises(RateLimited):
        port_a.list_pipelines(LOCATION_ID)


def test_the_daily_ceiling_429s_even_with_fresh_burst_windows(clock) -> None:
    _, port = tight_server(clock, burst=10, daily=5)
    for _ in range(5):
        port.list_pipelines(LOCATION_ID)
        clock.advance(11)  # every call in its own burst window
    with pytest.raises(RateLimited) as excinfo:
        port.list_pipelines(LOCATION_ID)
    assert excinfo.value.headers["X-RateLimit-Daily-Remaining"] == "0"


def test_the_daily_retry_hint_points_at_utc_midnight(clock) -> None:
    _, port = tight_server(clock, burst=10, daily=1)
    port.list_pipelines(LOCATION_ID)
    with pytest.raises(RateLimited) as excinfo:
        port.list_pipelines(LOCATION_ID)
    now = clock()
    tomorrow = datetime(now.year, now.month, now.day, tzinfo=UTC).replace(day=now.day + 1)
    assert excinfo.value.retry_after_seconds == pytest.approx(
        (tomorrow - now).total_seconds()
    )


def test_the_daily_count_resets_at_utc_midnight(clock) -> None:
    _, port = tight_server(clock, burst=10, daily=1)
    port.list_pipelines(LOCATION_ID)
    now = clock()
    seconds_to_midnight = (
        datetime(now.year, now.month, now.day, tzinfo=UTC).replace(day=now.day + 1) - now
    ).total_seconds()
    clock.advance(seconds_to_midnight)
    port.list_pipelines(LOCATION_ID)  # a new day, a new budget


def test_nonpositive_limits_are_refused_at_construction(clock) -> None:
    with pytest.raises(ValueError, match="positive"):
        FakeHighLevel(clock=clock, burst_limit=0)
    with pytest.raises(ValueError, match="positive"):
        FakeHighLevel(clock=clock, daily_limit=0)
    with pytest.raises(ValueError, match="positive"):
        FakeHighLevel(clock=clock, burst_window_seconds=0)
