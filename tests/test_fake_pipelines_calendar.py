"""Pipelines as ordered ladders and calendars that refuse double bookings."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from tests.conftest import (
    CALENDAR_ID,
    LOCATION_ID,
    PIPELINE_ID,
    STAGE_BOOKED,
    STAGE_NEW,
    STAGE_QUALIFIED,
)

from ghl_bridge import (
    BookingRequest,
    InvalidRequest,
    NotFound,
    OpportunityCreate,
    SlotTaken,
    StageNotInPipeline,
)

THURSDAY = date(2026, 3, 5)
SLOT_9 = datetime(2026, 3, 5, 15, 0, tzinfo=UTC)
SLOT_10 = datetime(2026, 3, 5, 16, 0, tzinfo=UTC)
SLOT_11 = datetime(2026, 3, 5, 17, 0, tzinfo=UTC)


# ----------------------------------------------------------------- pipelines


def test_list_pipelines_returns_the_seeded_ladder(port) -> None:
    pipelines = port.list_pipelines(LOCATION_ID)
    assert [p.pipeline_id for p in pipelines] == [PIPELINE_ID]
    assert [s.stage_id for s in pipelines[0].stages] == [
        STAGE_NEW,
        STAGE_QUALIFIED,
        STAGE_BOOKED,
    ]


def test_create_opportunity_lands_on_the_named_stage(port, dana) -> None:
    opportunity = port.create_opportunity(
        LOCATION_ID,
        OpportunityCreate(
            pipeline_id=PIPELINE_ID,
            stage_id=STAGE_NEW,
            contact_id=dana.contact_id,
            name="Dana Whitfield",
        ),
    )
    assert opportunity.stage_id == STAGE_NEW
    assert opportunity.status == "open"


def test_create_opportunity_refuses_a_ghost_pipeline(port, dana) -> None:
    with pytest.raises(NotFound, match="pipe-ghost"):
        port.create_opportunity(
            LOCATION_ID,
            OpportunityCreate(
                pipeline_id="pipe-ghost",
                stage_id=STAGE_NEW,
                contact_id=dana.contact_id,
                name="x",
            ),
        )


def test_create_opportunity_refuses_a_stage_from_nowhere(port, dana) -> None:
    """The adversarial case from the spec: a stage id that is not one of
    this pipeline's stages is refused with both ids named, not filed
    somewhere approximate."""
    with pytest.raises(StageNotInPipeline) as excinfo:
        port.create_opportunity(
            LOCATION_ID,
            OpportunityCreate(
                pipeline_id=PIPELINE_ID,
                stage_id="stg-ghost",
                contact_id=dana.contact_id,
                name="x",
            ),
        )
    assert excinfo.value.pipeline_id == PIPELINE_ID
    assert excinfo.value.stage_id == "stg-ghost"


def test_create_opportunity_refuses_a_ghost_contact(port) -> None:
    with pytest.raises(NotFound, match="con-ghost"):
        port.create_opportunity(
            LOCATION_ID,
            OpportunityCreate(
                pipeline_id=PIPELINE_ID,
                stage_id=STAGE_NEW,
                contact_id="con-ghost",
                name="x",
            ),
        )


def opportunity_fixture(port, dana):
    return port.create_opportunity(
        LOCATION_ID,
        OpportunityCreate(
            pipeline_id=PIPELINE_ID,
            stage_id=STAGE_NEW,
            contact_id=dana.contact_id,
            name="Dana Whitfield",
        ),
    )


def test_move_opportunity_walks_the_ladder(port, dana) -> None:
    opportunity = opportunity_fixture(port, dana)
    moved = port.move_opportunity(LOCATION_ID, opportunity.opportunity_id, STAGE_QUALIFIED)
    assert moved.stage_id == STAGE_QUALIFIED
    again = port.move_opportunity(LOCATION_ID, opportunity.opportunity_id, STAGE_BOOKED)
    assert again.stage_id == STAGE_BOOKED


def test_move_to_a_nonexistent_stage_is_refused_and_nothing_moves(port, dana) -> None:
    opportunity = opportunity_fixture(port, dana)
    with pytest.raises(StageNotInPipeline):
        port.move_opportunity(LOCATION_ID, opportunity.opportunity_id, "stg-ghost")
    unchanged = port.move_opportunity(LOCATION_ID, opportunity.opportunity_id, STAGE_NEW)
    assert unchanged.stage_id == STAGE_NEW


def test_move_a_ghost_opportunity_404s(port) -> None:
    with pytest.raises(NotFound, match="opp-ghost"):
        port.move_opportunity(LOCATION_ID, "opp-ghost", STAGE_NEW)


def test_moving_back_down_the_ladder_is_allowed(port, dana) -> None:
    """Demotion is a legitimate operation; the ladder orders stages, it
    does not forbid travel in either direction."""
    opportunity = opportunity_fixture(port, dana)
    port.move_opportunity(LOCATION_ID, opportunity.opportunity_id, STAGE_BOOKED)
    demoted = port.move_opportunity(LOCATION_ID, opportunity.opportunity_id, STAGE_NEW)
    assert demoted.stage_id == STAGE_NEW


# ----------------------------------------------------------------- calendars


def test_free_slots_lists_the_unbooked_day(port) -> None:
    slots = port.free_slots(LOCATION_ID, CALENDAR_ID, THURSDAY)
    assert [s.start for s in slots] == [SLOT_9, SLOT_10, SLOT_11]


def test_free_slots_on_an_empty_day_is_empty_not_an_error(port) -> None:
    assert port.free_slots(LOCATION_ID, CALENDAR_ID, date(2026, 3, 6)) == ()


def test_free_slots_404s_on_a_ghost_calendar(port) -> None:
    with pytest.raises(NotFound, match="cal-ghost"):
        port.free_slots(LOCATION_ID, "cal-ghost", THURSDAY)


def test_booking_a_slot_removes_it_from_free(port, dana) -> None:
    port.book_appointment(
        LOCATION_ID,
        BookingRequest(
            calendar_id=CALENDAR_ID,
            contact_id=dana.contact_id,
            start=SLOT_9,
            end=SLOT_10,
        ),
    )
    remaining = port.free_slots(LOCATION_ID, CALENDAR_ID, THURSDAY)
    assert [s.start for s in remaining] == [SLOT_10, SLOT_11]


def test_double_booking_the_same_slot_is_refused(port, dana) -> None:
    booking = BookingRequest(
        calendar_id=CALENDAR_ID,
        contact_id=dana.contact_id,
        start=SLOT_9,
        end=SLOT_10,
    )
    first = port.book_appointment(LOCATION_ID, booking)
    with pytest.raises(SlotTaken) as excinfo:
        port.book_appointment(LOCATION_ID, booking)
    assert excinfo.value.calendar_id == CALENDAR_ID
    assert first.appointment_id.startswith("apt-")


def test_double_booking_by_a_different_contact_is_refused_too(port, dana) -> None:
    from ghl_bridge import ContactUpsert

    other = port.upsert_contact(LOCATION_ID, ContactUpsert(email="o@riverbend.example"))
    port.book_appointment(
        LOCATION_ID,
        BookingRequest(
            calendar_id=CALENDAR_ID, contact_id=dana.contact_id, start=SLOT_9, end=SLOT_10
        ),
    )
    with pytest.raises(SlotTaken):
        port.book_appointment(
            LOCATION_ID,
            BookingRequest(
                calendar_id=CALENDAR_ID, contact_id=other.contact_id, start=SLOT_9, end=SLOT_10
            ),
        )


def test_booking_an_unoffered_interval_is_refused(port, dana) -> None:
    """A booking must name a slot the calendar actually offers; an
    arbitrary interval, even a plausible one, is not bookable."""
    with pytest.raises(InvalidRequest, match="not one of the calendar's offered slots"):
        port.book_appointment(
            LOCATION_ID,
            BookingRequest(
                calendar_id=CALENDAR_ID,
                contact_id=dana.contact_id,
                start=SLOT_9.replace(minute=30),
                end=SLOT_10.replace(minute=30),
            ),
        )


def test_booking_404s_on_a_ghost_calendar(port, dana) -> None:
    with pytest.raises(NotFound):
        port.book_appointment(
            LOCATION_ID,
            BookingRequest(
                calendar_id="cal-ghost",
                contact_id=dana.contact_id,
                start=SLOT_9,
                end=SLOT_10,
            ),
        )


def test_booking_404s_on_a_ghost_contact(port) -> None:
    with pytest.raises(NotFound):
        port.book_appointment(
            LOCATION_ID,
            BookingRequest(
                calendar_id=CALENDAR_ID,
                contact_id="con-ghost",
                start=SLOT_9,
                end=SLOT_10,
            ),
        )


def test_two_different_slots_book_independently(port, dana) -> None:
    a = port.book_appointment(
        LOCATION_ID,
        BookingRequest(
            calendar_id=CALENDAR_ID, contact_id=dana.contact_id, start=SLOT_9, end=SLOT_10
        ),
    )
    b = port.book_appointment(
        LOCATION_ID,
        BookingRequest(
            calendar_id=CALENDAR_ID, contact_id=dana.contact_id, start=SLOT_10, end=SLOT_11
        ),
    )
    assert a.appointment_id != b.appointment_id
    assert port.free_slots(LOCATION_ID, CALENDAR_ID, THURSDAY)[0].start == SLOT_11


# ------------------------------------------------------------------ webhooks


def test_register_webhook_returns_a_registration(port) -> None:
    from ghl_bridge import WebhookSubscription

    registration = port.register_webhook(
        LOCATION_ID,
        WebhookSubscription(
            url="https://bridge.riverbend.example/hooks", events=("ContactCreate",)
        ),
    )
    assert registration.registration_id.startswith("whk-")
    assert registration.events == ("ContactCreate",)
