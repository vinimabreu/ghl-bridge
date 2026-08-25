"""The intake: signatures on raw bytes, idempotency on the event."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from tests.conftest import SECRET, T0, event_body

from ghl_bridge import (
    Accepted,
    AuditLedger,
    Duplicate,
    HmacSha256Scheme,
    Rejected,
    WebhookEvent,
    WebhookIntake,
    event_key,
)
from ghl_bridge.fakes import FakeClock


def make_intake(handled: list[WebhookEvent]) -> tuple[WebhookIntake, AuditLedger, FakeClock]:
    clock = FakeClock(T0)
    ledger = AuditLedger()
    intake = WebhookIntake(
        scheme=HmacSha256Scheme(secret=SECRET),
        handler=handled.append,
        ledger=ledger,
        clock=clock,
    )
    return intake, ledger, clock


def signed(payload: dict[str, object]) -> tuple[bytes, str]:
    raw = json.dumps(payload, sort_keys=True).encode()
    return raw, HmacSha256Scheme(secret=SECRET).sign(raw)


LEAD = event_body(
    event_id="evt-0001",
    event_type="ContactCreate",
    payload={"email": "ana@riverbend.example"},
)


# ---------------------------------------------------------------- signatures


def test_a_valid_signature_is_accepted_and_dispatched() -> None:
    handled: list[WebhookEvent] = []
    intake, _, _ = make_intake(handled)
    raw, signature = signed(LEAD)
    result = intake.receive(raw, signature=signature, delivery_id="dlv-1")
    assert isinstance(result, Accepted)
    assert len(handled) == 1
    assert handled[0].event_type == "ContactCreate"


def test_a_wrong_secret_is_rejected_before_parsing() -> None:
    handled: list[WebhookEvent] = []
    intake, _, _ = make_intake(handled)
    raw, _ = signed(LEAD)
    forged = HmacSha256Scheme(secret=b"the-wrong-secret").sign(raw)
    result = intake.receive(raw, signature=forged, delivery_id="dlv-1")
    assert isinstance(result, Rejected)
    assert handled == []


def test_a_tampered_body_fails_the_original_signature() -> None:
    handled: list[WebhookEvent] = []
    intake, _, _ = make_intake(handled)
    raw, signature = signed(LEAD)
    tampered = raw.replace(b"ana@", b"eve@")
    result = intake.receive(tampered, signature=signature, delivery_id="dlv-1")
    assert isinstance(result, Rejected)
    assert handled == []


def test_an_empty_signature_is_rejected_with_its_own_reason() -> None:
    handled: list[WebhookEvent] = []
    intake, _, _ = make_intake(handled)
    raw, _ = signed(LEAD)
    result = intake.receive(raw, signature="", delivery_id="dlv-1")
    assert isinstance(result, Rejected)
    assert result.reason == "empty signature header"


def test_a_rejection_lands_in_the_ledger_with_the_delivery_named() -> None:
    handled: list[WebhookEvent] = []
    intake, ledger, _ = make_intake(handled)
    raw, _ = signed(LEAD)
    intake.receive(raw, signature="deadbeef", delivery_id="dlv-9")
    rejections = ledger.of_kind("webhook_rejected")
    assert len(rejections) == 1
    assert rejections[0].detail["delivery_id"] == "dlv-9"


def test_signature_verification_covers_raw_bytes_not_parsed_json() -> None:
    """Two byte strings that parse to the same JSON are different messages
    to the verifier; whitespace is not free."""
    handled: list[WebhookEvent] = []
    intake, _, _ = make_intake(handled)
    raw, signature = signed(LEAD)
    reserialized = json.dumps(json.loads(raw), indent=2).encode()
    result = intake.receive(reserialized, signature=signature, delivery_id="dlv-1")
    assert isinstance(result, Rejected)


def test_an_empty_secret_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="empty secret"):
        HmacSha256Scheme(secret=b"")


# --------------------------------------------------------------- idempotency


def test_the_same_event_redelivered_has_exactly_one_effect() -> None:
    """The thesis test: same event, two deliveries, two delivery ids, one
    effect. Keying on the delivery id would run the handler twice."""
    handled: list[WebhookEvent] = []
    intake, _, _ = make_intake(handled)
    raw, signature = signed(LEAD)
    first = intake.receive(raw, signature=signature, delivery_id="dlv-1")
    second = intake.receive(raw, signature=signature, delivery_id="dlv-2")
    assert isinstance(first, Accepted)
    assert isinstance(second, Duplicate)
    assert len(handled) == 1


def test_the_duplicate_names_the_delivery_that_won() -> None:
    handled: list[WebhookEvent] = []
    intake, _, _ = make_intake(handled)
    raw, signature = signed(LEAD)
    intake.receive(raw, signature=signature, delivery_id="dlv-1")
    result = intake.receive(raw, signature=signature, delivery_id="dlv-2")
    assert isinstance(result, Duplicate)
    assert result.first_delivery_id == "dlv-1"
    assert result.delivery_id == "dlv-2"


def test_two_distinct_events_have_two_effects() -> None:
    handled: list[WebhookEvent] = []
    intake, _, _ = make_intake(handled)
    other = event_body(
        event_id="evt-0002",
        event_type="ContactCreate",
        payload={"email": "bo@riverbend.example"},
    )
    for body in (LEAD, other):
        raw, signature = signed(body)
        intake.receive(raw, signature=signature, delivery_id=f"dlv-{body['event_id']}")
    assert len(handled) == 2


def test_a_duplicate_is_recorded_in_the_ledger_pointing_at_the_first() -> None:
    handled: list[WebhookEvent] = []
    intake, ledger, _ = make_intake(handled)
    raw, signature = signed(LEAD)
    intake.receive(raw, signature=signature, delivery_id="dlv-1")
    intake.receive(raw, signature=signature, delivery_id="dlv-2")
    duplicates = ledger.of_kind("webhook_duplicate")
    assert len(duplicates) == 1
    assert duplicates[0].detail["first_delivery_id"] == "dlv-1"


def test_a_failing_handler_does_not_poison_the_event_key() -> None:
    """At-least-once delivery meets at-most-once effect: the failed attempt
    leaves the key unmarked so the sender's retry can complete the work,
    and the failure itself is a ledger line."""
    calls: list[int] = []

    def flaky(event: WebhookEvent) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("downstream hiccup")

    clock = FakeClock(T0)
    ledger = AuditLedger()
    intake = WebhookIntake(
        scheme=HmacSha256Scheme(secret=SECRET),
        handler=flaky,
        ledger=ledger,
        clock=clock,
    )
    raw, signature = signed(LEAD)
    with pytest.raises(RuntimeError):
        intake.receive(raw, signature=signature, delivery_id="dlv-1")
    assert len(ledger.of_kind("webhook_handler_failed")) == 1
    result = intake.receive(raw, signature=signature, delivery_id="dlv-2")
    assert isinstance(result, Accepted)
    assert len(calls) == 2
    third = intake.receive(raw, signature=signature, delivery_id="dlv-3")
    assert isinstance(third, Duplicate)


# ----------------------------------------------------------------- event key


def event_of(body: dict[str, object]) -> WebhookEvent:
    return WebhookEvent.model_validate(body)


def test_the_key_prefers_the_platform_event_id() -> None:
    assert event_key(event_of(LEAD)) == "id:evt-0001"


def test_without_an_id_the_key_derives_from_identity_fields() -> None:
    body = event_body(
        event_id=None,
        event_type="InboundMessage",
        resource_id="msg-1",
    )
    key = event_key(event_of(body))
    assert key.startswith("derived:")


def test_the_derived_key_is_stable_across_deliveries() -> None:
    body = event_body(event_id=None, event_type="InboundMessage", resource_id="msg-1")
    assert event_key(event_of(body)) == event_key(event_of(dict(body)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", "ContactCreate"),
        ("location_id", "loc-elsewhere"),
        ("resource_id", "msg-2"),
        ("occurred_at", datetime(2026, 3, 3, 20, 0, 1, tzinfo=UTC).isoformat()),
    ],
)
def test_changing_any_identity_field_changes_the_derived_key(field: str, value: str) -> None:
    base = event_body(event_id=None, event_type="InboundMessage", resource_id="msg-1")
    changed = dict(base)
    changed[field] = value
    assert event_key(event_of(base)) != event_key(event_of(changed))


def test_a_fresh_delivery_id_never_changes_the_key() -> None:
    """The delivery id does not participate in the key at all; that is the
    whole point of keying on the event."""
    body = event_body(event_id="evt-7", event_type="ContactCreate")
    assert event_key(event_of(body)) == "id:evt-7"
