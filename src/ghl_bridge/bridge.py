"""The orchestration: webhook in, policy-gated action out.

The bridge is deliberately thin. Identity is decided in
:mod:`ghl_bridge.dedupe`, safety in :mod:`ghl_bridge.policy`, the last
line of defence in :mod:`ghl_bridge.guard`, pacing in
:mod:`ghl_bridge.ratelimit`; this module only wires the story together:

- a new-lead event resolves to exactly one contact (merge, create, or a
  named refusal) and files an opportunity on the configured pipeline
  stage;
- an inbound message gets a draft from the injected generator, the draft
  goes to the policy gate, and the outcome is one of three named paths:
  sent with an auto approval, parked for a human, or blocked.

The generator is a plain callable from request to text. This package
neither knows nor cares what produces the draft; it cares what happens to
the draft afterwards, which is the part CRM automations usually skip.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .clock import Clock
from .dedupe import ContactDeduper, NeedsHumanReview, Resolved
from .guard import ApprovedSender
from .ledger import AuditLedger
from .models import (
    Contact,
    ContactUpsert,
    Location,
    Message,
    OpportunityCreate,
    OutboundSend,
    WebhookEvent,
)
from .policy import GateDecision, Outcome, PolicyGate, approve_draft
from .ports import HighLevelPort
from .webhook import event_key

EVENT_NEW_LEAD = "ContactCreate"
"""The documented notification for a contact created in the location.
The exact event-name strings a live app receives are confirmed in the
RUNBOOK; the dispatcher below keys on these constants so a rename is one
edit."""

EVENT_INBOUND_MESSAGE = "InboundMessage"
"""The documented notification for an inbound conversation message."""


@dataclass(frozen=True)
class GenerationRequest:
    """Everything the injected generator is shown. Deliberately small: the
    draft is judged by the gate on its content, not on what the generator
    promises about itself."""

    location: Location
    contact: Contact
    inbound_text: str


Generator = Callable[[GenerationRequest], str]
"""Produces a draft reply body. Injected; the suite and the demo use a
deterministic template."""


@dataclass(frozen=True)
class PendingDraft:
    """A draft the gate parked. Held with its full decision so the human
    who releases it sees the same reasons the gate saw."""

    decision: GateDecision
    contact: Contact
    inbound_text: str
    event_key: str


class Bridge:
    """One location's webhook-to-action loop, every step on the ledger."""

    def __init__(
        self,
        *,
        location: Location,
        port: HighLevelPort,
        deduper: ContactDeduper,
        gate: PolicyGate,
        sender: ApprovedSender,
        generator: Generator,
        ledger: AuditLedger,
        clock: Clock,
        lead_pipeline_id: str,
        lead_stage_id: str,
    ) -> None:
        self._location = location
        self._port = port
        self._deduper = deduper
        self._gate = gate
        self._sender = sender
        self._generator = generator
        self._ledger = ledger
        self._clock = clock
        self._lead_pipeline_id = lead_pipeline_id
        self._lead_stage_id = lead_stage_id
        self._pending: dict[str, PendingDraft] = {}

    # ---------------------------------------------------------------- intake

    def handle_event(self, event: WebhookEvent) -> None:
        """The handler the :class:`~ghl_bridge.webhook.WebhookIntake`
        dispatches to, after signature verification and event dedupe."""
        if event.location_id != self._location.location_id:
            raise ValueError(
                f"event for location {event.location_id!r} reached the bridge "
                f"for {self._location.location_id!r}; wiring error upstream"
            )
        if event.event_type == EVENT_NEW_LEAD:
            self._handle_new_lead(event)
        elif event.event_type == EVENT_INBOUND_MESSAGE:
            self._handle_inbound(event)
        else:
            self._ledger.record(
                at=self._clock(),
                kind="event_ignored",
                detail={
                    "event_type": event.event_type,
                    "event_key": event_key(event),
                },
            )

    # ---------------------------------------------------------------- leads

    def _handle_new_lead(self, event: WebhookEvent) -> None:
        key = event_key(event)
        lead = ContactUpsert(
            first_name=str(event.payload.get("first_name", "")),
            last_name=str(event.payload.get("last_name", "")),
            email=str(event.payload["email"]) if event.payload.get("email") else None,
            phone=str(event.payload["phone"]) if event.payload.get("phone") else None,
            source=str(event.payload.get("source", "")),
        )
        result = self._deduper.resolve(self._location, lead, event_key=key)
        if isinstance(result, NeedsHumanReview):
            return
        opportunity = self._port.create_opportunity(
            self._location.location_id,
            OpportunityCreate(
                pipeline_id=self._lead_pipeline_id,
                stage_id=self._lead_stage_id,
                contact_id=result.contact.contact_id,
                name=self._opportunity_name(result),
            ),
        )
        self._ledger.record(
            at=self._clock(),
            kind="opportunity_created",
            detail={
                "opportunity_id": opportunity.opportunity_id,
                "contact_id": result.contact.contact_id,
                "pipeline_id": opportunity.pipeline_id,
                "stage_id": opportunity.stage_id,
                "event_key": key,
            },
        )

    @staticmethod
    def _opportunity_name(result: Resolved) -> str:
        full = f"{result.contact.first_name} {result.contact.last_name}".strip()
        return full or result.contact.contact_id

    # ---------------------------------------------------------------- replies

    def _handle_inbound(self, event: WebhookEvent) -> None:
        key = event_key(event)
        contact_id = str(event.payload["contact_id"])
        body = str(event.payload["body"])
        contact = self._port.get_contact(self._location.location_id, contact_id)

        draft_body = self._generator(
            GenerationRequest(
                location=self._location, contact=contact, inbound_text=body
            )
        )
        draft = OutboundSend(contact_id=contact.contact_id, body=draft_body)
        self._ledger.record(
            at=self._clock(),
            kind="draft_generated",
            detail={
                "contact_id": contact.contact_id,
                "chars": len(draft_body),
                "event_key": key,
            },
        )

        decision = self._gate.evaluate(
            location=self._location,
            contact=contact,
            inbound_text=body,
            draft=draft,
        )
        self._ledger.record(
            at=self._clock(),
            kind="gate_decision",
            detail={
                "decision_id": decision.decision_id,
                "outcome": decision.outcome.value,
                "failed": list(decision.reasons),
                "evaluated": [
                    {"name": r.name, "passed": r.passed, "detail": r.detail}
                    for r in decision.results
                ],
                "event_key": key,
            },
        )

        if decision.outcome is Outcome.AUTO_SEND:
            approval = self._gate.approval_for(decision)
            self._sender.send(
                self._location.location_id, draft, approval=approval, event_key=key
            )
        elif decision.outcome is Outcome.DRAFT_FOR_HUMAN:
            self._pending[decision.decision_id] = PendingDraft(
                decision=decision,
                contact=contact,
                inbound_text=body,
                event_key=key,
            )
            self._ledger.record(
                at=self._clock(),
                kind="message_drafted",
                detail={
                    "decision_id": decision.decision_id,
                    "contact_id": contact.contact_id,
                    "reasons": list(decision.reasons),
                    "event_key": key,
                },
            )
        else:
            self._ledger.record(
                at=self._clock(),
                kind="message_blocked",
                detail={
                    "decision_id": decision.decision_id,
                    "contact_id": contact.contact_id,
                    "reasons": list(decision.reasons),
                    "event_key": key,
                },
            )

    # ---------------------------------------------------------------- humans

    def pending_drafts(self) -> tuple[PendingDraft, ...]:
        return tuple(self._pending.values())

    def release_draft(self, decision_id: str, *, approver: str) -> Message:
        """The human path: a named person releases a parked draft, unchanged.
        The approval binds to the exact content the gate evaluated, so an
        edited draft would be refused by the guard, not silently sent."""
        if decision_id not in self._pending:
            raise KeyError(f"no pending draft under decision {decision_id!r}")
        pending = self._pending[decision_id]
        approval = approve_draft(pending.decision, approver=approver, clock=self._clock)
        message = self._sender.send(
            self._location.location_id,
            pending.decision.draft,
            approval=approval,
            event_key=pending.event_key,
        )
        del self._pending[decision_id]
        return message
