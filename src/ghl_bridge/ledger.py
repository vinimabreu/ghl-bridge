"""The append-only record of what the bridge did and why.

The question this module exists to answer, verbatim from the operator's
chair: "why did that message leave on its own at 14:03". The answer has to
name the inbound event that started it, every policy that was evaluated
with its result, the approval the send travelled under, and the clock
reading at each step. An automation that cannot answer that question in
one query is an automation nobody should let near a customer thread.

Append-only is enforced, not promised: records are frozen, the sequence
number is assigned here, and the public surface exposes no update and no
delete. Reading back returns a tuple, so a caller holding the history
cannot edit it in place.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .clock import require_aware
from .models import Approval


class LedgerRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    at: datetime
    kind: str
    detail: Mapping[str, object]
    approval: Approval | None = None


class AuditLedger:
    """Every action, in order, with its reason attached."""

    def __init__(self) -> None:
        self._records: list[LedgerRecord] = []

    def record(
        self,
        *,
        at: datetime,
        kind: str,
        detail: Mapping[str, object],
        approval: Approval | None = None,
    ) -> LedgerRecord:
        entry = LedgerRecord(
            seq=len(self._records) + 1,
            at=require_aware(at, field="at"),
            kind=kind,
            detail=dict(detail),
            approval=approval,
        )
        self._records.append(entry)
        return entry

    def records(self) -> tuple[LedgerRecord, ...]:
        return tuple(self._records)

    def of_kind(self, kind: str) -> tuple[LedgerRecord, ...]:
        return tuple(r for r in self._records if r.kind == kind)

    def explain_message(self, message_id: str) -> tuple[LedgerRecord, ...]:
        """The full decision chain behind one outbound message.

        Finds the record that names the message, then walks back to every
        record sharing its origin event key, in order. The result reads as
        a story: webhook received, draft generated, policies evaluated,
        approval granted, message sent.
        """
        origin: object | None = None
        for record in self._records:
            if record.detail.get("message_id") == message_id:
                origin = record.detail.get("event_key")
                break
        if origin is None:
            return ()
        return tuple(r for r in self._records if r.detail.get("event_key") == origin)
