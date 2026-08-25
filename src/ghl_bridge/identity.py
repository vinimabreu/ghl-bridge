"""Identity doctrine: how an email or a phone number becomes a dedupe key.

Deduplication merges records, and a merge is irreversible in every way that
matters: notes, conversation history and pipeline position all land on one
contact. So the normalisation that feeds the merge must never be able to
fold two genuinely different identifiers onto one key, and it must refuse
to guess when the input does not determine the answer.

Two rules fall out of that:

1. Email case folding is ASCII-only, never ``str.lower`` or
   ``str.casefold``. The Unicode case-folding tables are many-to-one
   across characters that are not case variants of each other: U+212A
   KELVIN SIGN folds to ASCII ``k``, U+00DF SHARP S folds to ``ss``,
   U+FB01 LIGATURE FI folds to ``fi``, U+017F LONG S folds to ``s``. Any
   of those merges two distinct addresses into one dedupe key before a
   single comparison happens. Folding only ``A-Z`` cannot merge two
   distinct characters, because ``A-Z`` and ``a-z`` are genuine case pairs
   and nothing else is touched. The cost is stated plainly: a non-ASCII
   address is never folded, so two case spellings of a non-ASCII local
   part stay two keys. That fails toward a duplicate contact, which a
   human can merge, instead of toward a silent merge, which nobody can
   split.

2. Phone normalisation is deterministic E.164 or an explicit refusal.
   A number that arrives without a country code and without a location
   default region does not determine its own E.164 form, and guessing the
   country merges strangers. The result type makes the refusal a value,
   :class:`PhoneNeedsReview`, not an exception and not a silent skip, so
   the caller has to route it to a human queue on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass

ASCII_WHITESPACE = " \t\n\r\v\f"
"""The only characters the normalisers trim.

``str.strip()`` with no argument removes every Unicode whitespace
character, which merges identifiers the same way a Unicode case fold
does: ``"dana@x.example"`` and ``"dana@x.example" + U+3000`` (IDEOGRAPHIC
SPACE) become one key before a single comparison happens. Trimming these
six characters and nothing else cannot merge two identifiers that differ
anywhere outside them.
"""

_ASCII_CASE_FOLD = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)

_DIAL_DIGITS = frozenset("0123456789")
_PHONE_FORMATTING = frozenset(" ()-. ")
"""Characters that carry formatting, not identity, in a phone number."""


class InvalidEmail(ValueError):
    """The address cannot be a dedupe key and saying so loudly is the point.

    An address carrying an invisible character is either a paste accident
    or a second spelling shadowing a contact that already exists, and the
    two cannot be told apart from here. Accepting it would file the lead
    under a key nobody can type or spot in an audit line; trimming the
    character away would merge two distinct inputs. A loud error at the
    boundary is the only option that leaves a human able to see what
    happened. The message names the code point rather than printing the
    character, because the whole problem is that printing it shows nothing.
    """


def normalise_email(value: str) -> str:
    """Return the canonical dedupe key for an email address.

    Trim ASCII whitespace, require exactly one ``@`` with a non-empty local
    part and a domain containing a dot, refuse invisible characters, then
    fold ASCII case over the whole address. ``" DANA@Riverbend.example "``
    and ``"dana@riverbend.example"`` are the same contact.

    Folding the local part is a deliberate, documented choice. RFC 5321
    permits a case-sensitive local part; no mainstream mailbox provider
    honours that distinction, and a dedupe key that treats ``Dana@`` and
    ``dana@`` as two people creates the duplicate this package exists to
    prevent. The fold is ASCII-only for the reasons in the module
    docstring: the Kelvin-sign anchor test proves that ``U+212A`` and
    ``k`` remain two distinct keys here, where ``str.lower`` would merge
    them.
    """
    trimmed = value.strip(ASCII_WHITESPACE)
    if trimmed.count("@") != 1:
        raise InvalidEmail(f"an email address has exactly one @; got {trimmed.count('@')}")
    local, domain = trimmed.split("@")
    if not local:
        raise InvalidEmail("the local part before @ is empty")
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise InvalidEmail(f"the domain {domain!r} is not a plausible mail domain")
    for ch in trimmed:
        if ch.isspace() or not ch.isprintable():
            raise InvalidEmail(
                f"the address contains U+{ord(ch):04X}, an invisible or whitespace "
                "character; refusing to mint a dedupe key nobody can read"
            )
    return trimmed.translate(_ASCII_CASE_FOLD)


@dataclass(frozen=True)
class NormalisedPhone:
    """A phone number the rules fully determined, in E.164 form."""

    e164: str


@dataclass(frozen=True)
class PhoneNeedsReview:
    """A phone number the rules refuse to guess about.

    Carries the raw input and the named reason so a review queue can show
    a human exactly what arrived and why it was not merged automatically.
    """

    raw: str
    reason: str


PhoneResult = NormalisedPhone | PhoneNeedsReview


_REGION_PLANS: dict[str, tuple[str, int, bool]] = {
    "US": ("1", 10, False),
    "CA": ("1", 10, False),
    "GB": ("44", 10, True),
    "AU": ("61", 9, True),
    "BR": ("55", 10, False),
}
"""National numbering plans this package resolves without a country code.

