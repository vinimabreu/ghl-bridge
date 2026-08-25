"""Client-side rate discipline: pace first, back off second, never guess.

The platform publishes its limits (a burst window and a daily ceiling per
location), so the client's first duty is to not hit them: the pacer keeps
a sliding window of recent calls per location and computes exactly how
long to wait before the next call is safe. A sliding window that admits at
most N calls in any window-length interval can never exceed a server that
admits N per fixed window, however the server's window happens to be
aligned, so pacing is conservative by construction rather than by tuning.

The second duty is honest behaviour when a 429 arrives anyway (another
process sharing the token, a limit changed upstream): wait exactly as long
as the platform said in the response, retry a bounded number of times, and
record every wait and every retry in the ledger. The caller above sees a
slower call, never an exception, until the retry budget is spent.

Time never passes silently. Waiting goes through an injected
:data:`Sleeper`, so production sleeps and tests advance a
:class:`ghl_bridge.fakes.FakeClock` by the same number the pacer computed,
which is what keeps the whole suite free of real sleeps.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import TypeVar

from .clock import Clock
from .ledger import AuditLedger
from .limits import BURST_LIMIT, BURST_WINDOW_SECONDS, DAILY_LIMIT
from .models import (
    Appointment,
    BookingRequest,
    CalendarSlot,
    Contact,
    ContactUpsert,
    Conversation,
    Message,
    Opportunity,
    OpportunityCreate,
    OutboundSend,
    Pipeline,
    WebhookRegistration,
    WebhookSubscription,
)
from .ports import HighLevelPort, RateLimited

T = TypeVar("T")

Sleeper = Callable[[float], None]
"""How the caller waits. Injected: ``time.sleep`` in production, a FakeClock
advance in tests and the demo."""

system_sleeper: Sleeper = time.sleep


class LocationPacer:
    """Computes the wait that keeps a location under the published limits.

    Stateless about outcomes: it only remembers when calls were made. The
    numbers default to the published platform limits and are constructor
    arguments so a test can watch the arithmetic with a burst of 3 instead
    of 100.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        burst_limit: int = BURST_LIMIT,
        burst_window_seconds: float = BURST_WINDOW_SECONDS,
        daily_limit: int = DAILY_LIMIT,
    ) -> None:
        if burst_limit < 1 or daily_limit < 1 or burst_window_seconds <= 0:
            raise ValueError("rate limits must be positive")
        self._clock = clock
        self._burst_limit = burst_limit
        self._window = burst_window_seconds
        self._daily_limit = daily_limit
        self._calls: dict[str, deque[datetime]] = {}
        self._day: dict[str, date] = {}
        self._day_count: dict[str, int] = {}

    def wait_before_call(self, location_id: str) -> float:
        """Seconds to wait so the next call cannot trip the limit. Zero
        when the call is already safe."""
        now = self._clock()
        self._roll_day(location_id, now)

        if self._day_count.get(location_id, 0) >= self._daily_limit:
            midnight = datetime.combine(
                now.astimezone(UTC).date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=UTC,
            )
            return (midnight - now).total_seconds()

        window = self._calls.setdefault(location_id, deque())
        while window and (now - window[0]).total_seconds() >= self._window:
            window.popleft()
        if len(window) < self._burst_limit:
            return 0.0
        oldest = window[0]
        return self._window - (now - oldest).total_seconds()

    def record_call(self, location_id: str) -> None:
        now = self._clock()
        self._roll_day(location_id, now)
        self._calls.setdefault(location_id, deque()).append(now)
        self._day_count[location_id] = self._day_count.get(location_id, 0) + 1

    def _roll_day(self, location_id: str, now: datetime) -> None:
        today = now.astimezone(UTC).date()
        if self._day.get(location_id) != today:
            self._day[location_id] = today
            self._day_count[location_id] = 0


class RetryBudgetExhausted(Exception):
    """The platform kept answering 429 past the retry budget. Carries the
    last answer so the operator sees the platform's own numbers."""

    def __init__(self, *, attempts: int, last: RateLimited) -> None:
        self.attempts = attempts
        self.last = last
        super().__init__(
            f"still rate limited after {attempts} paced attempts; "
            f"last retry hint was {last.retry_after_seconds:.3f}s"
        )


