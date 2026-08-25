"""The whole lane, end to end, through the signed intake."""

from __future__ import annotations

import json

from tests.conftest import (
    LOCATION_ID,
    PIPELINE_ID,
    SECRET,
    STAGE_NEW,
    event_body,
    make_event,
)

from ghl_bridge import (
    Accepted,
    Duplicate,
    HmacSha256Scheme,
    MessageDirection,
)


def deliver(intake, body: dict[str, object], delivery_id: str):
    raw = json.dumps(body, sort_keys=True).encode()
    signature = HmacSha256Scheme(secret=SECRET).sign(raw)
    return intake.receive(raw, signature=signature, delivery_id=delivery_id)


LEAD_EVENT = event_body(
    event_id="evt-lead-1",
    event_type="ContactCreate",
    payload={
        "first_name": "Dana",
        "last_name": "Whitfield",
        "email": " DANA@Riverbend.example ",
        "source": "website form",
    },
)


def inbound_event(body: str, event_id: str, contact_id: str) -> dict[str, object]:
    return event_body(
        event_id=event_id,
        event_type="InboundMessage",
        payload={"contact_id": contact_id, "body": body},
    )


# ------------------------------------------------------------------ the lane


def test_a_lead_webhook_merges_and_files_an_opportunity(intake, wired, ledger, dana, port) -> None:
    result = deliver(intake, LEAD_EVENT, "dlv-1")
    assert isinstance(result, Accepted)
    merged = ledger.of_kind("contact_merged")
    assert len(merged) == 1
    assert merged[0].detail["key"] == "dana@riverbend.example"
    opportunities = ledger.of_kind("opportunity_created")
    assert len(opportunities) == 1
    assert opportunities[0].detail["pipeline_id"] == PIPELINE_ID
    assert opportunities[0].detail["stage_id"] == STAGE_NEW
    assert opportunities[0].detail["contact_id"] == dana.contact_id


def test_a_brand_new_lead_creates_then_files(intake, ledger) -> None:
    body = event_body(
        event_id="evt-lead-2",
        event_type="ContactCreate",
        payload={"first_name": "Ana", "email": "ana@riverbend.example"},
    )
    deliver(intake, body, "dlv-1")
    assert len(ledger.of_kind("contact_created")) == 1
    assert len(ledger.of_kind("opportunity_created")) == 1


def test_replaying_the_lead_webhook_does_not_duplicate_the_opportunity(
    intake, ledger
) -> None:
    """The replay case from the spec: same event, new delivery id, zero new
    effects. One merge, one opportunity, and the duplicate on the record."""
    deliver(intake, LEAD_EVENT, "dlv-1")
    result = deliver(intake, LEAD_EVENT, "dlv-2")
    assert isinstance(result, Duplicate)
    assert len(ledger.of_kind("opportunity_created")) == 1
    assert len(ledger.of_kind("webhook_duplicate")) == 1


def test_a_lead_that_needs_review_files_no_opportunity(intake, ledger) -> None:
    body = event_body(
        event_id="evt-lead-4",
        event_type="ContactCreate",
        payload={"first_name": "Ghost"},
    )
    deliver(intake, body, "dlv-1")
    assert len(ledger.of_kind("lead_needs_review")) == 1
    assert ledger.of_kind("opportunity_created") == ()


def test_an_inbound_inside_policy_auto_sends(
    intake, wired, ledger, dana, port, server, clock
) -> None:
    deliver(
        intake,
        inbound_event("What times do you have on Thursday?", "evt-in-1", dana.contact_id),
        "dlv-1",
    )
    sent = ledger.of_kind("message_sent")
    assert len(sent) == 1
    assert sent[0].detail["mode"] == "auto"
    message_id = str(sent[0].detail["message_id"])
    # the message is really in the fake conversation store
    inbox = server.seed_inbound(LOCATION_ID, dana.contact_id, "follow-up", clock())
    conversation = port.get_conversation(LOCATION_ID, inbox.conversation_id)
    bodies = [m.body for m in conversation.messages if m.direction is MessageDirection.OUTBOUND]
    assert any("Thursday morning" in b for b in bodies)
    assert message_id.startswith("msg-")


def test_the_gate_decision_is_on_the_ledger_with_every_policy(intake, ledger, dana) -> None:
    deliver(
        intake,
        inbound_event("What times do you have on Thursday?", "evt-in-2", dana.contact_id),
        "dlv-1",
    )
    decisions = ledger.of_kind("gate_decision")
    assert len(decisions) == 1
    evaluated = decisions[0].detail["evaluated"]
    assert isinstance(evaluated, list)
    assert len(evaluated) == 8


