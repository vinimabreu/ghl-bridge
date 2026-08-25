"""The redundant layer: kill the gate and the guard still catches it."""

from __future__ import annotations

import pytest
from tests.conftest import LOCATION_ID, build_location, make_event, template_generator

from ghl_bridge import (
    Bridge,
    ContactDeduper,
    OutboundSend,
    Outcome,
    PolicyGate,
    UnapprovedOutbound,
    approve_draft,
    content_fingerprint,
)
from ghl_bridge.guard import ApprovedSender


def make_sender(server, token, clock, ledger) -> ApprovedSender:
    return ApprovedSender(port=server.port_for(token), ledger=ledger, clock=clock)


def make_gate_and_decision(clock, dana, draft):
    gate = PolicyGate(clock=clock)
    decision = gate.evaluate(
        location=build_location(),
        contact=dana,
        inbound_text="What times do you have on Thursday?",
        draft=draft,
    )
    return gate, decision


def test_an_approved_send_goes_through_and_is_recorded(server, token, clock, ledger, dana) -> None:
    draft = OutboundSend(contact_id=dana.contact_id, body="Thursday works.")
    gate, decision = make_gate_and_decision(clock, dana, draft)
    sender = make_sender(server, token, clock, ledger)
    message = sender.send(
        LOCATION_ID, draft, approval=gate.approval_for(decision), event_key="id:evt-1"
    )
    assert message.body == "Thursday works."
    sent = ledger.of_kind("message_sent")
    assert len(sent) == 1
    assert sent[0].approval is not None
    assert sent[0].approval.mode == "auto"


def test_no_approval_at_all_raises_and_nothing_sends(server, token, clock, ledger, dana) -> None:
    sender = make_sender(server, token, clock, ledger)
    draft = OutboundSend(contact_id=dana.contact_id, body="sneaky")
    with pytest.raises(UnapprovedOutbound, match="no approval attached"):
        sender.send(LOCATION_ID, draft, approval=None)
    port = server.port_for(token)
    conversations = ledger.of_kind("message_sent")
    assert conversations == ()
    assert port.search_contacts_by_email(LOCATION_ID, "x@y.example") == ()  # port still usable


def test_the_breach_is_recorded_before_it_is_raised(server, token, clock, ledger, dana) -> None:
    sender = make_sender(server, token, clock, ledger)
    draft = OutboundSend(contact_id=dana.contact_id, body="sneaky")
    with pytest.raises(UnapprovedOutbound):
        sender.send(LOCATION_ID, draft, approval=None)
    breaches = ledger.of_kind("guard_breach")
    assert len(breaches) == 1
    assert breaches[0].detail["why"] == "no approval attached"


def test_an_edited_draft_fails_the_fingerprint(server, token, clock, ledger, dana) -> None:
    """Approve one text, send another: the approval does not travel."""
    draft = OutboundSend(contact_id=dana.contact_id, body="Thursday works.")
    gate, decision = make_gate_and_decision(clock, dana, draft)
    approval = gate.approval_for(decision)
    edited = OutboundSend(contact_id=dana.contact_id, body="Thursday works, and it is $50 off.")
    sender = make_sender(server, token, clock, ledger)
    with pytest.raises(UnapprovedOutbound, match="different content"):
        sender.send(LOCATION_ID, edited, approval=approval)


def test_an_approval_reattached_to_another_contact_fails(
    server, token, clock, ledger, dana, port
) -> None:
    from ghl_bridge import ContactUpsert

    other = port.upsert_contact(LOCATION_ID, ContactUpsert(email="o@riverbend.example"))
    draft = OutboundSend(contact_id=dana.contact_id, body="Thursday works.")
    gate, decision = make_gate_and_decision(clock, dana, draft)
    approval = gate.approval_for(decision)
    redirected = OutboundSend(contact_id=other.contact_id, body="Thursday works.")
    sender = make_sender(server, token, clock, ledger)
    with pytest.raises(UnapprovedOutbound):
        sender.send(LOCATION_ID, redirected, approval=approval)


