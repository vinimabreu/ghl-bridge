"""The injected time source and the fake that scripts it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ghl_bridge import require_aware, system_clock
from ghl_bridge.fakes import FakeClock

T0 = datetime(2026, 3, 3, 9, 0, tzinfo=UTC)


def test_system_clock_returns_an_aware_datetime() -> None:
    now = system_clock()
    assert now.tzinfo is not None


def test_system_clock_is_utc() -> None:
    assert system_clock().utcoffset() == timedelta(0)


def test_fake_clock_starts_where_told() -> None:
    assert FakeClock(T0)() == T0


def test_fake_clock_is_callable_and_now_agree() -> None:
    clock = FakeClock(T0)
    assert clock() == clock.now()


def test_fake_clock_advances_by_the_exact_amount() -> None:
    clock = FakeClock(T0)
    clock.advance(90.0)
    assert clock() == T0 + timedelta(seconds=90)


def test_fake_clock_advance_returns_the_new_now() -> None:
    clock = FakeClock(T0)
    assert clock.advance(5) == T0 + timedelta(seconds=5)


def test_fake_clock_refuses_to_run_backwards() -> None:
    clock = FakeClock(T0)
    with pytest.raises(ValueError, match="only moves forward"):
        clock.advance(-1)


def test_fake_clock_zero_advance_is_allowed() -> None:
    clock = FakeClock(T0)
    clock.advance(0)
    assert clock() == T0


def test_fake_clock_refuses_a_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 3, 3, 9, 0))


def test_require_aware_passes_an_aware_value_through() -> None:
    assert require_aware(T0, field="x") == T0


def test_require_aware_accepts_non_utc_offsets() -> None:
    offset = datetime(2026, 3, 3, 6, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert require_aware(offset, field="x") == offset


def test_require_aware_refuses_naive_and_names_the_field() -> None:
    with pytest.raises(ValueError, match="occurred_at"):
        require_aware(datetime(2026, 3, 3), field="occurred_at")
