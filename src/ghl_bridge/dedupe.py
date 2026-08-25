"""Lead dedupe: merge on a doctrine key, create on proof of absence, and
refuse to guess in between.

A new lead is one of exactly three things: the same person again (merge,
with the matched key named), a genuinely new person (create), or an
identity the rules cannot determine (a phone with no country and no
regional default, an email with an invisible character). The third case is
a first-class outcome, :class:`NeedsHumanReview`, not an exception and not
a best-effort create: an automation that guesses on identity either merges
strangers or fills the CRM with near-duplicates, and both failures are
invisible until a human trips over them.

The match keys come from :mod:`ghl_bridge.identity` and nowhere else, so
the fake, the deduper and the audit line all agree on what "the same
email" means, Kelvin sign and all.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock
from .identity import (
    InvalidEmail,
    NormalisedPhone,
    PhoneNeedsReview,
    normalise_email,
    normalise_phone,
)
from .ledger import AuditLedger
from .models import Contact, ContactUpsert, Location
from .ports import HighLevelPort


@dataclass(frozen=True)
class Resolved:
    """The lead has a contact record now. ``created`` says which path;
    ``matched_on`` and ``key`` name the evidence when it was a merge."""

    contact: Contact
    created: bool
    matched_on: str | None
    key: str | None


@dataclass(frozen=True)
class NeedsHumanReview:
    """The rules refuse to decide. Carries the lead and the named reason so
    a review queue shows a human exactly what arrived and what was missing."""

    lead: ContactUpsert
    reason: str


DedupeResult = Resolved | NeedsHumanReview


class ContactDeduper:
    """Resolves a lead to exactly one contact, or to a named refusal.

    Resolution order is email first, then phone, and the order is doctrine:
    an email is chosen by a human and survives device changes; a phone
    number gets recycled by carriers and shared by households. When both
    are present and point at different existing contacts, that conflict is
    exactly the kind of guess this class refuses to make.
    """

    def __init__(self, *, port: HighLevelPort, ledger: AuditLedger, clock: Clock) -> None:
        self._port = port
        self._ledger = ledger
        self._clock = clock

    def resolve(
        self, location: Location, lead: ContactUpsert, *, event_key: str
    ) -> DedupeResult:
        email_key: str | None = None
        if lead.email:
            try:
                email_key = normalise_email(lead.email)
            except InvalidEmail as exc:
                return self._refuse(lead, f"email refused: {exc}", event_key)

        phone_key: str | None = None
        phone_review: PhoneNeedsReview | None = None
        if lead.phone:
            result = normalise_phone(lead.phone, default_region=location.default_region)
            if isinstance(result, NormalisedPhone):
                phone_key = result.e164
            else:
                phone_review = result

        if email_key is None and phone_key is None:
            reason = (
                f"phone refused: {phone_review.reason}"
                if phone_review is not None
                else "the lead carries no email and no phone; nothing to match or create on"
            )
            return self._refuse(lead, reason, event_key)

        email_hit = (
            self._first(self._port.search_contacts_by_email(location.location_id, email_key))
            if email_key
            else None
        )
        phone_hit = (
            self._first(self._port.search_contacts_by_phone(location.location_id, phone_key))
            if phone_key
            else None
        )

        if (
            email_hit is not None
            and phone_hit is not None
            and email_hit.contact_id != phone_hit.contact_id
        ):
            return self._refuse(
                lead,
                f"the email matches contact {email_hit.contact_id!r} but the phone "
                f"matches contact {phone_hit.contact_id!r}; merging either way is a "
                "guess about which record is the person",
                event_key,
            )

        hit = email_hit or phone_hit
        matched_on = (
            "email" if email_hit is not None else "phone" if phone_hit is not None else None
        )
        contact = self._port.upsert_contact(location.location_id, lead)

        if hit is not None:
            key = email_key if matched_on == "email" else phone_key
            self._ledger.record(
                at=self._clock(),
                kind="contact_merged",
                detail={
                    "contact_id": contact.contact_id,
                    "matched_on": matched_on or "",
                    "key": key or "",
                    "raw_email": lead.email or "",
                    "raw_phone": lead.phone or "",
                    "event_key": event_key,
                },
            )
            return Resolved(contact=contact, created=False, matched_on=matched_on, key=key)

        self._ledger.record(
            at=self._clock(),
            kind="contact_created",
            detail={"contact_id": contact.contact_id, "event_key": event_key},
        )
        return Resolved(contact=contact, created=True, matched_on=None, key=None)

    def _refuse(
        self, lead: ContactUpsert, reason: str, event_key: str
    ) -> NeedsHumanReview:
        self._ledger.record(
            at=self._clock(),
            kind="lead_needs_review",
            detail={"reason": reason, "event_key": event_key},
        )
        return NeedsHumanReview(lead=lead, reason=reason)

    @staticmethod
    def _first(hits: tuple[Contact, ...]) -> Contact | None:
        return hits[0] if hits else None
