"""Shared fixtures: a scripted clock, one seeded location, and the wired bridge.

Every fixture is function-scoped and freshly built, so a test that trips a
rate limit or moves the clock is describing its own world and nobody
else's.

The scenario, used by most of the suite and by the demo: Riverbend
Detailing, a single sub-account in America/Chicago, answering Monday to
Friday 09:00 to 18:00, one sales pipeline of three stages, one booking
calendar, and one existing contact (Dana Whitfield) who is about to come
in again through a form.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from ghl_bridge import (
    AuditLedger,
    Bridge,
    BusinessHours,
    CalendarSlot,
    Contact,
    ContactDeduper,
    ContactUpsert,
    GenerationRequest,
    HmacSha256Scheme,
    Location,
    Pipeline,
    PipelineStage,
    PolicyGate,
    WebhookEvent,
    WebhookIntake,
    rule_intent,
)
from ghl_bridge.fakes import FakeClock, FakeHighLevel
from ghl_bridge.guard import ApprovedSender

T0 = datetime(2026, 3, 3, 20, 0, tzinfo=UTC)
"""A Tuesday, 14:00 in America/Chicago: mid-afternoon, inside business hours."""

LOCATION_ID = "loc-riverbend"
OTHER_LOCATION_ID = "loc-lakeshore"
PIPELINE_ID = "pipe-sales"
STAGE_NEW = "stg-new"
STAGE_QUALIFIED = "stg-qualified"
STAGE_BOOKED = "stg-booked"
CALENDAR_ID = "cal-detail"
SECRET = b"riverbend-webhook-secret"

DANA_EMAIL = "dana@riverbend.example"
DANA_PHONE = "+15005550100"


def build_location(location_id: str = LOCATION_ID, name: str = "Riverbend Detailing") -> Location:
    """The standard location, buildable outside fixtures for tests that
    construct their own tightly limited servers."""
    return Location(
        location_id=location_id,
        name=name,
        timezone="America/Chicago",
        default_region="US",
        business_hours=BusinessHours(
            days=frozenset({0, 1, 2, 3, 4}),
            open_time=datetime(2026, 1, 1, 9, 0).time(),
            close_time=datetime(2026, 1, 1, 18, 0).time(),
        ),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def location() -> Location:
    return build_location()


@pytest.fixture
def other_location() -> Location:
    return build_location(OTHER_LOCATION_ID, "Lakeshore Detailing")


@pytest.fixture
def server(
    clock: FakeClock, location: Location, other_location: Location
) -> FakeHighLevel:
    server = FakeHighLevel(clock=clock)
    server.add_location(location)
    server.add_location(other_location)
    server.add_pipeline(
        LOCATION_ID,
        Pipeline(
            pipeline_id=PIPELINE_ID,
            name="Sales",
            stages=(
                PipelineStage(stage_id=STAGE_NEW, name="New Lead", position=1),
                PipelineStage(stage_id=STAGE_QUALIFIED, name="Qualified", position=2),
                PipelineStage(stage_id=STAGE_BOOKED, name="Booked", position=3),
            ),
        ),
    )
    thursday = datetime(2026, 3, 5, 15, 0, tzinfo=UTC)  # 09:00 in Chicago
    server.add_calendar_slots(
        LOCATION_ID,
        CALENDAR_ID,
        [
            CalendarSlot(
                calendar_id=CALENDAR_ID,
                start=thursday.replace(hour=15 + i),
                end=thursday.replace(hour=16 + i),
            )
            for i in range(3)
        ],
    )
    return server


@pytest.fixture
def dana(server: FakeHighLevel) -> Contact:
    return server.seed_contact(
        LOCATION_ID,
        ContactUpsert(
            first_name="Dana",
            last_name="Whitfield",
            email=DANA_EMAIL,
            phone=DANA_PHONE,
            source="walk-in",
        ),
    )


@pytest.fixture
def token(server: FakeHighLevel) -> str:
    return server.issue_private_token(LOCATION_ID)


@pytest.fixture
def port(server: FakeHighLevel, token: str) -> object:
    return server.port_for(token)


@pytest.fixture
def ledger() -> AuditLedger:
    return AuditLedger()


@pytest.fixture
def gate(clock: FakeClock) -> PolicyGate:
    return PolicyGate(clock=clock)


def template_generator(request: GenerationRequest) -> str:
    """The deterministic stand-in generator the suite and the demo inject.
    Keyed on the same intent rules the gate reads, which keeps the demo
    scenario legible; any callable from request to text plugs in here."""
    first = request.contact.first_name or "there"
    intent = rule_intent(request.inbound_text)
    if intent == "scheduling":
        return (
            f"Hi {first}, the next open detailing slots are Thursday morning. "
            "Reply with a time that suits you and I will book it."
        )
    if intent == "hours":
        return f"Hi {first}, the shop is open Monday to Friday, 9am to 6pm."
    if intent == "pricing":
        return (
            f"Hi {first}, a full interior detail is $180 and takes about "
            "three hours."
        )
    return f"Hi {first}, thanks for reaching out. How can I help?"


@pytest.fixture
def wired(
    clock: FakeClock,
    location: Location,
    server: FakeHighLevel,
    token: str,
    ledger: AuditLedger,
    gate: PolicyGate,
) -> Bridge:
    port = server.port_for(token)
    sender = ApprovedSender(port=port, ledger=ledger, clock=clock)
    deduper = ContactDeduper(port=port, ledger=ledger, clock=clock)
    return Bridge(
        location=location,
        port=port,
        deduper=deduper,
        gate=gate,
        sender=sender,
        generator=template_generator,
        ledger=ledger,
        clock=clock,
        lead_pipeline_id=PIPELINE_ID,
        lead_stage_id=STAGE_NEW,
    )


@pytest.fixture
def intake(
    wired: Bridge, ledger: AuditLedger, clock: FakeClock
) -> WebhookIntake:
    return WebhookIntake(
        scheme=HmacSha256Scheme(secret=SECRET),
        handler=wired.handle_event,
        ledger=ledger,
        clock=clock,
    )


@pytest.fixture
def signer() -> Callable[[dict[str, object]], tuple[bytes, str]]:
    """Serialise an event payload and sign it the way the sender would."""
    scheme = HmacSha256Scheme(secret=SECRET)

    def sign(payload: dict[str, object]) -> tuple[bytes, str]:
        raw = json.dumps(payload, sort_keys=True).encode()
        return raw, scheme.sign(raw)

    return sign


def event_body(
    *,
    event_id: str | None,
    event_type: str,
    location_id: str = LOCATION_ID,
    resource_id: str | None = None,
    occurred_at: datetime | None = None,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """A webhook body as a plain dict, ready for the signer."""
    return {
        "event_id": event_id,
        "event_type": event_type,
        "location_id": location_id,
        "resource_id": resource_id,
        "occurred_at": (occurred_at or T0).isoformat(),
        "payload": payload or {},
    }


def make_event(**kwargs: object) -> WebhookEvent:
    return WebhookEvent.model_validate(event_body(**kwargs))  # type: ignore[arg-type]
