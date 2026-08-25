"""The pure half of the live adapter: request shapes and response mapping.

Everything here is a function from values to values, kept apart from the
HTTP client so it stays under strict typing and full test coverage without
a network. The shapes are encoded from the public HighLevel API 2.0
documentation for ``services.leadconnectorhq.com``; where the docs left
room for doubt, the docstring on the specific function says so, and the
RUNBOOK carries the live verification step. Nothing in this module
pretends a precision the offline build cannot have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from ..clock import require_aware
from ..models import (
    Appointment,
    BookingRequest,
    CalendarSlot,
    Contact,
    ContactUpsert,
    Conversation,
    Message,
    MessageDirection,
    Opportunity,
    OpportunityCreate,
    OutboundSend,
    Pipeline,
    PipelineStage,
)
from ..ports import (
    CrossLocationDenied,
    HighLevelError,
    InvalidRequest,
    NotFound,
    RateLimited,
    Unauthorized,
)

BASE_URL = "https://services.leadconnectorhq.com"
"""The documented API 2.0 host."""

VERSION_CONTACTS = "2021-07-28"
"""The documented ``Version`` header for the contacts and opportunities
family of endpoints."""

VERSION_CONVERSATIONS = "2021-04-15"
"""The documented ``Version`` header for conversations and calendars.
Two version strings exist because the platform versions endpoint families
independently; the RUNBOOK confirms both against a live workspace."""


@dataclass(frozen=True)
class PlannedRequest:
    """One HTTP call, fully decided before any client library is involved."""

    method: str
    path: str
    version: str
    params: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, object] | None = None

    @property
    def url(self) -> str:
        return f"{BASE_URL}{self.path}"


@dataclass(frozen=True)
class HttpResponse:
    """What the transport hands back: status, headers, parsed JSON."""

    status: int
    headers: dict[str, str]
    body: dict[str, object]


def auth_headers(token: str, version: str) -> dict[str, str]:
    """The documented header pair: a bearer Private Integration token and
    the ``Version`` header the endpoint family requires."""
    return {
        "Authorization": f"Bearer {token}",
        "Version": version,
        "Accept": "application/json",
    }


def raise_for_status(
    response: HttpResponse, *, token_location: str, requested_location: str
) -> None:
    """Turn the documented error statuses into the port's typed errors, so
    the bridge routes on types for the live adapter exactly as it does for
    the fake."""
    if response.status < 400:
        return
    if response.status == 401:
        raise Unauthorized(detail=str(response.body.get("message", "invalid token")))
    if response.status == 403:
        raise CrossLocationDenied(
            token_location=token_location, requested_location=requested_location
        )
    if response.status == 404:
        raise NotFound(
            kind=str(response.body.get("resource", "resource")),
            resource_id=str(response.body.get("id", "unknown")),
        )
    if response.status == 422:
        raise InvalidRequest(detail=str(response.body.get("message", "unprocessable")))
    if response.status == 429:
        raise RateLimited(
            retry_after_seconds=retry_after_from_headers(response.headers),
            headers=dict(response.headers),
        )
    raise HighLevelError(
        f"unexpected status {response.status}: {response.body.get('message', '')}"
    )


def retry_after_from_headers(headers: dict[str, str]) -> float:
    """The platform's own retry hint, in order of preference: an explicit
    ``Retry-After``, then the documented burst-window interval, then a
    conservative default of the full published window."""
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    interval = headers.get("X-RateLimit-Interval-Milliseconds")
    if interval is not None:
        try:
            return max(0.0, float(interval) / 1000.0)
        except ValueError:
            pass
    return 10.0


# ---------------------------------------------------------------- contacts


def plan_upsert_contact(location_id: str, contact: ContactUpsert) -> PlannedRequest:
    """``POST /contacts/upsert`` as documented: the platform matches on
    email or phone inside the location and updates or creates."""
    body: dict[str, object] = {"locationId": location_id}
    if contact.first_name:
        body["firstName"] = contact.first_name
    if contact.last_name:
        body["lastName"] = contact.last_name
    if contact.email:
        body["email"] = contact.email
    if contact.phone:
        body["phone"] = contact.phone
    if contact.tags:
        body["tags"] = list(contact.tags)
    if contact.custom_fields:
        body["customFields"] = [
            {"key": key, "value": value} for key, value in contact.custom_fields.items()
        ]
    if contact.source:
        body["source"] = contact.source
    return PlannedRequest(
        method="POST", path="/contacts/upsert", version=VERSION_CONTACTS, json_body=body
    )


def plan_get_contact(contact_id: str) -> PlannedRequest:
    return PlannedRequest(
        method="GET", path=f"/contacts/{contact_id}", version=VERSION_CONTACTS
    )


def plan_search_contacts(location_id: str, *, query: str) -> PlannedRequest:
    """Contact lookup by email or phone.

    Encoded as ``GET /contacts/`` with ``locationId`` and ``query``, the
    lookup shape shown in the public docs. The docs also describe a newer
    ``POST /contacts/search`` with a filter body; if the live workspace
    prefers it, this is the single function to change, and the RUNBOOK's
    smoke step is what catches the difference.
    """
    return PlannedRequest(
        method="GET",
        path="/contacts/",
        version=VERSION_CONTACTS,
        params={"locationId": location_id, "query": query},
    )


def parse_contact(location_id: str, raw: dict[str, object]) -> Contact:
    """Field names encoded from the public API 2.0 docs (camelCase);
    ``dnd`` is the documented do-not-disturb flag this package reads as
    ``opted_out``. Verify against a live workspace via the RUNBOOK."""
    tags_raw = raw.get("tags", [])
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, list) else ()
    fields: dict[str, str] = {}
    fields_raw = raw.get("customFields", [])
    if isinstance(fields_raw, list):
        for item in fields_raw:
            if isinstance(item, dict):
                fields[str(item.get("key", item.get("id", "")))] = str(item.get("value", ""))
    return Contact(
        contact_id=str(raw.get("id", "")),
        location_id=location_id,
        first_name=str(raw.get("firstName", "") or ""),
        last_name=str(raw.get("lastName", "") or ""),
        email=str(raw["email"]) if raw.get("email") else None,
        phone=str(raw["phone"]) if raw.get("phone") else None,
        tags=tags,
        custom_fields=fields,
        source=str(raw.get("source", "") or ""),
        opted_out=bool(raw.get("dnd", False)),
    )


# ---------------------------------------------------------------- pipelines


def plan_list_pipelines(location_id: str) -> PlannedRequest:
    return PlannedRequest(
        method="GET",
        path="/opportunities/pipelines",
        version=VERSION_CONTACTS,
        params={"locationId": location_id},
    )


def parse_pipelines(raw: dict[str, object]) -> tuple[Pipeline, ...]:
    pipelines: list[Pipeline] = []
    items = raw.get("pipelines", [])
    if not isinstance(items, list):
        return ()
    for item in items:
        if not isinstance(item, dict):
            continue
        stages_raw = item.get("stages", [])
        stages: list[PipelineStage] = []
        if isinstance(stages_raw, list):
            for position, stage in enumerate(stages_raw):
                if isinstance(stage, dict):
                    stages.append(
                        PipelineStage(
                            stage_id=str(stage.get("id", "")),
                            name=str(stage.get("name", "")),
                            position=int(str(stage.get("position", position))),
                        )
                    )
        pipelines.append(
            Pipeline(
                pipeline_id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                stages=tuple(stages),
            )
        )
    return tuple(pipelines)


def plan_create_opportunity(
    location_id: str, create: OpportunityCreate
) -> PlannedRequest:
    return PlannedRequest(
        method="POST",
        path="/opportunities/",
        version=VERSION_CONTACTS,
        json_body={
            "locationId": location_id,
            "pipelineId": create.pipeline_id,
            "pipelineStageId": create.stage_id,
            "contactId": create.contact_id,
            "name": create.name,
            "monetaryValue": create.monetary_value,
            "status": "open",
        },
    )


def plan_move_opportunity(opportunity_id: str, stage_id: str) -> PlannedRequest:
    return PlannedRequest(
        method="PUT",
        path=f"/opportunities/{opportunity_id}",
        version=VERSION_CONTACTS,
        json_body={"pipelineStageId": stage_id},
    )


def parse_opportunity(location_id: str, raw: dict[str, object]) -> Opportunity:
    status = str(raw.get("status", "open"))
    if status not in ("open", "won", "lost", "abandoned"):
        status = "open"
    return Opportunity(
        opportunity_id=str(raw.get("id", "")),
        location_id=location_id,
        pipeline_id=str(raw.get("pipelineId", "")),
        stage_id=str(raw.get("pipelineStageId", "")),
        contact_id=str(raw.get("contactId", "")),
        name=str(raw.get("name", "")),
        monetary_value=float(str(raw.get("monetaryValue", 0.0))),
        status=status,  # narrowed by the check above; pydantic validates again
    )


# ---------------------------------------------------------------- messages


def plan_send_message(send: OutboundSend) -> PlannedRequest:
    """``POST /conversations/messages`` with ``type`` naming the channel,
    as documented for API 2.0."""
    return PlannedRequest(
        method="POST",
        path="/conversations/messages",
        version=VERSION_CONVERSATIONS,
        json_body={
            "type": send.channel,
            "contactId": send.contact_id,
            "message": send.body,
        },
    )


def plan_get_conversation(conversation_id: str) -> PlannedRequest:
    return PlannedRequest(
        method="GET",
        path=f"/conversations/{conversation_id}",
        version=VERSION_CONVERSATIONS,
    )


def parse_message(raw: dict[str, object], *, fallback_at: datetime) -> Message:
    """``direction`` and ``dateAdded`` as documented; a missing timestamp
    falls back to the adapter's clock reading rather than inventing one."""
    at_raw = raw.get("dateAdded")
    at = fallback_at
    if isinstance(at_raw, str):
        try:
            parsed = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
            at = require_aware(parsed, field="dateAdded")
        except ValueError:
            at = fallback_at
    direction = (
        MessageDirection.INBOUND
        if str(raw.get("direction", "outbound")) == "inbound"
        else MessageDirection.OUTBOUND
    )
    return Message(
        message_id=str(raw.get("id", raw.get("messageId", ""))),
        conversation_id=str(raw.get("conversationId", "")),
        direction=direction,
        channel=str(raw.get("type", "SMS")),
        body=str(raw.get("body", raw.get("message", ""))),
        at=at,
    )


