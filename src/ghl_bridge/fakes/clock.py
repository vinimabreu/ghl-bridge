"""A clock that moves only when told to.

Part of the product, not a test fixture: the offline demo scripts time
with it ("the second message arrives after closing time") and any
integrator can use it to rehearse business-hours boundaries, rate-limit
resets and replay windows before pointing the same code at a live
workspace.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..clock import require_aware


class FakeClock:
    """Deterministic time. Starts where you say, advances when you say."""

    def __init__(self, start: datetime) -> None:
        self._now = require_aware(start, field="start")

    def __call__(self) -> datetime:
        return self._now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        """Move time forward. Negative movement is refused: a clock that
        can run backwards makes "outside business hours" and "window
        reset" reversible states, and nothing in this package treats them
        as one."""
        if seconds < 0:
            raise ValueError("a FakeClock only moves forward")
        self._now = self._now + timedelta(seconds=seconds)
        return self._now