class PacedCaller:
    """Runs one platform call under the pacer, with 429 backoff.

    The wait is always the computed or platform-stated number, never an
    invented exponential; both kinds of waiting and every retry land in
    the ledger, so "why was the sync slow at 14:03" has the same quality
    of answer as "why did that message send".
    """

    def __init__(
        self,
        *,
        pacer: LocationPacer,
        sleeper: Sleeper,
        ledger: AuditLedger,
        clock: Clock,
        max_retries: int = 3,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._pacer = pacer
        self._sleeper = sleeper
        self._ledger = ledger
        self._clock = clock
        self._max_retries = max_retries

    def call(self, location_id: str, fn: Callable[[], T], *, label: str) -> T:
        retries = 0
        while True:
            wait = self._pacer.wait_before_call(location_id)
            if wait > 0:
                self._ledger.record(
                    at=self._clock(),
                    kind="rate_wait",
                    detail={
                        "location_id": location_id,
                        "seconds": round(wait, 3),
                        "label": label,
                    },
                )
                self._sleeper(wait)
            self._pacer.record_call(location_id)
            try:
                return fn()
            except RateLimited as exc:
                retries += 1
                if retries > self._max_retries:
                    raise RetryBudgetExhausted(attempts=retries, last=exc) from exc
                self._ledger.record(
                    at=self._clock(),
                    kind="api_retry",
                    detail={
                        "location_id": location_id,
                        "retry_after_seconds": round(exc.retry_after_seconds, 3),
                        "attempt": retries,
                        "label": label,
                    },
                )
                self._sleeper(exc.retry_after_seconds)


class PacedPort:
    """A :class:`~ghl_bridge.ports.HighLevelPort` that arrives disciplined.

    Wraps any port implementation so every operation flows through the
    pacer and the backoff. The bridge talks to this and only this; nothing
    above it needs to know rate limits exist.
    """

    def __init__(self, *, inner: HighLevelPort, caller: PacedCaller) -> None:
        self._inner = inner
        self._caller = caller

    def upsert_contact(self, location_id: str, contact: ContactUpsert) -> Contact:
        return self._caller.call(
            location_id,
            lambda: self._inner.upsert_contact(location_id, contact),
            label="upsert_contact",
        )

    def get_contact(self, location_id: str, contact_id: str) -> Contact:
        return self._caller.call(
            location_id,
            lambda: self._inner.get_contact(location_id, contact_id),
            label="get_contact",
        )

    def search_contacts_by_email(self, location_id: str, email: str) -> tuple[Contact, ...]:
        return self._caller.call(
            location_id,
            lambda: self._inner.search_contacts_by_email(location_id, email),
            label="search_contacts_by_email",
        )

    def search_contacts_by_phone(self, location_id: str, phone: str) -> tuple[Contact, ...]:
        return self._caller.call(
            location_id,
            lambda: self._inner.search_contacts_by_phone(location_id, phone),
            label="search_contacts_by_phone",
        )

    def list_pipelines(self, location_id: str) -> tuple[Pipeline, ...]:
        return self._caller.call(
            location_id,
            lambda: self._inner.list_pipelines(location_id),
            label="list_pipelines",
        )

    def create_opportunity(
        self, location_id: str, create: OpportunityCreate
    ) -> Opportunity:
        return self._caller.call(
            location_id,
            lambda: self._inner.create_opportunity(location_id, create),
            label="create_opportunity",
        )

    def move_opportunity(
        self, location_id: str, opportunity_id: str, stage_id: str
    ) -> Opportunity:
        return self._caller.call(
            location_id,
            lambda: self._inner.move_opportunity(location_id, opportunity_id, stage_id),
            label="move_opportunity",
        )

    def get_conversation(self, location_id: str, conversation_id: str) -> Conversation:
        return self._caller.call(
            location_id,
            lambda: self._inner.get_conversation(location_id, conversation_id),
            label="get_conversation",
        )

    def send_message(self, location_id: str, send: OutboundSend) -> Message:
        return self._caller.call(
            location_id,
            lambda: self._inner.send_message(location_id, send),
            label="send_message",
        )

    def free_slots(
        self, location_id: str, calendar_id: str, day: date
    ) -> tuple[CalendarSlot, ...]:
        return self._caller.call(
            location_id,
            lambda: self._inner.free_slots(location_id, calendar_id, day),
            label="free_slots",
        )

    def book_appointment(self, location_id: str, booking: BookingRequest) -> Appointment:
        return self._caller.call(
            location_id,
            lambda: self._inner.book_appointment(location_id, booking),
            label="book_appointment",
        )

    def register_webhook(
        self, location_id: str, subscription: WebhookSubscription
    ) -> WebhookRegistration:
        return self._caller.call(
            location_id,
            lambda: self._inner.register_webhook(location_id, subscription),
            label="register_webhook",
        )
