"""The fake's contact semantics: upsert matching, search keys, DND."""

from __future__ import annotations

import pytest
from tests.conftest import DANA_EMAIL, DANA_PHONE, LOCATION_ID

from ghl_bridge import ContactUpsert, InvalidRequest, NotFound
from ghl_bridge.fakes import FakeHighLevel


def test_upsert_with_a_new_email_creates_a_contact(port, server) -> None:
    contact = port.upsert_contact(
        LOCATION_ID, ContactUpsert(first_name="Ana", email="ana@riverbend.example")
    )
    assert contact.contact_id.startswith("con-")
    assert contact.first_name == "Ana"


def test_upsert_assigns_distinct_ids(port) -> None:
    a = port.upsert_contact(LOCATION_ID, ContactUpsert(email="a@riverbend.example"))
    b = port.upsert_contact(LOCATION_ID, ContactUpsert(email="b@riverbend.example"))
    assert a.contact_id != b.contact_id


def test_upsert_matching_on_email_updates_instead_of_creating(port, dana) -> None:
    again = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email=DANA_EMAIL, last_name="Whitfield-Ortiz")
    )
    assert again.contact_id == dana.contact_id
    assert again.last_name == "Whitfield-Ortiz"


def test_upsert_matches_email_case_insensitively_ascii_only(port, dana) -> None:
    again = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="DANA@Riverbend.example")
    )
    assert again.contact_id == dana.contact_id


def test_upsert_matching_on_phone_updates_instead_of_creating(port, dana) -> None:
    again = port.upsert_contact(LOCATION_ID, ContactUpsert(phone=DANA_PHONE))
    assert again.contact_id == dana.contact_id


def test_upsert_matches_phone_across_formatting(port, dana) -> None:
    again = port.upsert_contact(
        LOCATION_ID, ContactUpsert(phone="+1 (500) 555-0100")
    )
    assert again.contact_id == dana.contact_id


def test_upsert_matches_a_national_number_via_the_location_region(port, dana) -> None:
    """The location's default region is US, so the bare 10-digit form keys
    to the same E.164 as the stored +1 number."""
    again = port.upsert_contact(LOCATION_ID, ContactUpsert(phone="(500) 555-0100"))
    assert again.contact_id == dana.contact_id


def test_upsert_merge_keeps_existing_fields_that_the_update_omits(port, dana) -> None:
    merged = port.upsert_contact(LOCATION_ID, ContactUpsert(email=DANA_EMAIL))
    assert merged.first_name == "Dana"
    assert merged.phone == DANA_PHONE


def test_upsert_merge_unions_tags_preserving_order(port, server) -> None:
    port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="t@riverbend.example", tags=("vip",))
    )
    merged = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="t@riverbend.example", tags=("returning", "vip"))
    )
    assert merged.tags == ("vip", "returning")


def test_upsert_merge_overlays_custom_fields(port) -> None:
    port.upsert_contact(
        LOCATION_ID,
        ContactUpsert(email="c@riverbend.example", custom_fields={"car": "sedan"}),
    )
    merged = port.upsert_contact(
        LOCATION_ID,
        ContactUpsert(email="c@riverbend.example", custom_fields={"car": "wagon", "color": "blue"}),
    )
    assert merged.custom_fields == {"car": "wagon", "color": "blue"}


def test_upsert_with_neither_email_nor_phone_is_refused(port) -> None:
    with pytest.raises(InvalidRequest, match="email or a phone"):
        port.upsert_contact(LOCATION_ID, ContactUpsert(first_name="Ghost"))


def test_the_kelvin_spelling_does_not_merge_in_the_fake_either(port) -> None:
    """The doctrine holds at the storage layer too: the fake indexes by the
    same ASCII-only fold, so the Kelvin-sign address is a second contact,
    not a silent merge."""
    ascii_contact = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="kelvin@riverbend.example")
    )
    kelvin_contact = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="Kelvin@riverbend.example")
    )
    assert ascii_contact.contact_id != kelvin_contact.contact_id


def test_get_contact_returns_the_stored_record(port, dana) -> None:
    assert port.get_contact(LOCATION_ID, dana.contact_id).email == DANA_EMAIL


def test_get_contact_404s_on_a_stranger(port) -> None:
    with pytest.raises(NotFound, match="con-ghost"):
        port.get_contact(LOCATION_ID, "con-ghost")


def test_search_by_email_finds_the_contact(port, dana) -> None:
    hits = port.search_contacts_by_email(LOCATION_ID, DANA_EMAIL)
    assert [c.contact_id for c in hits] == [dana.contact_id]


def test_search_by_email_folds_ascii_case(port, dana) -> None:
    hits = port.search_contacts_by_email(LOCATION_ID, " DANA@RIVERBEND.EXAMPLE ")
    assert [c.contact_id for c in hits] == [dana.contact_id]


def test_search_by_email_misses_cleanly(port) -> None:
    assert port.search_contacts_by_email(LOCATION_ID, "ghost@riverbend.example") == ()