def test_one_approval_cannot_send_twice(server, token, clock, ledger, dana) -> None:
    draft = OutboundSend(contact_id=dana.contact_id, body="Thursday works.")
    gate, decision = make_gate_and_decision(clock, dana, draft)
    approval = gate.approval_for(decision)
    sender = make_sender(server, token, clock, ledger)
    sender.send(LOCATION_ID, draft, approval=approval)
    with pytest.raises(UnapprovedOutbound, match="already used"):
        sender.send(LOCATION_ID, draft, approval=approval)


def test_a_human_approval_passes_the_guard(server, token, clock, ledger, dana) -> None:
    clock.advance(8 * 3600)  # park it after hours
    draft = OutboundSend(contact_id=dana.contact_id, body="Thursday works.")
    _, decision = make_gate_and_decision(clock, dana, draft)
    assert decision.outcome is Outcome.DRAFT_FOR_HUMAN
    approval = approve_draft(decision, approver="sam@riverbend.example", clock=clock)
    sender = make_sender(server, token, clock, ledger)
    message = sender.send(LOCATION_ID, draft, approval=approval)
    assert message.message_id.startswith("msg-")


def test_killing_the_gate_is_caught_by_the_guard_not_by_luck(
    server, token, clock, ledger, location, dana, signer
) -> None:
    """The mutation test from the spec: rewire the bridge so the gate is
    effectively dead (a subclassed bridge that sends without asking the
    gate for an approval) and prove the breach stops at the guard, with a
    ledger record naming the reason."""

    port = server.port_for(token)
    sender = ApprovedSender(port=port, ledger=ledger, clock=clock)
    deduper = ContactDeduper(port=port, ledger=ledger, clock=clock)

    class GateKilledBridge(Bridge):
        def _handle_inbound(self, event) -> None:  # type: ignore[override]
            contact = self._port.get_contact(  # type: ignore[attr-defined]
                LOCATION_ID, str(event.payload["contact_id"])
            )
            draft = OutboundSend(contact_id=contact.contact_id, body="unreviewed text")
            self._sender.send(LOCATION_ID, draft, approval=None)  # type: ignore[attr-defined]

    bridge = GateKilledBridge(
        location=location,
        port=port,
        deduper=deduper,
        gate=PolicyGate(clock=clock),
        sender=sender,
        generator=template_generator,
        ledger=ledger,
        clock=clock,
        lead_pipeline_id="pipe-sales",
        lead_stage_id="stg-new",
    )
    event = make_event(
        event_id="evt-inbound",
        event_type="InboundMessage",
        payload={"contact_id": dana.contact_id, "body": "hello"},
    )
    with pytest.raises(UnapprovedOutbound):
        bridge.handle_event(event)
    assert len(ledger.of_kind("guard_breach")) == 1
    assert ledger.of_kind("message_sent") == ()


def test_the_fingerprint_covers_contact_channel_and_body() -> None:
    base = OutboundSend(contact_id="con-1", body="hello")
    assert content_fingerprint(base) != content_fingerprint(
        OutboundSend(contact_id="con-2", body="hello")
    )
    assert content_fingerprint(base) != content_fingerprint(
        OutboundSend(contact_id="con-1", body="hello ")
    )
    assert content_fingerprint(base) != content_fingerprint(
        OutboundSend(contact_id="con-1", body="hello", channel="Email")
    )
    assert content_fingerprint(base) == content_fingerprint(
        OutboundSend(contact_id="con-1", body="hello")
    )


def test_the_exception_carries_structured_detail() -> None:
    exc = UnapprovedOutbound(contact_id="con-9", why="no approval attached")
    assert exc.contact_id == "con-9"
    assert exc.why == "no approval attached"
    assert "con-9" in str(exc)
