"""Webhook intake: verify the signature, then dedupe on the EVENT.

Two decisions here carry the whole module.

First, the signature scheme is injected. This package ships
:class:`HmacSha256Scheme` because a shared-secret HMAC is the scheme the
bridge can prove offline end to end. The HighLevel marketplace documents a
platform-signed signature header for app webhooks (an asymmetric scheme
verified with the platform's published public key); :class:`SignatureScheme`
is the seam where that verifier plugs in, and the RUNBOOK carries the step
that confirms the exact header and key against a live app. What never
changes across schemes: the raw body bytes are what is verified, before
any parsing, and a failed verification is a rejection with a ledger line,
never a silent drop.

Second, idempotency is keyed on the event, never on the delivery. Webhook
senders retry: a timeout, a 502, a redeploy, and the same event arrives
again under a fresh delivery id. Keying the dedupe on the delivery id
makes every retry a brand-new fact and the automation double-creates the
lead, double-moves the stage, double-sends the message. The key here is
the event's own identity, so a redelivery is recognised as the same fact
and has no second effect.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .clock import Clock
from .ledger import AuditLedger
from .models import WebhookEvent


class SignatureScheme(Protocol):
    """Verifies that ``signature`` covers ``raw_body``. Returns quietly on
    success, raises :class:`SignatureRejected` on failure."""

    def verify(self, raw_body: bytes, signature: str) -> None: ...


class SignatureRejected(Exception):
    """The signature does not cover these bytes. The reason is named so the
    ledger line distinguishes a wrong secret from a tampered body from a
    malformed header, which alarm differently."""

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(f"webhook signature rejected: {reason}")


class HmacSha256Scheme:
    """Shared-secret HMAC over the raw body, hex encoded, compared in
    constant time. The secret is injected, never read from a global."""

    def __init__(self, *, secret: bytes) -> None:
        if not secret:
            raise ValueError("an empty secret verifies everything; refused")
        self._secret = secret

    def sign(self, raw_body: bytes) -> str:
        return hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()

    def verify(self, raw_body: bytes, signature: str) -> None:
        if not signature:
            raise SignatureRejected(reason="empty signature header")
        expected = self.sign(raw_body)
        if not hmac.compare_digest(expected, signature):
            raise SignatureRejected(reason="signature does not match the body")


def event_key(event: WebhookEvent) -> str:
    """The identity of the event, independent of how many times it is delivered.

    Prefers the platform's own event id. When the payload carries none, the
    key is derived from what makes the event the event: its type, its
    location, the resource it concerns and the moment it occurred. Two
    deliveries of the same fact derive the same key; two different facts
    cannot collide without agreeing on all four fields.
    """
    if event.event_id:
        return f"id:{event.event_id}"
    material = "|".join(
        [
            event.event_type,
            event.location_id,
            event.resource_id or "",
            event.occurred_at.isoformat(),
        ]
    )
    return "derived:" + hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class Accepted:
    """The event was new; the handler ran once."""

    key: str
    delivery_id: str


@dataclass(frozen=True)
class Duplicate:
    """The same event, delivered again. No effect ran; the ledger shows
    both deliveries pointing at one processing record."""

    key: str
    delivery_id: str
    first_delivery_id: str


@dataclass(frozen=True)
class Rejected:
    """The signature failed. Nothing was parsed past the boundary."""

    delivery_id: str
    reason: str


IntakeResult = Accepted | Duplicate | Rejected


class WebhookIntake:
    """The front door. Verify, parse, dedupe on the event, then hand off.

    The handler is injected, so the intake can be tested for exactly-once
    dispatch without dragging the whole bridge in, and the bridge can be
    tested without re-testing signatures.

    The dedupe key is marked seen only after the handler completes. A
    handler that raises leaves the key unmarked, so the sender's retry gets
    to run the effect that never finished; the failed attempt is a ledger
    line, not a poisoned key. The contract that buys is stated plainly: a
    completed effect never runs twice, and the handler must keep its own
    partial writes safe to re-run, which every handler in
    :class:`ghl_bridge.bridge.Bridge` does by resolving contacts through
    upsert and dedupe rather than blind creation.
    """

    def __init__(
        self,
        *,
        scheme: SignatureScheme,
        handler: Callable[[WebhookEvent], None],
        ledger: AuditLedger,
        clock: Clock,
    ) -> None:
        self._scheme = scheme
        self._handler = handler
        self._ledger = ledger
        self._clock = clock
        self._seen: dict[str, str] = {}

    def receive(self, raw_body: bytes, *, signature: str, delivery_id: str) -> IntakeResult:
        try:
            self._scheme.verify(raw_body, signature)
        except SignatureRejected as exc:
            self._ledger.record(
                at=self._clock(),
                kind="webhook_rejected",
                detail={"delivery_id": delivery_id, "reason": exc.reason},
            )
            return Rejected(delivery_id=delivery_id, reason=exc.reason)

        event = WebhookEvent.model_validate(json.loads(raw_body))
        key = event_key(event)

        if key in self._seen:
            first = self._seen[key]
            self._ledger.record(
                at=self._clock(),
                kind="webhook_duplicate",
                detail={
                    "event_key": key,
                    "delivery_id": delivery_id,
                    "first_delivery_id": first,
                },
            )
            return Duplicate(key=key, delivery_id=delivery_id, first_delivery_id=first)

        self._ledger.record(
            at=self._clock(),
            kind="webhook_received",
            detail={
                "event_key": key,
                "event_type": event.event_type,
                "delivery_id": delivery_id,
                "location_id": event.location_id,
            },
        )
        try:
            self._handler(event)
        except Exception:
            self._ledger.record(
                at=self._clock(),
                kind="webhook_handler_failed",
                detail={"event_key": key, "delivery_id": delivery_id},
            )
            raise
        self._seen[key] = delivery_id
        return Accepted(key=key, delivery_id=delivery_id)
