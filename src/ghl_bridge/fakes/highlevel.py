"""A deterministic HighLevel workspace, built from the documented semantics.

This is the honest core of the repository. There is no live workspace
behind it; there is the public API 2.0 documentation, read carefully, and
this module modelling what it promises: location-scoped tokens, contact
upsert that dedupes inside the location, pipelines whose stages are an
ordered ladder, conversations with inbound and outbound messages,
calendars that refuse a double booking, and the published rate limits
answered as a 429 with the platform's own headers.

It ships in the package rather than in the test tree because it is what
lets the demo, the suite and any integrator run the full bridge with no
account, no card and no key, and then swap in
:class:`ghl_bridge.live.LiveHighLevel` without touching the calling code.

What it deliberately does not claim: exact wire field names. Where the
docs were unambiguous the names match; where they were not, the invariant
is modelled and the README's honesty section plus the RUNBOOK carry the
live verification step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..clock import Clock, require_aware
from ..identity import NormalisedPhone, normalise_email, normalise_phone
from ..limits import BURST_LIMIT, BURST_WINDOW_SECONDS, DAILY_LIMIT
from ..models import (
    Appointment,
    BookingRequest,
    CalendarSlot,
    Contact,
    ContactUpsert,
    Conversation,
    Location,
    Message,
    MessageDirection,
    Opportunity,
    OpportunityCreate,
    OutboundSend,
    Pipeline,
    WebhookRegistration,
    WebhookSubscription,
)
from ..ports import (
    CrossLocationDenied,
    InvalidRequest,
    NotFound,
    RateLimited,
    SlotTaken,
    StageNotInPipeline,
    Unauthorized,
)


@dataclass
class _RateState:
    window_start: datetime | None = None
    window_count: int = 0
    day: date | None = None
    day_count: int = 0


@dataclass
class _LocationState:
    location: Location
    contacts: dict[str, Contact] = field(default_factory=dict)
    email_index: dict[str, str] = field(default_factory=dict)
    phone_index: dict[str, str] = field(default_factory=dict)
    pipelines: dict[str, Pipeline] = field(default_factory=dict)
    opportunities: dict[str, Opportunity] = field(default_factory=dict)
    conversations: dict[str, Conversation] = field(default_factory=dict)
    conversation_by_contact: dict[str, str] = field(default_factory=dict)
    calendar_slots: dict[str, list[CalendarSlot]] = field(default_factory=dict)
    appointments: dict[str, list[Appointment]] = field(default_factory=dict)
    registrations: dict[str, WebhookRegistration] = field(default_factory=dict)
    rate: _RateState = field(default_factory=_RateState)


class FakeHighLevel:
    """The workspace. Seed it, issue a token, and talk through the port.

    Rate limits are constructor arguments so a test can set the burst to 5
    and watch the 429 arrive on call 6 instead of driving 101 calls; the
    defaults are the published numbers.
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
        self._burst_window = burst_window_seconds
        self._daily_limit = daily_limit
        self._locations: dict[str, _LocationState] = {}
        self._tokens: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    # ------------------------------------------------------------------ seeding

    def add_location(self, location: Location) -> None:
        if location.location_id in self._locations:
            raise ValueError(f"location {location.location_id!r} already exists")
        self._locations[location.location_id] = _LocationState(location=location)

    def issue_private_token(self, location_id: str) -> str:
        """A Private Integration token, scoped to exactly one sub-account,
        which is the scoping the platform documents for this token type."""
        self._require_location(location_id)
        token = f"pit-{self._next('pit'):04d}"
        self._tokens[token] = location_id
        return token

    def add_pipeline(self, location_id: str, pipeline: Pipeline) -> None:
        state = self._require_location(location_id)
        state.pipelines[pipeline.pipeline_id] = pipeline

    def add_calendar_slots(
        self, location_id: str, calendar_id: str, slots: list[CalendarSlot]
    ) -> None:
        state = self._require_location(location_id)
        state.calendar_slots.setdefault(calendar_id, []).extend(slots)
        state.appointments.setdefault(calendar_id, [])

    def seed_contact(self, location_id: str, contact: ContactUpsert) -> Contact:
        """Server-side seeding: creates directly, no token and no quota.
        The scenario builder for demos and tests."""
        state = self._require_location(location_id)
        return self._store_contact(state, contact)

    def seed_inbound(
        self, location_id: str, contact_id: str, body: str, at: datetime
    ) -> Message:
        """A customer texting in. Not an API call by the integration, so it
        consumes no quota; it lands in the conversation the way the
        platform's own channels do."""
        state = self._require_location(location_id)
        if contact_id not in state.contacts:
            raise NotFound(kind="contact", resource_id=contact_id)
        return self._append_message(
            state,
            contact_id,
            body=body,
            direction=MessageDirection.INBOUND,
            at=require_aware(at, field="at"),
        )

    def set_opted_out(self, location_id: str, contact_id: str, opted_out: bool) -> Contact:
        state = self._require_location(location_id)
        if contact_id not in state.contacts:
            raise NotFound(kind="contact", resource_id=contact_id)
        updated = state.contacts[contact_id].model_copy(update={"opted_out": opted_out})
        state.contacts[contact_id] = updated
        return updated

    # ------------------------------------------------------------------ access

    def port_for(self, token: str) -> _ScopedPort:
        """The port a caller holding ``token`` gets: every operation checked
        against the token's location first, charged against the location's
        quota second, executed third."""
        if token not in self._tokens:
            raise Unauthorized(detail="unknown or revoked token")
        return _ScopedPort(server=self, token_location=self._tokens[token])

    # ------------------------------------------------------------------ internals

    def _next(self, kind: str) -> int:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return self._counters[kind]

    def _require_location(self, location_id: str) -> _LocationState:
        if location_id not in self._locations:
            raise NotFound(kind="location", resource_id=location_id)
        return self._locations[location_id]

    def _charge(self, state: _LocationState) -> None:
        """The published limits, enforced the way the platform describes:
        a burst window per location and a daily ceiling per location, both
        answered as 429 with the numbers in the headers and the honest
        retry hint in ``retry_after_seconds``."""
        now = self._clock()
        rate = state.rate

        today = now.astimezone(UTC).date()
        if rate.day != today:
            rate.day = today
            rate.day_count = 0
        if rate.day_count >= self._daily_limit:
            midnight = datetime.combine(
                today + timedelta(days=1), datetime.min.time(), tzinfo=UTC
            )
            raise RateLimited(
                retry_after_seconds=(midnight - now).total_seconds(),
                headers=self._headers(rate, burst_remaining=self._burst_remaining(rate, now)),
            )

        if (
            rate.window_start is None
            or (now - rate.window_start).total_seconds() >= self._burst_window
        ):
            rate.window_start = now
            rate.window_count = 0
        if rate.window_count >= self._burst_limit:
            reset_at = rate.window_start + timedelta(seconds=self._burst_window)
            raise RateLimited(
                retry_after_seconds=(reset_at - now).total_seconds(),
                headers=self._headers(rate, burst_remaining=0),
            )

        rate.window_count += 1
        rate.day_count += 1

    def _burst_remaining(self, rate: _RateState, now: datetime) -> int:
        if (
            rate.window_start is None
            or (now - rate.window_start).total_seconds() >= self._burst_window
        ):
            return self._burst_limit
        return max(0, self._burst_limit - rate.window_count)

    def _headers(self, rate: _RateState, *, burst_remaining: int) -> dict[str, str]:
        return {
            "X-RateLimit-Max": str(self._burst_limit),
            "X-RateLimit-Remaining": str(burst_remaining),
            "X-RateLimit-Interval-Milliseconds": str(int(self._burst_window * 1000)),
            "X-RateLimit-Limit-Daily": str(self._daily_limit),
            "X-RateLimit-Daily-Remaining": str(max(0, self._daily_limit - rate.day_count)),
        }

    def _email_key(self, raw: str) -> str:
        return normalise_email(raw)

    def _phone_key(self, state: _LocationState, raw: str) -> str | None:
        """The E.164 dedupe key, or None when the number does not determine
        one. An unresolvable phone is stored as a field but never indexed:
        indexing the raw string would merge two strangers who both typed
        "N/A" into the form, and trimming it with ``str.strip`` would break
        the ASCII-only doctrine everything else keys on. No key, no match,
        no merge."""
        result = normalise_phone(raw, default_region=state.location.default_region)
        if isinstance(result, NormalisedPhone):
            return result.e164
        return None

    def _store_contact(self, state: _LocationState, upsert: ContactUpsert) -> Contact:
        """The documented upsert semantics: within the location, an existing
        contact matching on email or phone is updated in place; otherwise a
        contact is created. Matching never crosses locations, because
        nothing here can see another location at all."""
        email_key = self._email_key(upsert.email) if upsert.email else None
        phone_key = self._phone_key(state, upsert.phone) if upsert.phone else None

        existing_id: str | None = None
        if email_key and email_key in state.email_index:
            existing_id = state.email_index[email_key]
        elif phone_key and phone_key in state.phone_index:
            existing_id = state.phone_index[phone_key]

        if existing_id is not None:
            current = state.contacts[existing_id]
            merged = current.model_copy(
                update={
                    "first_name": upsert.first_name or current.first_name,
                    "last_name": upsert.last_name or current.last_name,
                    "email": upsert.email or current.email,
                    "phone": upsert.phone or current.phone,
                    "tags": tuple(dict.fromkeys(current.tags + upsert.tags)),
                    "custom_fields": {**current.custom_fields, **upsert.custom_fields},
                    "source": current.source or upsert.source,
                }
            )
            state.contacts[existing_id] = merged
            self._index_contact(state, merged)
            return merged

        contact = Contact(
            contact_id=f"con-{self._next('con'):04d}",
            location_id=state.location.location_id,
            first_name=upsert.first_name,
            last_name=upsert.last_name,
            email=upsert.email,
            phone=upsert.phone,
            tags=upsert.tags,
            custom_fields=dict(upsert.custom_fields),
            source=upsert.source,
        )
        state.contacts[contact.contact_id] = contact
        self._index_contact(state, contact)
        return contact

    def _index_contact(self, state: _LocationState, contact: Contact) -> None:
        if contact.email:
            state.email_index[self._email_key(contact.email)] = contact.contact_id
        if contact.phone:
            phone_key = self._phone_key(state, contact.phone)
            if phone_key is not None:
                state.phone_index[phone_key] = contact.contact_id

    def _append_message(
        self,
        state: _LocationState,
        contact_id: str,
        *,
        body: str,
        direction: MessageDirection,
        at: datetime,
    ) -> Message:
        conversation_id = state.conversation_by_contact.get(contact_id)
        if conversation_id is None:
            conversation_id = f"cnv-{self._next('cnv'):04d}"
            state.conversation_by_contact[contact_id] = conversation_id
            state.conversations[conversation_id] = Conversation(
                conversation_id=conversation_id,
                location_id=state.location.location_id,
                contact_id=contact_id,
            )
        message = Message(
            message_id=f"msg-{self._next('msg'):04d}",
            conversation_id=conversation_id,
            direction=direction,
            body=body,
            at=at,
        )
        conversation = state.conversations[conversation_id]
        state.conversations[conversation_id] = conversation.model_copy(
            update={"messages": conversation.messages + (message,)}
        )
        return message


