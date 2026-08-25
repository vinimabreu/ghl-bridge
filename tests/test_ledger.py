"""Append-only, ordered, and able to answer the operator's question."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError
from tests.conftest import T0

from ghl_bridge import AuditLedger


def test_records_are_numbered_from_one(clock) -> None:
    ledger = AuditLedger()
    a = ledger.record(at=clock(), kind="x", detail={})
    b = ledger.record(at=clock(), kind="y", detail={})
    assert (a.seq, b.seq) == (1, 2)


def test_records_come_back_in_order(clock) -> None:
    ledger = AuditLedger()
    for kind in ("a", "b", "c"):
        ledger.record(at=clock(), kind=kind, detail={})
    assert [r.kind for r in ledger.records()] == ["a", "b", "c"]


def test_the_read_surface_is_a_tuple_not_the_list(clock) -> None:
    ledger = AuditLedger()
    ledger.record(at=clock(), kind="a", detail={})
    view = ledger.records()
    assert isinstance(view, tuple)


def test_records_are_frozen(clock) -> None:
    ledger = AuditLedger()
    record = ledger.record(at=clock(), kind="a", detail={"k": "v"})
    with pytest.raises(ValidationError):
        record.kind = "b"  # type: ignore[misc]


def test_the_detail_is_copied_at_write_time(clock) -> None:
    """Mutating the caller's dict after recording must not rewrite history."""
    ledger = AuditLedger()
    detail: dict[str, object] = {"state": "before"}
    record = ledger.record(at=clock(), kind="a", detail=detail)
    detail["state"] = "after"
    assert record.detail["state"] == "before"


def test_of_kind_filters(clock) -> None:
    ledger = AuditLedger()
    ledger.record(at=clock(), kind="a", detail={})
    ledger.record(at=clock(), kind="b", detail={})
    ledger.record(at=clock(), kind="a", detail={})
    assert len(ledger.of_kind("a")) == 2
    assert len(ledger.of_kind("ghost")) == 0


def test_timestamps_must_be_aware() -> None:
    ledger = AuditLedger()
    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.record(at=datetime(2026, 3, 3, 9, 0), kind="a", detail={})


def test_timestamps_are_the_clock_reading_given(clock) -> None:
    ledger = AuditLedger()
    clock.advance(120)
    record = ledger.record(at=clock(), kind="a", detail={})
    assert record.at == clock()
    assert record.at != T0


def test_explain_message_walks_the_event_chain(clock) -> None:
    ledger = AuditLedger()
    key = "id:evt-1"
    ledger.record(at=clock(), kind="webhook_received", detail={"event_key": key})
    ledger.record(at=clock(), kind="draft_generated", detail={"event_key": key})
    ledger.record(at=clock(), kind="gate_decision", detail={"event_key": key})
    ledger.record(
        at=clock(),
        kind="message_sent",
        detail={"event_key": key, "message_id": "msg-0001"},
    )
    ledger.record(at=clock(), kind="webhook_received", detail={"event_key": "id:evt-2"})
    chain = ledger.explain_message("msg-0001")
    assert [r.kind for r in chain] == [
        "webhook_received",
        "draft_generated",
        "gate_decision",
        "message_sent",
    ]


def test_explain_message_returns_empty_for_a_stranger(clock) -> None:
    ledger = AuditLedger()
    ledger.record(at=clock(), kind="message_sent", detail={"message_id": "msg-1", "event_key": "k"})
    assert ledger.explain_message("msg-ghost") == ()