def test_search_by_phone_finds_across_formats(port, dana) -> None:
    hits = port.search_contacts_by_phone(LOCATION_ID, "500-555-0100")
    assert [c.contact_id for c in hits] == [dana.contact_id]


def test_search_by_phone_misses_cleanly(port) -> None:
    assert port.search_contacts_by_phone(LOCATION_ID, "+15005550199") == ()


def test_seeded_contacts_and_upserts_share_one_id_space(server, port, dana) -> None:
    new = port.upsert_contact(LOCATION_ID, ContactUpsert(email="n@riverbend.example"))
    assert new.contact_id != dana.contact_id


def test_dnd_send_is_refused_by_the_platform_itself(server, port, dana) -> None:
    from ghl_bridge import OutboundSend

    server.set_opted_out(LOCATION_ID, dana.contact_id, True)
    with pytest.raises(InvalidRequest, match="opted out"):
        port.send_message(
            LOCATION_ID, OutboundSend(contact_id=dana.contact_id, body="hello")
        )


def test_send_message_appends_an_outbound_to_the_conversation(port, dana) -> None:
    from ghl_bridge import MessageDirection, OutboundSend

    message = port.send_message(
        LOCATION_ID, OutboundSend(contact_id=dana.contact_id, body="hello Dana")
    )
    conversation = port.get_conversation(LOCATION_ID, message.conversation_id)
    assert conversation.messages[-1].direction is MessageDirection.OUTBOUND
    assert conversation.messages[-1].body == "hello Dana"


def test_send_message_404s_on_a_missing_contact(port) -> None:
    from ghl_bridge import OutboundSend

    with pytest.raises(NotFound):
        port.send_message(LOCATION_ID, OutboundSend(contact_id="con-ghost", body="x"))


def test_seed_inbound_and_send_share_one_conversation(server, port, dana, clock) -> None:
    inbound = server.seed_inbound(LOCATION_ID, dana.contact_id, "hi", clock())
    from ghl_bridge import OutboundSend

    outbound = port.send_message(
        LOCATION_ID, OutboundSend(contact_id=dana.contact_id, body="hello")
    )
    assert inbound.conversation_id == outbound.conversation_id
    conversation = port.get_conversation(LOCATION_ID, inbound.conversation_id)
    assert [m.direction.value for m in conversation.messages] == ["inbound", "outbound"]


def test_message_timestamps_come_from_the_injected_clock(port, dana, clock) -> None:
    from ghl_bridge import OutboundSend

    clock.advance(120)
    message = port.send_message(
        LOCATION_ID, OutboundSend(contact_id=dana.contact_id, body="x")
    )
    assert message.at == clock()


def test_get_conversation_404s_on_a_stranger(port) -> None:
    with pytest.raises(NotFound):
        port.get_conversation(LOCATION_ID, "cnv-ghost")


def test_duplicate_location_registration_is_refused(server, location) -> None:
    with pytest.raises(ValueError, match="already exists"):
        server.add_location(location)


def test_issuing_a_token_for_a_ghost_location_404s(clock) -> None:
    server = FakeHighLevel(clock=clock)
    with pytest.raises(NotFound):
        server.issue_private_token("loc-ghost")


def test_two_strangers_with_the_same_unparseable_phone_do_not_merge(port) -> None:
    """The audit case: two people whose form phone is the same junk string
    ("N/A") are two people. Indexing the raw string would fold them into
    one contact with one conversation history; an unresolvable phone mints
    no key at all."""
    ann = port.upsert_contact(
        LOCATION_ID, ContactUpsert(first_name="Ann", email="ann@x.example", phone="N/A")
    )
    bob = port.upsert_contact(
        LOCATION_ID, ContactUpsert(first_name="Bob", email="bob@x.example", phone="N/A")
    )
    assert ann.contact_id != bob.contact_id
    assert port.get_contact(LOCATION_ID, ann.contact_id).first_name == "Ann"
    assert port.get_contact(LOCATION_ID, bob.contact_id).first_name == "Bob"


def test_an_unparseable_phone_is_stored_as_a_field_but_never_indexed(port) -> None:
    contact = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="junk@x.example", phone="ext-12")
    )
    assert contact.phone == "ext-12"  # the raw value survives as data
    assert port.search_contacts_by_phone(LOCATION_ID, "ext-12") == ()  # but is no key


def test_the_ascii_trim_doctrine_holds_for_phone_indexing_too(port) -> None:
    """The anchor: "ext-12" and "ext-12" + U+3000 IDEOGRAPHIC SPACE are two
    distinct raw strings. A str.strip() fallback would fold them onto one
    index key behind the doctrine's back; with no raw indexing at all,
    neither spelling can reach the other through the phone index."""
    a = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="exta@x.example", phone="ext-12")
    )
    b = port.upsert_contact(
        LOCATION_ID, ContactUpsert(email="extb@x.example", phone="ext-12　")
    )
    assert a.contact_id != b.contact_id
    assert port.search_contacts_by_phone(LOCATION_ID, "ext-12") == ()
    assert port.search_contacts_by_phone(LOCATION_ID, "ext-12　") == ()
