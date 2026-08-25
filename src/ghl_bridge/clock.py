"""The injected time source.

Business hours, rate-limit windows, daily quotas, appointment slots and
audit timestamps are all decisions about "now", and in this package every
one of them changes what is allowed to leave for a customer. A module that
reads the wall clock directly cannot be tested at the boundaries where
those decisions flip, so no domain module here calls ``datetime.now``.
Everything takes a :data:`Clock`, and :func:`system_clock` is the single
function in the package that reads real time.

Tests and the offline demo inject :class:`ghl_bridge.fakes.FakeClock` and
move time by explicit amounts, which is what makes "one second past the
close of business hours" an ordinary assertion instead of a flaky sleep.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]
"""Injected time source. Must return a timezone-aware datetime."""


def system_clock() -> datetime:
    """Timezone-aware wall clock.

    The single place in the package that reads the current time. Every
    other module takes a :data:`Clock`, so a business-hours decision or a
    rate-limit reset can be reproduced exactly in a test, and there is one
    line to change if a deployment needs a different time source.
    """
    return datetime.now(tz=UTC)


def require_aware(value: datetime, *, field: str) -> datetime:
    """Return ``value`` if it carries a timezone, raise if it does not.

    A naive datetime compared against an aware one raises ``TypeError``
    deep inside a business-hours check, at message time, in production.
    Refusing it at construction moves the failure to the line that made
    the mistake.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{field} must be timezone-aware; a naive datetime makes every "
            "business-hours and rate-limit comparison ambiguous, and this "
            "package decides what leaves for a customer on those comparisons"
        )
    return value
