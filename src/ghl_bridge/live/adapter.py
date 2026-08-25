"""The live implementation of the port, honestly labelled.

:class:`LiveHighLevel` implements the same :class:`~ghl_bridge.ports.HighLevelPort`
as the fake, against ``services.leadconnectorhq.com`` with a Private
Integration token. The transport is injected, so every URL, parameter and
error mapping in this file is exercised offline by the suite against a
recorded-shape fake transport; what has NOT happened, and is stated in the
README rather than hidden, is a run against a live workspace. The RUNBOOK
is the checklist for the day a workspace exists: issue the token, run the
smoke sequence, and diff the real response shapes against
:mod:`ghl_bridge.live.mapping`'s expectations.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ..clock import Clock, system_clock
from ..models import (
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
from . import mapping
from .mapping import HttpResponse, PlannedRequest


class Transport(Protocol):
    """Sends one planned request, returns the wire answer. The seam that
    keeps this adapter testable without a network."""

    def send(self, request: PlannedRequest) -> HttpResponse: ...


class LiveHighLevel:
    """One location, one token, the documented endpoints."""

    def __init__(
        self,
        *,
        location_id: str,
        transport: Transport,
        clock: Clock = system_clock,
    ) -> None:
        self._location_id = location_id
        self._transport = transport
        self._clock = clock

    def _send(self, location_id: str, request: PlannedRequest) -> HttpResponse:
        response = self._transport.send(request)
        mapping.raise_for_status(
            response,
            token_location=self._location_id,
            requested_location=location_id,
        )
        return response

    def upsert_contact(self, location_id: str, contact: ContactUpsert) -> Contact:
        response = self._send(location_id, mapping.plan_upsert_contact(location_id, contact))
        raw = response.body.get("contact", response.body)
        return mapping.parse_contact(location_id, raw if isinstance(raw, dict) else {})

    def get_contact(self, location_id: str, contact_id: str) -> Contact:
        response = self._send(location_id, mapping.plan_get_contact(contact_id))
        raw = response.body.get("contact", response.body)
        return mapping.parse_contact(location_id, raw if isinstance(raw, dict) else {})

    def search_contacts_by_email(self, location_id: str, email: str) -> tuple[Contact, ...]:
        return self._search(location_id, query=email)

    def search_contacts_by_phone(self, location_id: str, phone: str) -> tuple[Contact, ...]:
        return self._search(location_id, query=phone)

    def _search(self, location_id: str, *, query: str) -> tuple[Contact, ...]:
        response = self._send(
            location_id, mapping.plan_search_contacts(location_id, query=query)
        )
        raw = response.body.get("contacts", [])
        if not isinstance(raw, list):
            return ()
        return tuple(
            mapping.parse_contact(location_id, item)
            for item in raw
            if isinstance(item, dict)
        )

    def list_pipelines(self, location_id: str) -> tuple[Pipeline, ...]:
        response = self._send(location_id, mapping.plan_list_pipelines(location_id))
        return mapping.parse_pipelines(response.body)

    def create_opportunity(
        self, location_id: str, create: OpportunityCreate
    ) -> Opportunity:
        response = self._send(
            location_id, mapping.plan_create_opportunity(location_id, create)
        )
        raw = response.body.get("opportunity", response.body)
        return mapping.parse_opportunity(location_id, raw if isinstance(raw, dict) else {})

    def move_opportunity(
        self, location_id: str, opportunity_id: str, stage_id: str
    ) -> Opportunity:
        response = self._send(
            location_id, mapping.plan_move_opportunity(opportunity_id, stage_id)
        )
        raw = response.body.get("opportunity", response.body)
        return mapping.parse_opportunity(location_id, raw if isinstance(raw, dict) else {})

    def get_conversation(self, location_id: str, conversation_id: str) -> Conversation:
        response = self._send(location_id, mapping.plan_get_conversation(conversation_id))
        return mapping.parse_conversation(location_id, response.body)

    def send_message(self, location_id: str, send: OutboundSend) -> Message:
        response = self._send(location_id, mapping.plan_send_message(send))
        return mapping.parse_message(response.body, fallback_at=self._clock())

    def free_slots(
        self, location_id: str, calendar_id: str, day: date
    ) -> tuple[CalendarSlot, ...]:
        response = self._send(location_id, mapping.plan_free_slots(calendar_id, day))
        return mapping.parse_free_slots(calendar_id, response.body)

    def book_appointment(self, location_id: str, booking: BookingRequest) -> Appointment:
        response = self._send(
            location_id, mapping.plan_book_appointment(location_id, booking)
        )
        raw = response.body.get("appointment", response.body)
        return mapping.parse_appointment(
            raw if isinstance(raw, dict) else {}, booking=booking
        )

    def register_webhook(
        self, location_id: str, subscription: WebhookSubscription
    ) -> WebhookRegistration:
        """Refused honestly. Marketplace app webhooks are configured in the
        app's settings, not through a per-location endpoint this build
        could verify offline. Raising beats encoding a guessed endpoint
        that fails at the first live call with a confusing 404."""
        raise NotImplementedError(
            "webhook subscriptions for a marketplace app are configured in the "
            "app settings; see the RUNBOOK's webhook step"
        )
