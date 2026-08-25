"""The redundant check between the gate and the transport.

In a correct wiring this never fires: every outbound message reaches the
transport through :class:`ghl_bridge.bridge.Bridge`, which only sends what
the policy gate stamped an approval on. It stays because the bridge is one
refactor away from not being the only caller: a bulk campaign script, a
"quick fix" that calls the port directly, a retry path that rebuilds the
send and forgets the stamp. Any of those can put words in front of a
customer that nobody approved, and this is the layer that notices.

It fails loud. A violation raises :class:`UnapprovedOutbound` naming the
contact and the reason, after the ledger records the breach. It does not
drop the message and continue, because a silent drop hides a real bug in
the layer above: the system would keep looking healthy while some path
quietly tries to talk to customers without approval.
"""

from __future__ import annotations

import hashlib

from .clock import Clock
from .ledger import AuditLedger
from .models import Approval, Message, OutboundSend
from .ports import HighLevelPort
from .store import InMemoryKeyStore, KeyStore


def content_fingerprint(send: OutboundSend) -> str:
    """The exact thing an approval covers: this text, to this contact, on
    this channel. Change any of the three and it is a different message
    that nobody approved.

    Each field is length-prefixed before joining, so the field boundaries
    are part of the hash: a plain join would let ``("con-1|S", "MS")`` and
    ``("con-1", "S|MS")`` collide into one fingerprint, and a fingerprint
    two different messages share is an approval that travels."""
    material = "|".join(
        f"{len(part)}:{part}" for part in (send.contact_id, send.channel, send.body)
    )
    return hashlib.sha256(material.encode()).hexdigest()


class UnapprovedOutbound(Exception):
    """An outbound message reached the transport without a valid approval.

    Carries the structured detail as attributes as well as in the message,
    so a handler can route on it without parsing a string.
    """

    def __init__(self, *, contact_id: str, why: str) -> None:
        self.contact_id = contact_id
        self.why = why
        super().__init__(
            f"unapproved outbound to contact {contact_id!r} stopped at the "
            f"transport: {why}"
        )


class ApprovedSender:
    """The only object in this package that talks to the send endpoint.

    Three checks, each a way an unapproved message arrives looking right:

    - an approval must be present at all, which catches the path that
      skipped the gate entirely;
    - the approval's fingerprint must match this exact send, which catches
      a draft edited after approval and an approval reattached to a
      different contact or a different text;
    - the approval must not have been used already, which catches a retry
      loop replaying one approval across many sends.

    The spent-approval ids live behind an injected
    :class:`~ghl_bridge.store.KeyStore` defaulting to process-local
    memory, so the single-use rule holds per process lifetime; inject a
    durable store to make it hold across restarts. The RUNBOOK carries
    the step.
    """

    def __init__(
        self,
        *,
        port: HighLevelPort,
        ledger: AuditLedger,
        clock: Clock,
        spent_store: KeyStore | None = None,
    ) -> None:
        self._port = port
        self._ledger = ledger
        self._clock = clock
        self._spent: KeyStore = spent_store if spent_store is not None else InMemoryKeyStore()

    def send(
        self,
        location_id: str,
        send: OutboundSend,
        *,
        approval: Approval | None,
        event_key: str | None = None,
    ) -> Message:
        why: str | None = None
        if approval is None:
            why = "no approval attached"
        elif approval.content_sha256 != content_fingerprint(send):
            why = (
                "the approval covers different content; the draft changed "
                "after it was approved, or it was minted for another message"
            )
        elif self._spent.get(approval.decision_id) is not None:
            why = f"approval {approval.decision_id!r} was already used for a send"

        if why is not None:
            self._ledger.record(
                at=self._clock(),
                kind="guard_breach",
                detail={
                    "contact_id": send.contact_id,
                    "why": why,
                    "event_key": event_key or "",
                },
            )
            raise UnapprovedOutbound(contact_id=send.contact_id, why=why)

        assert approval is not None
        message = self._port.send_message(location_id, send)
        self._spent.set(approval.decision_id, message.message_id)
        self._ledger.record(
            at=self._clock(),
            kind="message_sent",
            detail={
                "message_id": message.message_id,
                "contact_id": send.contact_id,
                "mode": approval.mode,
                "approved_by": approval.approved_by,
                "event_key": event_key or "",
            },
            approval=approval,
        )
        return message
