"""The dedupe doctrine applied: merge with evidence, create on absence,
refuse the guess."""

from __future__ import annotations

from tests.conftest import DANA_EMAIL, DANA_PHONE, LOCATION_ID, build_location

from ghl_bridge import (
    AuditLedger,
    ContactDeduper,
    ContactUpsert,
    NeedsHumanReview,
    Resolved,
)


def deduper_with_ledger(server, token, clock) -> tuple[ContactDeduper, AuditLedger]:
    ledger = AuditLedger()
    return ContactDeduper(port=server.port_for(token), ledger=ledger, clock=clock), ledger


LOCATION = build_location()


# -------------------------------------------------------------------- merges


def test_a_case_variant_email_merges_with_the_named_key(server, token, clock, dana) -> None:
    deduper, ledger = deduper_with_ledger(server, token, clock)
    result = deduper.resolve(
        LOCATION,
        ContactUpsert(first_name="Dana", email=" DANA@Riverbend.example "),
        event_key="id:evt-1",
    )
    assert isinstance(result, Resolved)
    assert result.created is False
    assert result.matched_on == "email"
    assert result.key == "dana@riverbend.example"
    assert result.contact.contact_id == dana.contact_id
    merged = ledger.of_kind("contact_merged")
    assert merged[0].detail["raw_email"] == " DANA@Riverbend.example "


def test_a_formatted_phone_merges_on_the_e164_key(server, token, clock, dana) -> None:
    deduper, _ = deduper_with_ledger(server, token, clock)
    result = deduper.resolve(
        LOCATION,
        ContactUpsert(phone="(500) 555-0100"),
        event_key="id:evt-2",
    )
    assert isinstance(result, Resolved)
    assert result.matched_on == "phone"
    assert result.key == DANA_PHONE
    assert result.contact.contact_id == dana.contact_id


def test_email_outranks_phone_when_both_match_the_same_contact(server, token, clock, dana) -> None:
    deduper, _ = deduper_with_ledger(server, token, clock)
    result = deduper.resolve(
        LOCATION,
        ContactUpsert(email=DANA_EMAIL, phone=DANA_PHONE),
        event_key="id:evt-3",
    )
    assert isinstance(result, Resolved)
    assert result.matched_on == "email"


def test_the_merge_carries_new_fields_onto_the_existing_contact(server, token, clock, dana) -> None:
    deduper, _ = deduper_with_ledger(server, token, clock)
    result = deduper.resolve(
        LOCATION,
        ContactUpsert(email=DANA_EMAIL, last_name="Whitfield-Ortiz"),
        event_key="id:evt-4",
    )
    assert isinstance(result, Resolved)
    assert result.contact.last_name == "Whitfield-Ortiz"
    assert result.contact.phone == DANA_PHONE  # untouched fields survive


def test_the_kelvin_spelling_creates_a_second_contact_not_a_merge(server, token, clock) -> None:
    deduper, _ = deduper_with_ledger(server, token, clock)
    first = deduper.resolve(
        LOCATION, ContactUpsert(email="kelvin@riverbend.example"), event_key="id:evt-5"
    )
    second = deduper.resolve(
        LOCATION,
        ContactUpsert(email="Kelvin@riverbend.example"),
        event_key="id:evt-6",
    )
    assert isinstance(first, Resolved)
    assert isinstance(second, Resolved)
    assert second.created is True
    assert first.contact.contact_id != second.contact.contact_id


# ------------------------------------------------------------------- creates


def test_a_new_email_creates_and_says_so(server, token, clock, dana) -> None:
    deduper, ledger = deduper_with_ledger(server, token, clock)
    result = deduper.resolve(
        LOCATION, ContactUpsert(email="ana@riverbend.example"), event_key="id:evt-7"
    )
    assert isinstance(result, Resolved)
    assert result.created is True
    assert result.matched_on is None
    assert len(ledger.of_kind("contact_created")) == 1


def test_resolving_the_same_lead_twice_is_idempotent(server, token, clock) -> None:
    """Second resolution of the same normalized key merges instead of
    creating; the contact count does not grow."""
    deduper, _ = deduper_with_ledger(server, token, clock)
    first = deduper.resolve(
        LOCATION, ContactUpsert(email="ana@riverbend.example"), event_key="id:evt-8"
    )
    second = deduper.resolve(
        LOCATION, ContactUpsert(email="ANA@riverbend.example"), event_key="id:evt-9"
    )
    assert isinstance(first, Resolved)
    assert isinstance(second, Resolved)
    assert second.created is False
    assert second.contact.contact_id == first.contact.contact_id


# ------------------------------------------------------------------ refusals


def test_a_bare_number_with_no_region_goes_to_review(server, token, clock) -> None:
    deduper, ledger = deduper_with_ledger(server, token, clock)
    no_region = build_location().model_copy(update={"default_region": None})
    result = deduper.resolve(
        no_region, ContactUpsert(phone="5005550100"), event_key="id:evt-10"
    )
    assert isinstance(result, NeedsHumanReview)
    assert "guessing a country merges strangers" in result.reason
    assert len(ledger.of_kind("lead_needs_review")) == 1


def test_an_invalid_email_goes_to_review_not_to_a_crash(server, token, clock) -> None:
    deduper, _ = deduper_with_ledger(server, token, clock)
    result = deduper.resolve(
        LOCATION, ContactUpsert(email="not-an-address"), event_key="id:evt-11"
    )
    assert isinstance(result, NeedsHumanReview)
    assert result.reason.startswith("email refused")


def test_a_lead_with_nothing_to_match_on_goes_to_review(server, token, clock) -> None:
    deduper, _ = deduper_with_ledger(server, token, clock)
    result = deduper.resolve(
        LOCATION, ContactUpsert(first_name="Ghost"), event_key="id:evt-12"
    )
    assert isinstance(result, NeedsHumanReview)
    assert "no email and no phone" in result.reason


def test_conflicting_matches_refuse_rather_than_pick(server, token, clock, dana, port) -> None:
    """Email points at one contact, phone at another. Either merge is a
    guess about which record is the person, so neither happens."""
    other = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="ana@riverbend.example", phone="+15005550199")
    )
    deduper, ledger = deduper_with_ledger(server, token, clock)
    result = deduper.resolve(
        LOCATION,
        ContactUpsert(email=DANA_EMAIL, phone="+15005550199"),
        event_key="id:evt-13",
    )
    assert isinstance(result, NeedsHumanReview)
    assert dana.contact_id in result.reason
    assert other.contact_id in result.reason


def test_a_review_lead_creates_no_contact(server, token, clock, port) -> None:
    deduper, _ = deduper_with_ledger(server, token, clock)
    deduper.resolve(LOCATION, ContactUpsert(email="broken@"), event_key="id:evt-14")
    assert port.search_contacts_by_email(LOCATION_ID, "broken@x.example") == ()


def test_a_valid_email_with_an_unresolvable_phone_still_merges_on_email(
    server, token, clock, dana
) -> None:
    """The phone cannot become a key, but the email can, and one good key
    is enough to resolve; the raw phone rides along on the contact."""
    deduper, _ = deduper_with_ledger(server, token, clock)
    result = deduper.resolve(
        LOCATION,
        ContactUpsert(email=DANA_EMAIL, phone="not a number"),
        event_key="id:evt-15",
    )
    assert isinstance(result, Resolved)
    assert result.matched_on == "email"
