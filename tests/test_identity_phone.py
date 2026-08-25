"""The phone dedupe key: deterministic E.164 or an explicit refusal."""

from __future__ import annotations

import pytest

from ghl_bridge import NormalisedPhone, PhoneNeedsReview, normalise_phone


def e164(raw: str, region: str | None = None) -> str:
    result = normalise_phone(raw, default_region=region)
    assert isinstance(result, NormalisedPhone), result
    return result.e164


def review(raw: str, region: str | None = None) -> PhoneNeedsReview:
    result = normalise_phone(raw, default_region=region)
    assert isinstance(result, PhoneNeedsReview), result
    return result


# ------------------------------------------------------------- already E.164


def test_a_plus_number_passes_through() -> None:
    assert e164("+15005550100") == "+15005550100"


@pytest.mark.parametrize(
    "raw",
    [
        "+1 500 555 0100",
        "+1 (500) 555-0100",
        "+1-500-555-0100",
        "+1.500.555.0100",
    ],
)
def test_formatting_characters_carry_no_identity(raw: str) -> None:
    assert e164(raw) == "+15005550100"


def test_the_international_call_prefix_00_converts_to_plus() -> None:
    assert e164("0015005550100") == "+15005550100"


def test_00_conversion_needs_no_region() -> None:
    assert e164("00442071838750", None) == "+442071838750"


@pytest.mark.parametrize("raw", ["+15005550100", "+442071838750", "+5521995550100"])
def test_idempotent_a_normalised_number_normalises_to_itself(raw: str) -> None:
    assert e164(e164(raw)) == e164(raw)


# ------------------------------------------------------------- refusals on +


def test_plus_then_nothing_is_refused() -> None:
    assert "no digits" in review("+").reason


def test_a_country_code_starting_with_zero_is_refused() -> None:
    assert "starts with 0" in review("+05005550100").reason


def test_too_few_digits_after_plus_is_refused() -> None:
    assert "E.164 admits 8 to 15" in review("+1234567").reason


def test_too_many_digits_after_plus_is_refused() -> None:
    assert "E.164 admits 8 to 15" in review("+1234567890123456").reason


@pytest.mark.parametrize("raw", ["+1500555010x", "+1500555a0100", "call me"])
def test_letters_are_refused_with_the_code_point_named(raw: str) -> None:
    assert "neither a digit" in review(raw).reason


def test_an_empty_string_is_refused() -> None:
    assert review("").reason == "empty after trimming whitespace"


def test_whitespace_only_is_refused() -> None:
    assert review("   ").reason == "empty after trimming whitespace"


# ------------------------------------------------------- the ambiguity rule


def test_the_anchor_a_bare_national_number_with_no_region_is_never_guessed() -> None:
    """The central refusal: 10 digits could be a US number, a UK number
    missing its trunk digit, or a BR landline. Without a region there is
    no fact of the matter, and guessing merges strangers."""
    result = review("5005550100", None)
    assert "guessing a country merges strangers" in result.reason
    assert result.raw == "5005550100"


def test_a_region_outside_the_table_is_refused_not_guessed() -> None:
    assert "no numbering plan" in review("5005550100", "DE").reason


def test_the_refusal_carries_the_raw_input_for_the_review_queue() -> None:
    assert review("call me").raw == "call me"


# --------------------------------------------------------- regional resolves


def test_a_us_national_number_resolves_with_the_region() -> None:
    assert e164("5005550100", "US") == "+15005550100"


def test_us_formatting_resolves_too() -> None:
    assert e164("(500) 555-0100", "US") == "+15005550100"


def test_a_us_number_already_carrying_its_country_code_resolves() -> None:
    assert e164("15005550100", "US") == "+15005550100"


def test_ca_shares_the_nanp_plan() -> None:
    assert e164("5005550100", "CA") == "+15005550100"


def test_a_gb_number_with_the_trunk_zero_resolves() -> None:
    assert e164("02071838750", "GB") == "+442071838750"


def test_a_gb_number_without_the_trunk_zero_resolves() -> None:
    assert e164("2071838750", "GB") == "+442071838750"


def test_an_au_number_with_the_trunk_zero_resolves() -> None:
    assert e164("0355501234", "AU") == "+61355501234"


def test_a_br_landline_resolves() -> None:
    assert e164("2135550100", "BR") == "+552135550100"


def test_a_br_mobile_with_the_leading_9_resolves() -> None:
    assert e164("21995550100", "BR") == "+5521995550100"


def test_region_codes_are_case_insensitive() -> None:
    assert e164("5005550100", "us") == "+15005550100"


@pytest.mark.parametrize(
    ("raw", "region"),
    [
        ("500555010", "US"),
        ("50055501000", "US"),
        ("207183875", "GB"),
        ("123", "BR"),
    ],
)
def test_a_digit_count_the_plan_does_not_admit_is_refused(raw: str, region: str) -> None:
    assert "numbering plan" in review(raw, region).reason


def test_an_11_digit_br_number_without_the_mobile_9_is_refused() -> None:
    result = review("21185550100", "BR")
    assert "numbering plan" in result.reason


def test_a_plus_number_ignores_the_region_entirely() -> None:
    """An explicit country code always wins; the region is only for bare
    national numbers."""
    assert e164("+442071838750", "US") == "+442071838750"