def test_an_after_hours_inbound_parks_with_the_reason(intake, wired, ledger, dana, clock) -> None:
    clock.advance(8 * 3600)  # 22:00 in Chicago
    deliver(
        intake,
        inbound_event("can you fit me in tomorrow?", "evt-in-3", dana.contact_id),
        "dlv-1",
    )
    assert ledger.of_kind("message_sent") == ()
    drafted = ledger.of_kind("message_drafted")
    assert len(drafted) == 1
    assert drafted[0].detail["reasons"] == ["within_business_hours"]
    assert len(wired.pending_drafts()) == 1


def test_a_parked_draft_releases_under_a_named_human(intake, wired, ledger, dana, clock) -> None:
    clock.advance(8 * 3600)
    deliver(
        intake,
        inbound_event("can you fit me in tomorrow?", "evt-in-4", dana.contact_id),
        "dlv-1",
    )
    pending = wired.pending_drafts()[0]
    message = wired.release_draft(pending.decision.decision_id, approver="sam@riverbend.example")
    assert message.message_id.startswith("msg-")
    sent = ledger.of_kind("message_sent")
    assert sent[0].detail["mode"] == "human"
    assert sent[0].detail["approved_by"] == "sam@riverbend.example"
    assert wired.pending_drafts() == ()


def test_releasing_a_ghost_decision_raises(wired) -> None:
    import pytest

    with pytest.raises(KeyError):
        wired.release_draft("dec-ghost", approver="sam")


def test_a_pricing_inbound_parks_for_intent_and_price(intake, wired, ledger, dana) -> None:
    """The template generator quotes a number on pricing questions, so two
    policies fail at once and both reasons are named."""
    deliver(
        intake,
        inbound_event("how much for a full detail?", "evt-in-5", dana.contact_id),
        "dlv-1",
    )
    drafted = ledger.of_kind("message_drafted")
    assert len(drafted) == 1
    assert set(drafted[0].detail["reasons"]) == {"intent_covered", "no_price_commitment"}


def test_a_stop_inbound_blocks_and_nothing_is_pending(intake, wired, ledger, dana) -> None:
    deliver(intake, inbound_event("STOP", "evt-in-6", dana.contact_id), "dlv-1")
    blocked = ledger.of_kind("message_blocked")
    assert len(blocked) == 1
    assert "not_a_reply_to_opt_out" in blocked[0].detail["reasons"]
    assert wired.pending_drafts() == ()
    assert ledger.of_kind("message_sent") == ()


def test_an_unknown_event_type_is_ignored_on_the_record(intake, ledger) -> None:
    body = event_body(event_id="evt-x", event_type="AppointmentDelete")
    deliver(intake, body, "dlv-1")
    ignored = ledger.of_kind("event_ignored")
    assert len(ignored) == 1
    assert ignored[0].detail["event_type"] == "AppointmentDelete"


def test_an_event_for_another_location_is_a_wiring_error(wired) -> None:
    import pytest

    event = make_event(
        event_id="evt-wrong",
        event_type="ContactCreate",
        location_id="loc-lakeshore",
        payload={"email": "a@b.example"},
    )
    with pytest.raises(ValueError, match="wiring error"):
        wired.handle_event(event)


def test_explain_message_answers_why_it_left_at_1403(intake, ledger, dana) -> None:
    """The operator's question, verbatim: the chain shows the inbound event,
    the draft, every policy evaluated, and the auto approval, in order."""
    deliver(
        intake,
        inbound_event("What times do you have on Thursday?", "evt-1403", dana.contact_id),
        "dlv-1",
    )
    sent = ledger.of_kind("message_sent")
    message_id = str(sent[0].detail["message_id"])
    chain = ledger.explain_message(message_id)
    kinds = [r.kind for r in chain]
    assert kinds == [
        "webhook_received",
        "draft_generated",
        "gate_decision",
        "message_sent",
    ]
    assert chain[-1].approval is not None
    assert chain[-1].approval.mode == "auto"


def test_the_full_story_two_events_two_outcomes_one_ledger(
    intake, wired, ledger, dana, clock
) -> None:
    deliver(
        intake,
        inbound_event("What times do you have on Thursday?", "evt-a", dana.contact_id),
        "dlv-1",
    )
    clock.advance(8 * 3600)
    deliver(
        intake,
        inbound_event("book me for tomorrow please", "evt-b", dana.contact_id),
        "dlv-2",
    )
    assert len(ledger.of_kind("message_sent")) == 1
    assert len(ledger.of_kind("message_drafted")) == 1
    assert len(wired.pending_drafts()) == 1
