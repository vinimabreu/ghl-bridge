"""The port the bridge talks through, and the errors the platform answers with.

:class:`HighLevelPort` is the contract: the operations the public API 2.0
exposes for contacts, opportunities, conversations, calendars and webhooks,
as one typed protocol. Two implementations ship in this package and both
answer to the same test suite where behaviour overlaps:

- :class:`ghl_bridge.fakes.FakeHighLevel` models the documented semantics
  deterministically, offline, with no account and no key;
- :class:`ghl_bridge.live.LiveHighLevel` maps the same operations onto
  ``services.leadconnectorhq.com`` with a Private Integration token, behind
  the optional ``[live]`` extra.

The errors are typed because the bridge routes on them: a 429 becomes a
computed wait, a cross-location 403 becomes a hard stop, a missing stage
becomes a refusal with the pipeline named. String-matching an error message
is how a retry loop ends up retrying a permission failure.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

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


class HighLevelError(Exception):
    """Base for every error the port can answer with."""


class RateLimited(HighLevelError):
    """HTTP 429. Carries the platform's own numbers so the caller can wait
    exactly as long as told instead of guessing an exponential backoff.

    Header names follow the public rate-limit documentation; the RUNBOOK
    holds the step that confirms them against a live workspace.
    """

    def __init__(self, *, retry_after_seconds: float, headers: dict[str, str]) -> None:
        self.retry_after_seconds = retry_after_seconds
        self.headers = headers
        super().__init__(
            f"rate limited; the platform says retry in {retry_after_seconds:.3f}s"
        )


class Unauthorized(HighLevelError):
    """HTTP 401: the token is unknown or revoked."""

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__(f"unauthorized: {detail}")


class CrossLocationDenied(HighLevelError):
    """HTTP 403 for a resource outside the token's sub-account.

    A Private Integration token is scoped to one location. Answering with a
    typed refusal instead of an empty list is the difference between a
    permission event an operator can alarm on and a lead that silently
    never syncs.
    """

    def __init__(self, *, token_location: str, requested_location: str) -> None:
        self.token_location = token_location
        self.requested_location = requested_location
        super().__init__(
            f"token is scoped to location {token_location!r}; "
            f"it cannot touch location {requested_location!r}"
        )


class NotFound(HighLevelError):
    """HTTP 404 for a named resource."""

    def __init__(self, *, kind: str, resource_id: str) -> None:
        self.kind = kind
        self.resource_id = resource_id
        super().__init__(f"{kind} {resource_id!r} does not exist")


class StageNotInPipeline(HighLevelError):
    """The requested stage id is not one of this pipeline's stages.

    Distinct from :class:`NotFound` because the fix is different: the
    pipeline exists, the automation is holding a stale or wrong stage map.
    """

    def __init__(self, *, pipeline_id: str, stage_id: str) -> None:
        self.pipeline_id = pipeline_id
        self.stage_id = stage_id
        super().__init__(
            f"stage {stage_id!r} is not a stage of pipeline {pipeline_id!r}; "
            "refusing to file the opportunity somewhere that does not exist"
        )


class SlotTaken(HighLevelError):
    """The calendar slot is already booked. Double-booking is refused, not
    resolved by overwriting whoever booked first."""

    def __init__(self, *, calendar_id: str, start: str) -> None:
        self.calendar_id = calendar_id
        self.start = start
        super().__init__(f"calendar {calendar_id!r} already has a booking at {start}")


class InvalidRequest(HighLevelError):
    """HTTP 422: the request shape is wrong. Carries the platform's detail."""

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__(f"the platform refused the request: {detail}")


class HighLevelPort(Protocol):
    """Everything the bridge is allowed to ask of the platform.

    Each operation names the location it acts on, mirroring the API, and an
    implementation must refuse a location the credential does not cover
    with :class:`CrossLocationDenied`.
    """

    def upsert_contact(self, location_id: str, contact: ContactUpsert) -> Contact: ...

    def get_contact(self, location_id: str, contact_id: str) -> Contact: ...

    def search_contacts_by_email(self, location_id: str, email: str) -> tuple[Contact, ...]: ...

    def search_contacts_by_phone(self, location_id: str, phone: str) -> tuple[Contact, ...]: ...

    def list_pipelines(self, location_id: str) -> tuple[Pipeline, ...]: ...

    def create_opportunity(
        self, location_id: str, create: OpportunityCreate
    ) -> Opportunity: ...

    def move_opportunity(
        self, location_id: str, opportunity_id: str, stage_id: str
    ) -> Opportunity: ...

    def get_conversation(self, location_id: str, conversation_id: str) -> Conversation: ...

    def send_message(self, location_id: str, send: OutboundSend) -> Message: ...

    def free_slots(
        self, location_id: str, calendar_id: str, day: date
    ) -> tuple[CalendarSlot, ...]: ...

    def book_appointment(self, location_id: str, booking: BookingRequest) -> Appointment: ...

    def register_webhook(
        self, location_id: str, subscription: WebhookSubscription
    ) -> WebhookRegistration: ...
