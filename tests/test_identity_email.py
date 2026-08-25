"""The email dedupe key: ASCII fold, loud refusals, and the Kelvin anchor."""

from __future__ import annotations

import pytest

from ghl_bridge import InvalidEmail, normalise_email


def test_plain_address_passes_through() -> None:
    assert normalise_email("dana@riverbend.example") == "dana@riverbend.example"


@pytest.mark.parametrize(
    "raw",
    [
        "DANA@riverbend.example",
        "Dana@Riverbend.example",
        "dana@RIVERBEND.EXAMPLE",
        "DANA@RIVERBEND.EXAMPLE",
    ],
)
def test_ascii_case_folds_to_one_key(raw: str) -> None:
    assert normalise_email(raw) == "dana@riverbend.example"


@pytest.mark.parametrize(
    "raw",
    [
        " dana@riverbend.example",
        "dana@riverbend.example ",
        "\tdana@riverbend.example\n",
        "\r\vdana@riverbend.example\f",
    ],
)
def test_ascii_whitespace_is_trimmed(raw: str) -> None:
    assert normalise_email(raw) == "dana@riverbend.example"


def test_the_kelvin_anchor_two_spellings_do_not_merge() -> None:
    """U+212A KELVIN SIGN folds to ASCII k under str.lower and casefold.

    The whole doctrine in one assertion: the two addresses below are
    distinct identifiers, a Unicode fold would merge them into one dedupe
    key, and the ASCII-only fold keeps them apart.
    """
    kelvin = "Kelvin@riverbend.example"
    ascii_k = "kelvin@riverbend.example"
    assert kelvin.lower() == ascii_k.lower()  # the trap this fold avoids
    assert normalise_email(kelvin) != normalise_email(ascii_k)


def test_the_kelvin_sign_survives_the_fold_unchanged() -> None:
    kelvin = "Kelvin@riverbend.example"
    assert normalise_email(kelvin) == kelvin


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("straße@riverbend.example", "strasse@riverbend.example"),
        ("ﬁona@riverbend.example", "fiona@riverbend.example"),
        ("ſam@riverbend.example", "sam@riverbend.example"),
    ],
)
def test_other_unicode_fold_traps_stay_distinct(a: str, b: str) -> None:
    """Sharp s, the fi ligature and long s all fold onto ASCII under
    casefold; none of them may merge here."""
    assert a.casefold() == b.casefold()
    assert normalise_email(a) != normalise_email(b)


def test_non_ascii_case_pairs_stay_distinct_and_that_is_the_stated_cost() -> None:
    upper = "Ärger@riverbend.example"
    lower = "ärger@riverbend.example"
    assert normalise_email(upper) != normalise_email(lower)


@pytest.mark.parametrize("raw", ["dana", "dana@", "@riverbend.example", "a@b@c.example"])
def test_shapes_that_are_not_an_address_are_refused(raw: str) -> None:
    with pytest.raises(InvalidEmail):
        normalise_email(raw)


def test_zero_at_signs_is_refused_with_the_count_named() -> None:
    with pytest.raises(InvalidEmail, match="got 0"):
        normalise_email("dana.riverbend.example")


@pytest.mark.parametrize("domain", ["example", ".example.com", "example.com."])
def test_implausible_domains_are_refused(domain: str) -> None:
    with pytest.raises(InvalidEmail):
        normalise_email(f"dana@{domain}")


@pytest.mark.parametrize(
    ("label", "sneaky"),
    [
        ("zero width space", "dana@riverbend.example​"),
        ("ideographic space", "dana@riverbend.example　"),
        ("no-break space", "dana @riverbend.example"),
        ("soft hyphen", "da­na@riverbend.example"),
        ("left-to-right mark", "dana@river‎bend.example"),
        ("interior newline", "dana@river\nbend.example"),
    ],
)
def test_invisible_characters_are_refused_loudly(label: str, sneaky: str) -> None:
    with pytest.raises(InvalidEmail):
        normalise_email(sneaky)


def test_the_refusal_names_the_code_point_not_the_glyph() -> None:
    with pytest.raises(InvalidEmail, match="U\\+200B"):
        normalise_email("dana@riverbend.example​")


def test_interior_ascii_space_is_refused_not_trimmed() -> None:
    """Trimming is edges only. An interior space is not formatting; folding
    it away would merge two distinct raw inputs."""
    with pytest.raises(InvalidEmail):
        normalise_email("da na@riverbend.example")


def test_dots_in_the_local_part_are_preserved() -> None:
    """No provider-specific dot stripping: dana.w@ and danaw@ are distinct
    on most providers, and guessing the provider's aliasing rules is the
    kind of cleverness that merges strangers."""
    assert normalise_email("dana.w@riverbend.example") != normalise_email(
        "danaw@riverbend.example"
    )


def test_plus_tags_are_preserved() -> None:
    assert normalise_email("dana+quotes@riverbend.example") == "dana+quotes@riverbend.example"


def test_idempotent_normalising_twice_changes_nothing() -> None:
    once = normalise_email(" DANA@Riverbend.example ")
    assert normalise_email(once) == once
