"""The typed vocabulary of the bridge.

Every model is frozen: a contact, a message or an approval that mutates in
flight cannot be audited, and the ledger is only worth keeping if the
objects it references stay what they were. Field names follow the public
HighLevel API 2.0 documentation in spirit (camelCase mapped to snake_case);
where the exact wire shape could not be verified offline, the invariant is
modelled and the uncertainty is stated in the docstring and in the README,
with the RUNBOOK holding the live verification steps.
"""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .clock import require_aware


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class BusinessHours(_Frozen):
    """When a location answers customers, in the location's own timezone.

    ``open_time`` is inclusive and ``close_time`` is exclusive: a message at
    the opening instant is inside hours, a message at the closing instant is
    outside. The boundary has to land somewhere, and it is pinned by test
    rather than left to whoever reads the comparison operator.
    """

    days: frozenset[int]
    open_time: time
    close_time: time

    @field_validator("days")
    @classmethod
    def _valid_days(cls, value: frozenset[int]) -> frozenset[int]:
        bad = sorted(d for d in value if not 0 <= d <= 6)
        if bad:
            raise ValueError(f"weekdays are 0 (Monday) to 6 (Sunday); got {bad}")
        return value

    @model_validator(mode="after")
    def _open_before_close(self) -> BusinessHours:
        if self.open_time >= self.close_time:
            raise ValueError(
                "open_time must be before close_time; an overnight window is "
                "two windows, model it as two locations or extend this class"
            )
        return self

    def contains(self, local: datetime) -> bool:
        if local.weekday() not in self.days:
            return False
        moment = local.timetz().replace(tzinfo=None)
        return self.open_time <= moment < self.close_time


class Location(_Frozen):
    """A HighLevel sub-account. A Private Integration token is scoped to one.

    ``timezone`` is an IANA name, validated at construction, because the
    business-hours policy converts the injected clock into this zone before
    deciding whether a reply may leave on its own.
    """

    location_id: str
    name: str
    timezone: str
    default_region: str | None = None
    business_hours: BusinessHours

    @field_validator("timezone")
    @classmethod
    def _valid_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"{value!r} is not an IANA timezone name") from exc
        return value

    def local_now(self, now: datetime) -> datetime:
        return require_aware(now, field="now").astimezone(ZoneInfo(self.timezone))


class ContactUpsert(_Frozen):
    """The write half of a contact, before the platform assigns an id.

    ``email`` and ``phone`` are carried raw; normalisation to dedupe keys
    is the job of :mod:`ghl_bridge.identity` and happens where the merge
    decision is made, so the ledger can show both the raw input and the key
    it folded to.
    """

    first_name: str = ""
    last_name: str = ""
    email: str | None = None
    phone: str | None = None
    tags: tuple[str, ...] = ()
    custom_fields: dict[str, str] = {}
    source: str = ""


class Contact(_Frozen):
    contact_id: str
    location_id: str
    first_name: str = ""
    last_name: str = ""
    email: str | None = None
    phone: str | None = None
    tags: tuple[str, ...] = ()
    custom_fields: dict[str, str] = {}
    source: str = ""
    opted_out: bool = False
    """DND in HighLevel terms: the contact asked not to be messaged."""


class PipelineStage(_Frozen):
    stage_id: str
    name: str
    position: int


class Pipeline(_Frozen):
    """An ordered ladder of stages. Order is identity here: moving an
    opportunity means naming a stage that exists in this pipeline, and the
    fake refuses a stage id it does not hold, the way the platform 404s."""

    pipeline_id: str
    name: str
    stages: tuple[PipelineStage, ...]

    @field_validator("stages")
    @classmethod
    def _ordered(cls, value: tuple[PipelineStage, ...]) -> tuple[PipelineStage, ...]:
        positions = [s.position for s in value]
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            raise ValueError("stage positions must be strictly increasing")
        return value

    def stage(self, stage_id: str) -> PipelineStage | None:
        for s in self.stages:
            if s.stage_id == stage_id:
                return s
        return None


class OpportunityCreate(_Frozen):
    pipeline_id: str
    stage_id: str
    contact_id: str
    name: str
    monetary_value: float = 0.0


class Opportunity(_Frozen):
    opportunity_id: str
    location_id: str
    pipeline_id: str
    stage_id: str
    contact_id: str
    name: str
    monetary_value: float = 0.0
    status: Literal["open", "won", "lost", "abandoned"] = "open"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Message(_Frozen):
    message_id: str
    conversation_id: str
    direction: MessageDirection
    channel: str = "SMS"
    body: str
    at: datetime

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return require_aware(value, field="Message.at")


class Conversation(_Frozen):
    conversation_id: str
    location_id: str
    contact_id: str
    messages: tuple[Message, ...] = ()


class OutboundSend(_Frozen):
    """What the caller asks the platform to send. Approval is not a field
    here on purpose: it travels beside the send in
    :class:`ghl_bridge.guard.ApprovedOutbound`, so a send without one is a
    type error at the guard, not a default that quietly passes."""

    contact_id: str
    body: str
    channel: str = "SMS"


class CalendarSlot(_Frozen):
    calendar_id: str
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return require_aware(value, field="CalendarSlot start/end")


class BookingRequest(_Frozen):
    calendar_id: str
    contact_id: str
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return require_aware(value, field="BookingRequest start/end")


class Appointment(_Frozen):
    appointment_id: str
    calendar_id: str
    contact_id: str
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return require_aware(value, field="Appointment start/end")


class WebhookSubscription(_Frozen):
    url: str
    events: tuple[str, ...]


class WebhookRegistration(_Frozen):
    registration_id: str
    location_id: str
    url: str
    events: tuple[str, ...]


class WebhookEvent(_Frozen):
    """One event from the platform, after signature verification.

    ``event_id`` is optional because the public docs show an identifier on
    the payload but its presence on every event type could not be verified
    offline. :func:`ghl_bridge.webhook.event_key` prefers it and falls back
    to a canonical derivation from the event's own identity fields, never
    from the delivery attempt.
    """

    event_id: str | None = None
    event_type: str
    location_id: str
    resource_id: str | None = None
    occurred_at: datetime
    payload: dict[str, object] = {}

    @field_validator("occurred_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return require_aware(value, field="WebhookEvent.occurred_at")


class Approval(_Frozen):
    """The named permission for one specific outbound message.

    ``content_sha256`` binds the approval to the exact bytes that were
    approved: edit the draft after approval and the guard refuses the send,
    because approving one text and sending another is not approval.
    """

    decision_id: str
    mode: Literal["auto", "human"]
    approved_by: str
    content_sha256: str
    at: datetime

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return require_aware(value, field="Approval.at")