class _ScopedPort:
    """What a token actually reaches: one location, quota charged per call.

    Scope is checked before quota on purpose. A caller probing another
    location learns it is denied, not rate limited, and the denial does not
    burn the legitimate traffic's window.
    """

    def __init__(self, *, server: FakeHighLevel, token_location: str) -> None:
        self._server = server
        self._location = token_location

    def _state(self, location_id: str) -> _LocationState:
        if location_id != self._location:
            raise CrossLocationDenied(
                token_location=self._location, requested_location=location_id
            )
        state = self._server._require_location(location_id)
        self._server._charge(state)
        return state

    def upsert_contact(self, location_id: str, contact: ContactUpsert) -> Contact:
        state = self._state(location_id)
        if not contact.email and not contact.phone:
            raise InvalidRequest(detail="an upsert needs an email or a phone to match on")
        return self._server._store_contact(state, contact)

    def get_contact(self, location_id: str, contact_id: str) -> Contact:
        state = self._state(location_id)
        if contact_id not in state.contacts:
            raise NotFound(kind="contact", resource_id=contact_id)
        return state.contacts[contact_id]

    def search_contacts_by_email(self, location_id: str, email: str) -> tuple[Contact, ...]:
        state = self._state(location_id)
        key = self._server._email_key(email)
        contact_id = state.email_index.get(key)
        if contact_id is None:
            return ()
        return (state.contacts[contact_id],)

    def search_contacts_by_phone(self, location_id: str, phone: str) -> tuple[Contact, ...]:
        state = self._state(location_id)
        key = self._server._phone_key(state, phone)
        if key is None:
            return ()
        contact_id = state.phone_index.get(key)
        if contact_id is None:
            return ()
        return (state.contacts[contact_id],)

    def list_pipelines(self, location_id: str) -> tuple[Pipeline, ...]:
        state = self._state(location_id)
        return tuple(state.pipelines.values())

    def create_opportunity(
        self, location_id: str, create: OpportunityCreate
    ) -> Opportunity:
        state = self._state(location_id)
        pipeline = state.pipelines.get(create.pipeline_id)
        if pipeline is None:
            raise NotFound(kind="pipeline", resource_id=create.pipeline_id)
        if pipeline.stage(create.stage_id) is None:
            raise StageNotInPipeline(
                pipeline_id=create.pipeline_id, stage_id=create.stage_id
            )
        if create.contact_id not in state.contacts:
            raise NotFound(kind="contact", resource_id=create.contact_id)
        opportunity = Opportunity(
            opportunity_id=f"opp-{self._server._next('opp'):04d}",
            location_id=location_id,
            pipeline_id=create.pipeline_id,
            stage_id=create.stage_id,
            contact_id=create.contact_id,
            name=create.name,
            monetary_value=create.monetary_value,
        )
        state.opportunities[opportunity.opportunity_id] = opportunity
        return opportunity

    def move_opportunity(
        self, location_id: str, opportunity_id: str, stage_id: str
    ) -> Opportunity:
        state = self._state(location_id)
        opportunity = state.opportunities.get(opportunity_id)
        if opportunity is None:
            raise NotFound(kind="opportunity", resource_id=opportunity_id)
        pipeline = state.pipelines[opportunity.pipeline_id]
        if pipeline.stage(stage_id) is None:
            raise StageNotInPipeline(
                pipeline_id=pipeline.pipeline_id, stage_id=stage_id
            )
        moved = opportunity.model_copy(update={"stage_id": stage_id})
        state.opportunities[opportunity_id] = moved
        return moved

    def get_conversation(self, location_id: str, conversation_id: str) -> Conversation:
        state = self._state(location_id)
        if conversation_id not in state.conversations:
            raise NotFound(kind="conversation", resource_id=conversation_id)
        return state.conversations[conversation_id]

    def send_message(self, location_id: str, send: OutboundSend) -> Message:
        state = self._state(location_id)
        contact = state.contacts.get(send.contact_id)
        if contact is None:
            raise NotFound(kind="contact", resource_id=send.contact_id)
        if contact.opted_out:
            raise InvalidRequest(
                detail=f"contact {send.contact_id!r} has opted out (DND); "
                "the platform refuses the send and so does this fake"
            )
        return self._server._append_message(
            state,
            send.contact_id,
            body=send.body,
            direction=MessageDirection.OUTBOUND,
            at=self._server._clock(),
        )

    def free_slots(
        self, location_id: str, calendar_id: str, day: date
    ) -> tuple[CalendarSlot, ...]:
        state = self._state(location_id)
        if calendar_id not in state.calendar_slots:
            raise NotFound(kind="calendar", resource_id=calendar_id)
        booked = {a.start for a in state.appointments.get(calendar_id, [])}
        return tuple(
            slot
            for slot in state.calendar_slots[calendar_id]
            if slot.start.astimezone(UTC).date() == day and slot.start not in booked
        )

    def book_appointment(self, location_id: str, booking: BookingRequest) -> Appointment:
        state = self._state(location_id)
        if booking.calendar_id not in state.calendar_slots:
            raise NotFound(kind="calendar", resource_id=booking.calendar_id)
        if booking.contact_id not in state.contacts:
            raise NotFound(kind="contact", resource_id=booking.contact_id)
        offered = {
            (slot.start, slot.end) for slot in state.calendar_slots[booking.calendar_id]
        }
        if (booking.start, booking.end) not in offered:
            raise InvalidRequest(
                detail="that interval is not one of the calendar's offered slots"
            )
        for existing in state.appointments[booking.calendar_id]:
            if existing.start == booking.start:
                raise SlotTaken(
                    calendar_id=booking.calendar_id, start=booking.start.isoformat()
                )
        appointment = Appointment(
            appointment_id=f"apt-{self._server._next('apt'):04d}",
            calendar_id=booking.calendar_id,
            contact_id=booking.contact_id,
            start=booking.start,
            end=booking.end,
        )
        state.appointments[booking.calendar_id].append(appointment)
        return appointment

    def register_webhook(
        self, location_id: str, subscription: WebhookSubscription
    ) -> WebhookRegistration:
        state = self._state(location_id)
        registration = WebhookRegistration(
            registration_id=f"whk-{self._server._next('whk'):04d}",
            location_id=location_id,
            url=subscription.url,
            events=subscription.events,
        )
        state.registrations[registration.registration_id] = registration
        return registration