def parse_conversation(location_id: str, raw: dict[str, object]) -> Conversation:
    messages_raw = raw.get("messages", [])
    messages: list[Message] = []
    if isinstance(messages_raw, list):
        for item in messages_raw:
            if isinstance(item, dict):
                messages.append(
                    parse_message(item, fallback_at=datetime.fromtimestamp(0, tz=UTC))
                )
    return Conversation(
        conversation_id=str(raw.get("id", "")),
        location_id=location_id,
        contact_id=str(raw.get("contactId", "")),
        messages=tuple(messages),
    )


# ---------------------------------------------------------------- calendars


def plan_free_slots(calendar_id: str, day: date) -> PlannedRequest:
    """``GET /calendars/{id}/free-slots`` with epoch-millisecond bounds, the
    parameter shape the docs show. The response grouping (slots keyed by
    date) is the least certain shape in this module; see
    :func:`parse_free_slots`."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start.replace(hour=23, minute=59, second=59)
    return PlannedRequest(
        method="GET",
        path=f"/calendars/{calendar_id}/free-slots",
        version=VERSION_CONVERSATIONS,
        params={
            "startDate": str(int(start.timestamp() * 1000)),
            "endDate": str(int(end.timestamp() * 1000)),
        },
    )


def parse_free_slots(calendar_id: str, raw: dict[str, object]) -> tuple[CalendarSlot, ...]:
    """The docs show slots grouped under date keys, each an ISO start time.
    Slot length is the calendar's configured duration and is not in the
    response, so the slot end is left equal to the start here and the
    booking request carries the real interval. This is the one place the
    offline model is thinner than the platform; the RUNBOOK's calendar
    step verifies the real grouping before live use."""
    slots: list[CalendarSlot] = []
    for value in raw.values():
        if not isinstance(value, dict):
            continue
        listed = value.get("slots", [])
        if not isinstance(listed, list):
            continue
        for item in listed:
            if not isinstance(item, str):
                continue
            try:
                start = datetime.fromisoformat(item.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start.tzinfo is None:
                continue
            slots.append(CalendarSlot(calendar_id=calendar_id, start=start, end=start))
    return tuple(slots)


def plan_book_appointment(location_id: str, booking: BookingRequest) -> PlannedRequest:
    return PlannedRequest(
        method="POST",
        path="/calendars/events/appointments",
        version=VERSION_CONVERSATIONS,
        json_body={
            "calendarId": booking.calendar_id,
            "locationId": location_id,
            "contactId": booking.contact_id,
            "startTime": booking.start.isoformat(),
            "endTime": booking.end.isoformat(),
        },
    )


def parse_appointment(raw: dict[str, object], *, booking: BookingRequest) -> Appointment:
    return Appointment(
        appointment_id=str(raw.get("id", "")),
        calendar_id=booking.calendar_id,
        contact_id=booking.contact_id,
        start=booking.start,
        end=booking.end,
    )