Region code to (country calling code, national significant digits, whether
the national written form carries a trunk 0 that E.164 drops). BR is listed
with 10 digits (landline form); an 11-digit BR mobile with the leading 9
also resolves, handled explicitly below. A region outside this table with
no ``+`` prefix is a :class:`PhoneNeedsReview`, never a guess.
"""


def normalise_phone(value: str, *, default_region: str | None = None) -> PhoneResult:
    """Return the E.164 form of ``value``, or an explicit refusal.

    Deterministic rules, applied in order:

    - Formatting characters (spaces, dashes, dots, parentheses) are
      dropped; they carry presentation, not identity.
    - ``+`` followed by 8 to 15 digits is accepted as E.164 as given
      (a zero right after ``+`` is refused; no country code starts with 0).
    - ``00`` is the international call prefix and converts to ``+``.
    - A bare national number resolves only through ``default_region``
      (the sub-account's country setting) and the numbering-plan table
      above. No region, or a region outside the table, or a digit count
      the plan does not admit: :class:`PhoneNeedsReview` with the reason
      named. Guessing a country code merges strangers.
    """
    trimmed = value.strip(ASCII_WHITESPACE)
    if not trimmed:
        return PhoneNeedsReview(raw=value, reason="empty after trimming whitespace")

    plus = trimmed.startswith("+")
    body = trimmed[1:] if plus else trimmed
    digits: list[str] = []
    for ch in body:
        if ch in _DIAL_DIGITS:
            digits.append(ch)
        elif ch in _PHONE_FORMATTING:
            continue
        else:
            return PhoneNeedsReview(
                raw=value,
                reason=f"contains U+{ord(ch):04X} {ch!r}, which is neither a digit "
                "nor phone formatting",
            )
    number = "".join(digits)

    if not plus and number.startswith("00"):
        plus = True
        number = number[2:]

    if plus:
        if not number:
            return PhoneNeedsReview(raw=value, reason="a + with no digits after it")
        if number.startswith("0"):
            return PhoneNeedsReview(
                raw=value, reason="no country calling code starts with 0"
            )
        if not 8 <= len(number) <= 15:
            return PhoneNeedsReview(
                raw=value,
                reason=f"{len(number)} digits after +; E.164 admits 8 to 15",
            )
        return NormalisedPhone(e164=f"+{number}")

    if default_region is None:
        return PhoneNeedsReview(
            raw=value,
            reason="no country code on the number and no default region on the "
            "location; guessing a country merges strangers",
        )
    plan = _REGION_PLANS.get(default_region.upper())
    if plan is None:
        return PhoneNeedsReview(
            raw=value,
            reason=f"default region {default_region!r} has no numbering plan in "
            "this package; add it explicitly rather than guessing",
        )
    country_code, national_len, trunk_zero = plan

    if trunk_zero and number.startswith("0") and len(number) == national_len + 1:
        number = number[1:]
    if default_region.upper() == "BR" and len(number) == 11 and number[2] == "9":
        return NormalisedPhone(e164=f"+{country_code}{number}")
    if len(number) == national_len:
        return NormalisedPhone(e164=f"+{country_code}{number}")
    if len(number) == national_len + len(country_code) and number.startswith(country_code):
        return NormalisedPhone(e164=f"+{number}")
    return PhoneNeedsReview(
        raw=value,
        reason=f"{len(number)} digits does not fit the {default_region.upper()} "
        f"numbering plan ({national_len} national digits)",
    )
