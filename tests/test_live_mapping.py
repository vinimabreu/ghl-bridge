"""The live adapter's pure logic, exercised offline against recorded shapes.

Nothing here talks to a network. What is being tested is everything the
adapter decides: which URL, which method, which headers, which parameters,
and how each documented status and body maps back to typed values. The one
thing these tests cannot prove, a live workspace agreeing with the
recorded shapes, is exactly what the README's honesty section and the
RUNBOOK say is pending.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ghl_bridge import (
    BookingRequest,
    ContactUpsert,
    CrossLocationDenied,
    InvalidRequest,
    NotFound,
    OpportunityCreate,
    OutboundSend,
    RateLimited,
    Unauthorized,
)
from ghl_bridge.live import LiveHighLevel
from ghl_bridge.live.mapping import (
    BASE_URL,
    VERSION_CONTACTS,
    VERSION_CONVERSATIONS,
    HttpResponse,
    PlannedRequest,
    auth_headers,
    parse_contact,
    parse_free_slots,
    parse_message,
    parse_opportunity,
    parse_pipelines,
    plan_book_appointment,
    plan_create_opportunity,
    plan_free_slots,
    plan_get_contact,
    plan_list_pipelines,
    plan_move_opportunity,
    plan_search_contacts,
    plan_send_message,
    plan_upsert_contact,
    raise_for_status,
    retry_after_from_headers,
)

LOC = "loc-live"


def ok(body: dict[str, object]) -> HttpResponse:
    return HttpResponse(status=200, headers={}, body=body)


# ------------------------------------------------------------------- headers


def test_auth_headers_carry_bearer_and_version() -> None:
    headers = auth_headers("pit-secret", VERSION_CONTACTS)
    assert headers["Authorization"] == "Bearer pit-secret"
    assert headers["Version"] == "2021-07-28"


def test_the_base_url_is_the_documented_host() -> None:
    assert BASE_URL == "https://services.leadconnectorhq.com"
    request = plan_get_contact("con-1")
    assert request.url == "https://services.leadconnectorhq.com/contacts/con-1"


# --------------------------------------------------------------------- plans


def test_upsert_plan_hits_the_documented_endpoint() -> None:
    request = plan_upsert_contact(
        LOC,
        ContactUpsert(
            first_name="Dana",
            email="dana@riverbend.example",
            phone="+15005550100",
            tags=("lead",),
            custom_fields={"car": "wagon"},
            source="form",
        ),
    )
    assert (request.method, request.path) == ("POST", "/contacts/upsert")
    assert request.version == VERSION_CONTACTS
    assert request.json_body == {
        "locationId": LOC,
        "firstName": "Dana",
        "email": "dana@riverbend.example",
        "phone": "+15005550100",
        "tags": ["lead"],
        "customFields": [{"key": "car", "value": "wagon"}],
        "source": "form",
    }


def test_upsert_plan_omits_empty_fields_rather_than_sending_blanks() -> None:
    request = plan_upsert_contact(LOC, ContactUpsert(email="a@b.example"))
    assert request.json_body == {"locationId": LOC, "email": "a@b.example"}


def test_search_plan_carries_location_and_query() -> None:
    request = plan_search_contacts(LOC, query="dana@riverbend.example")
    assert (request.method, request.path) == ("GET", "/contacts/")
    assert request.params == {"locationId": LOC, "query": "dana@riverbend.example"}


def test_pipeline_list_plan() -> None:
    request = plan_list_pipelines(LOC)
    assert request.path == "/opportunities/pipelines"
    assert request.params == {"locationId": LOC}


def test_create_opportunity_plan_uses_the_documented_field_names() -> None:
    request = plan_create_opportunity(
        LOC,
        OpportunityCreate(
            pipeline_id="pipe-1", stage_id="stg-1", contact_id="con-1", name="Dana"
        ),
    )
    body = request.json_body
    assert body is not None
    assert body["pipelineId"] == "pipe-1"
    assert body["pipelineStageId"] == "stg-1"
    assert body["contactId"] == "con-1"


def test_move_opportunity_plan_is_a_put_on_the_resource() -> None:
    request = plan_move_opportunity("opp-9", "stg-2")
    assert (request.method, request.path) == ("PUT", "/opportunities/opp-9")
    assert request.json_body == {"pipelineStageId": "stg-2"}


def test_send_message_plan_names_the_channel_as_type() -> None:
    request = plan_send_message(OutboundSend(contact_id="con-1", body="hello"))
    assert (request.method, request.path) == ("POST", "/conversations/messages")
    assert request.version == VERSION_CONVERSATIONS
    assert request.json_body == {"type": "SMS", "contactId": "con-1", "message": "hello"}


def test_free_slots_plan_bounds_the_day_in_epoch_millis() -> None:
    request = plan_free_slots("cal-1", date(2026, 3, 5))
    assert request.path == "/calendars/cal-1/free-slots"
    start = int(request.params["startDate"])
    end = int(request.params["endDate"])
    assert end > start
    assert (end - start) == (24 * 3600 - 1) * 1000


def test_book_appointment_plan_carries_iso_times() -> None:
    booking = BookingRequest(
        calendar_id="cal-1",
        contact_id="con-1",
        start=datetime(2026, 3, 5, 15, 0, tzinfo=UTC),
        end=datetime(2026, 3, 5, 16, 0, tzinfo=UTC),
    )
    request = plan_book_appointment(LOC, booking)
    body = request.json_body
    assert body is not None
    assert body["startTime"] == "2026-03-05T15:00:00+00:00"
    assert body["calendarId"] == "cal-1"


# -------------------------------------------------------------------- parses


def test_parse_contact_maps_the_documented_camel_case() -> None:
    contact = parse_contact(
        LOC,
        {
            "id": "con-77",
            "firstName": "Dana",
            "lastName": "Whitfield",
            "email": "dana@riverbend.example",
            "phone": "+15005550100",
            "tags": ["lead"],
            "customFields": [{"key": "car", "value": "wagon"}],
            "dnd": True,
        },
    )
    assert contact.contact_id == "con-77"
    assert contact.first_name == "Dana"
    assert contact.opted_out is True
    assert contact.custom_fields == {"car": "wagon"}


def test_parse_contact_tolerates_nulls() -> None:
    contact = parse_contact(LOC, {"id": "con-1", "firstName": None, "email": None})
    assert contact.first_name == ""
    assert contact.email is None


def test_parse_pipelines_orders_stages_by_position() -> None:
    pipelines = parse_pipelines(
        {
            "pipelines": [
                {
                    "id": "pipe-1",
                    "name": "Sales",
                    "stages": [
                        {"id": "s1", "name": "New", "position": 1},
                        {"id": "s2", "name": "Won", "position": 2},
                    ],
                }
            ]
        }
    )
    assert [s.stage_id for s in pipelines[0].stages] == ["s1", "s2"]


def test_parse_pipelines_of_nothing_is_empty() -> None:
    assert parse_pipelines({}) == ()


def test_parse_opportunity_reads_the_stage_field() -> None:
    opportunity = parse_opportunity(
        LOC,
        {
            "id": "opp-1",
            "pipelineId": "pipe-1",
            "pipelineStageId": "stg-2",
            "contactId": "con-1",
            "name": "Dana",
            "monetaryValue": 250,
            "status": "open",
        },
    )
    assert opportunity.stage_id == "stg-2"
    assert opportunity.monetary_value == 250.0


def test_parse_opportunity_defaults_an_alien_status_to_open() -> None:
    opportunity = parse_opportunity(LOC, {"id": "o", "status": "vaporized"})
    assert opportunity.status == "open"


def test_parse_message_reads_direction_and_dateadded() -> None:
    fallback = datetime(2026, 3, 3, 20, 0, tzinfo=UTC)
    message = parse_message(
        {
            "id": "msg-1",
            "conversationId": "cnv-1",
            "direction": "inbound",
            "body": "hi",
            "dateAdded": "2026-03-03T19:59:00Z",
        },
        fallback_at=fallback,
    )
    assert message.direction.value == "inbound"
    assert message.at.isoformat() == "2026-03-03T19:59:00+00:00"


def test_parse_message_falls_back_to_the_clock_not_an_invention() -> None:
    fallback = datetime(2026, 3, 3, 20, 0, tzinfo=UTC)
    message = parse_message({"id": "m", "body": "x"}, fallback_at=fallback)
    assert message.at == fallback


def test_parse_free_slots_reads_the_date_grouped_shape() -> None:
    slots = parse_free_slots(
        "cal-1",
        {
            "2026-03-05": {
                "slots": ["2026-03-05T15:00:00Z", "2026-03-05T16:00:00Z"]
            }
        },
    )
    assert len(slots) == 2
    assert slots[0].start.isoformat() == "2026-03-05T15:00:00+00:00"


def test_parse_free_slots_skips_garbage_rather_than_crashing() -> None:
    slots = parse_free_slots(
        "cal-1",
        {"traceId": "abc", "2026-03-05": {"slots": ["not-a-time", 42]}},
    )
    assert slots == ()


# ------------------------------------------------------------- status errors


def test_2xx_raises_nothing() -> None:
    raise_for_status(ok({}), token_location=LOC, requested_location=LOC)


def test_401_maps_to_unauthorized() -> None:
    with pytest.raises(Unauthorized):
        raise_for_status(
            HttpResponse(status=401, headers={}, body={"message": "bad token"}),
            token_location=LOC,
            requested_location=LOC,
        )


def test_403_maps_to_cross_location_with_both_sides_named() -> None:
    with pytest.raises(CrossLocationDenied) as excinfo:
        raise_for_status(
            HttpResponse(status=403, headers={}, body={}),
            token_location=LOC,
            requested_location="loc-other",
        )
    assert excinfo.value.requested_location == "loc-other"


def test_404_maps_to_not_found() -> None:
    with pytest.raises(NotFound):
        raise_for_status(
            HttpResponse(status=404, headers={}, body={}),
            token_location=LOC,
            requested_location=LOC,
        )


def test_422_maps_to_invalid_request_with_the_platform_message() -> None:
    with pytest.raises(InvalidRequest, match="email is invalid"):
        raise_for_status(
            HttpResponse(status=422, headers={}, body={"message": "email is invalid"}),
            token_location=LOC,
            requested_location=LOC,
        )


def test_429_maps_to_rate_limited_with_the_headers_kept() -> None:
    with pytest.raises(RateLimited) as excinfo:
        raise_for_status(
            HttpResponse(
                status=429,
                headers={"Retry-After": "7", "X-RateLimit-Remaining": "0"},
                body={},
            ),
            token_location=LOC,
            requested_location=LOC,
        )
    assert excinfo.value.retry_after_seconds == 7.0
    assert excinfo.value.headers["X-RateLimit-Remaining"] == "0"


def test_retry_after_prefers_the_explicit_header() -> None:
    assert retry_after_from_headers({"Retry-After": "3"}) == 3.0


def test_retry_after_falls_back_to_the_interval_header() -> None:
    assert retry_after_from_headers({"X-RateLimit-Interval-Milliseconds": "10000"}) == 10.0


def test_retry_after_defaults_to_the_full_window() -> None:
    assert retry_after_from_headers({}) == 10.0


def test_retry_after_survives_garbage_headers() -> None:
    assert retry_after_from_headers({"Retry-After": "soon"}) == 10.0


# ------------------------------------------------------------------- adapter


class ScriptedTransport:
    """Returns canned responses and records every planned request."""

    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.sent: list[PlannedRequest] = []

    def send(self, request: PlannedRequest) -> HttpResponse:
        self.sent.append(request)
        return self.responses.pop(0)


def test_the_adapter_sends_the_plan_and_parses_the_answer() -> None:
    transport = ScriptedTransport(
        [ok({"contact": {"id": "con-9", "firstName": "Dana"}})]
    )
    adapter = LiveHighLevel(location_id=LOC, transport=transport)
    contact = adapter.upsert_contact(LOC, ContactUpsert(email="d@r.example"))
    assert contact.contact_id == "con-9"
    assert transport.sent[0].path == "/contacts/upsert"


def test_the_adapter_surfaces_typed_errors(clock) -> None:
    transport = ScriptedTransport(
        [HttpResponse(status=429, headers={"Retry-After": "5"}, body={})]
    )
    adapter = LiveHighLevel(location_id=LOC, transport=transport, clock=clock)
    with pytest.raises(RateLimited):
        adapter.list_pipelines(LOC)


def test_the_adapter_unwraps_search_results() -> None:
    transport = ScriptedTransport(
        [ok({"contacts": [{"id": "con-1", "email": "a@b.example"}]})]
    )
    adapter = LiveHighLevel(location_id=LOC, transport=transport)
    hits = adapter.search_contacts_by_email(LOC, "a@b.example")
    assert [c.contact_id for c in hits] == ["con-1"]


def test_send_message_falls_back_to_the_injected_clock(clock) -> None:
    transport = ScriptedTransport([ok({"id": "msg-1", "conversationId": "cnv-1"})])
    adapter = LiveHighLevel(location_id=LOC, transport=transport, clock=clock)
    message = adapter.send_message(LOC, OutboundSend(contact_id="con-1", body="x"))
    assert message.at == clock()


def test_register_webhook_is_refused_honestly() -> None:
    adapter = LiveHighLevel(location_id=LOC, transport=ScriptedTransport([]))
    from ghl_bridge import WebhookSubscription

    with pytest.raises(NotImplementedError, match="RUNBOOK"):
        adapter.register_webhook(
            LOC, WebhookSubscription(url="https://x.example", events=())
        )


def test_the_requests_transport_names_the_extra_when_absent() -> None:
    """This suite's environment deliberately lacks the [live] extra, which
    makes the graceful failure itself testable."""
    from ghl_bridge.live import RequestsTransport

    with pytest.raises(ImportError, match="ghl-bridge\\[live\\]"):
        RequestsTransport(token="pit-x")


def test_the_paced_port_wraps_the_live_adapter_too(clock) -> None:
    """The same discipline composes over either implementation of the port;
    nothing in the pacer knows which one it is wrapping."""
    from ghl_bridge import AuditLedger, LocationPacer, PacedCaller, PacedPort

    transport = ScriptedTransport([ok({"pipelines": []}), ok({"pipelines": []})])
    adapter = LiveHighLevel(location_id=LOC, transport=transport, clock=clock)
    paced = PacedPort(
        inner=adapter,
        caller=PacedCaller(
            pacer=LocationPacer(clock=clock, burst_limit=1),
            sleeper=lambda s: clock.advance(s),
            ledger=AuditLedger(),
            clock=clock,
        ),
    )
    paced.list_pipelines(LOC)
    paced.list_pipelines(LOC)
    assert len(transport.sent) == 2
