"""Construction-time refusals and the small behaviours on the models."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ghl_bridge import (
    Approval,
    BusinessHours,
    CalendarSlot,
    Contact,
    Location,
    Message,
    MessageDirection,
    Pipeline,
    PipelineStage,
    WebhookEvent,
)

HOURS = BusinessHours(days=frozenset({0, 1, 2, 3, 4}), open_time=time(9), close_time=time(18))


def chicago(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=ZoneInfo("America/Chicago"))


# ------------------------------------------------------------ business hours


def test_a_weekday_afternoon_is_inside() -> None:
    assert HOURS.contains(chicago(3, 14)) is True


def test_saturday_is_outside() -> None:
    assert HOURS.contains(chicago(7, 14)) is False


def test_sunday_is_outside() -> None:
    assert HOURS.contains(chicago(8, 14)) is False


def test_the_opening_instant_is_inside() -> None:
    assert HOURS.contains(chicago(3, 9, 0)) is True


def test_one_minute_before_opening_is_outside() -> None:
    assert HOURS.contains(chicago(3, 8, 59)) is False


def test_the_closing_instant_is_outside() -> None:
    """Open is inclusive, close is exclusive: at 18:00:00 the window is
    shut. The boundary is pinned here so nobody relitigates it by reading
    the comparison operator."""
    assert HOURS.contains(chicago(3, 18, 0)) is False


def test_one_minute_before_closing_is_inside() -> None:
    assert HOURS.contains(chicago(3, 17, 59)) is True


def test_weekdays_outside_0_to_6_are_refused() -> None:
    with pytest.raises(ValidationError, match="0 \\(Monday\\) to 6"):
        BusinessHours(days=frozenset({7}), open_time=time(9), close_time=time(18))


def test_open_at_or_after_close_is_refused() -> None:
    with pytest.raises(ValidationError, match="before close_time"):
        BusinessHours(days=frozenset({0}), open_time=time(18), close_time=time(9))


def test_equal_open_and_close_is_refused() -> None:
    with pytest.raises(ValidationError, match="before close_time"):
        BusinessHours(days=frozenset({0}), open_time=time(9), close_time=time(9))


# ------------------------------------------------------------------ location


def make_location(tz: str = "America/Chicago") -> Location:
    return Location(
        location_id="loc-a", name="A", timezone=tz, business_hours=HOURS
    )


def test_a_real_iana_zone_is_accepted() -> None:
    assert make_location("America/Sao_Paulo").timezone == "America/Sao_Paulo"


def test_a_fake_zone_is_refused_at_construction() -> None:
    with pytest.raises(ValidationError, match="IANA"):
        make_location("Mars/Olympus_Mons")


def test_a_fixed_offset_string_is_refused() -> None:
    with pytest.raises(ValidationError, match="IANA"):
        make_location("UTC-6")


def test_local_now_converts_into_the_location_zone() -> None:
    loc = make_location()
    utc_now = datetime(2026, 3, 3, 20, 0, tzinfo=UTC)
    local = loc.local_now(utc_now)
    assert (local.hour, local.minute) == (14, 0)


def test_local_now_refuses_a_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_location().local_now(datetime(2026, 3, 3, 20, 0))


def test_local_now_handles_the_dst_spring_forward() -> None:
    """2026-03-08 02:30 does not exist in Chicago; 07:30 UTC that morning
    lands at 01:30 CST and 08:30 UTC lands at 03:30 CDT. The hour between
    them vanished, and the conversion has to agree."""
    loc = make_location()
    before = loc.local_now(datetime(2026, 3, 8, 7, 30, tzinfo=UTC))
    after = loc.local_now(datetime(2026, 3, 8, 8, 30, tzinfo=UTC))
    assert before.hour == 1
    assert after.hour == 3


# ----------------------------------------------------------------- pipelines


def test_stages_must_be_strictly_increasing() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        Pipeline(
            pipeline_id="p",
            name="P",
            stages=(
                PipelineStage(stage_id="b", name="B", position=2),
                PipelineStage(stage_id="a", name="A", position=1),
            ),
        )


def test_duplicate_positions_are_refused() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        Pipeline(
            pipeline_id="p",
            name="P",
            stages=(
                PipelineStage(stage_id="a", name="A", position=1),
                PipelineStage(stage_id="b", name="B", position=1),
            ),
        )


def test_stage_lookup_finds_by_id() -> None:
    pipeline = Pipeline(
        pipeline_id="p",
        name="P",
        stages=(PipelineStage(stage_id="a", name="A", position=1),),
    )
    stage = pipeline.stage("a")
    assert stage is not None
    assert stage.name == "A"


def test_stage_lookup_returns_none_for_a_stranger() -> None:
    pipeline = Pipeline(pipeline_id="p", name="P", stages=())
    assert pipeline.stage("ghost") is None


# ------------------------------------------------------------------ datetimes


def test_message_refuses_a_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Message(
            message_id="m",
            conversation_id="c",
            direction=MessageDirection.OUTBOUND,
            body="x",
            at=datetime(2026, 3, 3, 9, 0),
        )


def test_calendar_slot_refuses_a_naive_start() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        CalendarSlot(
            calendar_id="c",
            start=datetime(2026, 3, 5, 9, 0),
            end=datetime(2026, 3, 5, 10, 0, tzinfo=UTC),
        )


def test_webhook_event_refuses_a_naive_occurred_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        WebhookEvent(
            event_type="ContactCreate",
            location_id="loc",
            occurred_at=datetime(2026, 3, 3, 9, 0),
        )


def test_approval_refuses_a_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Approval(
            decision_id="d",
            mode="auto",
            approved_by="gate",
            content_sha256="00",
            at=datetime(2026, 3, 3, 9, 0),
        )


# -------------------------------------------------------------------- frozen


def test_contacts_are_frozen() -> None:
    contact = Contact(contact_id="c", location_id="loc")
    with pytest.raises(ValidationError):
        contact.first_name = "changed"  # type: ignore[misc]


def test_messages_are_frozen() -> None:
    message = Message(
        message_id="m",
        conversation_id="c",
        direction=MessageDirection.INBOUND,
        body="x",
        at=datetime(2026, 3, 3, 9, 0, tzinfo=UTC),
    )
    with pytest.raises(ValidationError):
        message.body = "edited"  # type: ignore[misc]


def test_approvals_are_frozen() -> None:
    approval = Approval(
        decision_id="d",
        mode="human",
        approved_by="sam",
        content_sha256="00",
        at=datetime(2026, 3, 3, 9, 0, tzinfo=UTC),
    )
    with pytest.raises(ValidationError):
        approval.approved_by = "someone else"  # type: ignore[misc]
