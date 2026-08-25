"""The gate: three outcomes, every reason named, no model in the loop."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import LOCATION_ID, T0, build_location

from ghl_bridge import (
    Contact,
    Location,
    OutboundSend,
    Outcome,
    PolicyGate,
    approve_draft,
    content_fingerprint,
    rule_intent,
)
from ghl_bridge.fakes import FakeClock

DANA = Contact(contact_id="con-1", location_id=LOCATION_ID, first_name="Dana")
OPTED_OUT = Contact(
    contact_id="con-2", location_id=LOCATION_ID, first_name="Sam", opted_out=True
)

SAFE_DRAFT = OutboundSend(
    contact_id="con-1",
    body="Hi Dana, the next open detailing slots are Thursday morning.",
)
SCHEDULING_INBOUND = "What times do you have on Thursday?"


def gate_at(clock: FakeClock, **kwargs: object) -> PolicyGate:
    return PolicyGate(clock=clock, **kwargs)  # type: ignore[arg-type]


def decide(
    gate: PolicyGate,
    *,
    location: Location | None = None,
    contact: Contact = DANA,
    inbound: str = SCHEDULING_INBOUND,
    draft: OutboundSend = SAFE_DRAFT,
):
    return gate.evaluate(
        location=location or build_location(),
        contact=contact,
        inbound_text=inbound,
        draft=draft,
    )


# ---------------------------------------------------------------- auto send


def test_a_safe_draft_inside_hours_auto_sends() -> None:
    decision = decide(gate_at(FakeClock(T0)))
    assert decision.outcome is Outcome.AUTO_SEND
    assert decision.reasons == ()


def test_the_decision_lists_every_policy_evaluated_passes_included() -> None:
    decision = decide(gate_at(FakeClock(T0)))
    names = [r.name for r in decision.results]
    assert names == [
        "contact_not_opted_out",
        "not_a_reply_to_opt_out",
        "draft_not_empty",
        "no_payment_details_request",
        "within_business_hours",
        "intent_covered",
        "no_price_commitment",
        "draft_length",
    ]
    assert all(r.passed for r in decision.results)


def test_decision_ids_are_sequential_per_gate() -> None:
    gate = gate_at(FakeClock(T0))
    first = decide(gate)
    second = decide(gate)
    assert (first.decision_id, second.decision_id) == ("dec-0001", "dec-0002")


# ------------------------------------------------------------ business hours


def test_after_hours_parks_the_draft_with_the_reason_named() -> None:
    clock = FakeClock(T0)
    clock.advance(8 * 3600)  # 22:00 in Chicago
    decision = decide(gate_at(clock))
    assert decision.outcome is Outcome.DRAFT_FOR_HUMAN
    assert decision.reasons == ("within_business_hours",)


def test_the_hours_detail_names_the_local_time_and_zone() -> None:
    clock = FakeClock(T0)
    clock.advance(8 * 3600)
    decision = decide(gate_at(clock))
    hours = next(r for r in decision.results if r.name == "within_business_hours")
    assert "America/Chicago" in hours.detail
    assert "22:00" in hours.detail


def test_saturday_is_after_hours_even_at_noon() -> None:
    saturday_noon_chicago = datetime(2026, 3, 7, 18, 0, tzinfo=UTC)
    decision = decide(gate_at(FakeClock(saturday_noon_chicago)))
    assert decision.outcome is Outcome.DRAFT_FOR_HUMAN
    assert "within_business_hours" in decision.reasons


def test_the_opening_instant_auto_sends() -> None:
    opening = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)  # 09:00 in Chicago
    decision = decide(gate_at(FakeClock(opening)))
    assert decision.outcome is Outcome.AUTO_SEND


def test_the_closing_instant_parks() -> None:
    closing = datetime(2026, 3, 4, 0, 0, tzinfo=UTC)  # 18:00 in Chicago
    decision = decide(gate_at(FakeClock(closing)))
    assert decision.outcome is Outcome.DRAFT_FOR_HUMAN


def test_one_second_before_closing_auto_sends() -> None:
    almost = datetime(2026, 3, 3, 23, 59, 59, tzinfo=UTC)
    decision = decide(gate_at(FakeClock(almost)))
    assert decision.outcome is Outcome.AUTO_SEND


# ------------------------------------------------------------------- intents


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("What times do you have on Thursday?", "scheduling"),
        ("can I book tomorrow", "scheduling"),
        ("need to reschedule my appointment", "scheduling"),
        ("how much for a full detail", "pricing"),
        ("what does it cost", "pricing"),
        ("any discount for two cars?", "pricing"),
        ("are you open today", "scheduling"),
        ("where are you located", "hours"),
        ("STOP", "opt_out"),
        ("stop texting me", "opt_out"),
        ("please unsubscribe", "opt_out"),
        ("thanks!", "general"),
    ],
)
def test_the_rule_classifier(text: str, intent: str) -> None:
    assert rule_intent(text) == intent


def test_opt_out_wins_every_tie() -> None:
    assert rule_intent("stop, but first how much does it cost") == "opt_out"


def test_an_uncovered_intent_parks_the_draft() -> None:
    decision = decide(gate_at(FakeClock(T0)), inbound="how much for a full detail")
    assert decision.outcome is Outcome.DRAFT_FOR_HUMAN
    assert "intent_covered" in decision.reasons


def test_covered_intents_are_configurable() -> None:
    generous = gate_at(
        FakeClock(T0), covered_intents=frozenset({"scheduling", "hours", "general", "pricing"})
    )
    decision = decide(generous, inbound="how much for a full detail")
    assert "intent_covered" not in decision.reasons


def test_a_custom_classifier_is_honoured() -> None:
    def everything_is_pricing(text: str) -> str:
        return "pricing"

    gate = gate_at(FakeClock(T0), intent_classifier=everything_is_pricing)
    decision = decide(gate)
    assert "intent_covered" in decision.reasons


# ------------------------------------------------------------- price and len


@pytest.mark.parametrize(
    "body",
    [
        "A full detail is $180.",
        "I can do 15% off this week.",
        "That runs about 200 dollars.",
        "It is guaranteed to pass inspection.",
        "I promise it will be ready by noon.",
        "Full refund if you are not satisfied.",
        "There is no charge for the estimate.",
    ],
)
def test_money_talk_parks_the_draft(body: str) -> None:
    decision = decide(
        gate_at(FakeClock(T0)),
        draft=OutboundSend(contact_id="con-1", body=body),
    )
    assert decision.outcome is Outcome.DRAFT_FOR_HUMAN
    assert "no_price_commitment" in decision.reasons


def test_the_price_detail_quotes_the_offending_fragment() -> None:
    decision = decide(
        gate_at(FakeClock(T0)),
        draft=OutboundSend(contact_id="con-1", body="A full detail is $180."),
    )
    price = next(r for r in decision.results if r.name == "no_price_commitment")
    assert "$1" in price.detail


def test_an_overlong_draft_parks() -> None:
    long_draft = OutboundSend(contact_id="con-1", body="a" * 641)
    decision = decide(gate_at(FakeClock(T0)), draft=long_draft)
    assert "draft_length" in decision.reasons


def test_the_length_limit_is_inclusive() -> None:
    exact = OutboundSend(contact_id="con-1", body="a" * 640)
    decision = decide(gate_at(FakeClock(T0)), draft=exact)
    assert "draft_length" not in decision.reasons


def test_the_length_limit_is_configurable() -> None:
    tight = gate_at(FakeClock(T0), max_draft_chars=10)
    decision = decide(tight)
    assert "draft_length" in decision.reasons


def test_a_nonpositive_length_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        PolicyGate(clock=FakeClock(T0), max_draft_chars=0)


# -------------------------------------------------------------------- blocks


def test_an_opted_out_contact_blocks() -> None:
    decision = decide(gate_at(FakeClock(T0)), contact=OPTED_OUT)
    assert decision.outcome is Outcome.BLOCKED
    assert "contact_not_opted_out" in decision.reasons


def test_replying_to_a_stop_blocks() -> None:
    decision = decide(gate_at(FakeClock(T0)), inbound="STOP")
    assert decision.outcome is Outcome.BLOCKED
    assert "not_a_reply_to_opt_out" in decision.reasons


def test_an_empty_draft_blocks() -> None:
    decision = decide(
        gate_at(FakeClock(T0)), draft=OutboundSend(contact_id="con-1", body="   ")
    )
    assert decision.outcome is Outcome.BLOCKED
    assert "draft_not_empty" in decision.reasons


@pytest.mark.parametrize(
    "body",
    [
        "Reply with your card number to hold the slot.",
        "What is the CVV on the back?",
        "Send over your routing number and account number.",
        "I need your social security number for the file.",
    ],
)
def test_asking_for_payment_details_blocks(body: str) -> None:
    decision = decide(
        gate_at(FakeClock(T0)), draft=OutboundSend(contact_id="con-1", body=body)
    )
    assert decision.outcome is Outcome.BLOCKED
    assert "no_payment_details_request" in decision.reasons


def test_a_block_outranks_any_number_of_draft_failures() -> None:
    """After hours AND money talk AND opted out: the outcome is BLOCKED,
    because escalating blockable content to a human queue normalises
    approving it."""
    clock = FakeClock(T0)
    clock.advance(8 * 3600)
    decision = decide(
        gate_at(clock),
        contact=OPTED_OUT,
        draft=OutboundSend(contact_id="con-2", body="It is $180, guaranteed."),
    )
    assert decision.outcome is Outcome.BLOCKED
    assert "contact_not_opted_out" in decision.reasons
    assert "within_business_hours" in decision.reasons  # still named for the audit


# ----------------------------------------------------------------- approvals


def test_the_auto_approval_binds_to_the_exact_draft() -> None:
    gate = gate_at(FakeClock(T0))
    decision = decide(gate)
    approval = gate.approval_for(decision)
    assert approval.mode == "auto"
    assert approval.content_sha256 == content_fingerprint(SAFE_DRAFT)


def test_the_auto_approval_names_the_policies_that_vouched() -> None:
    gate = gate_at(FakeClock(T0))
    approval = gate.approval_for(decide(gate))
    assert approval.approved_by.startswith("policy_gate[")
    assert "within_business_hours" in approval.approved_by


def test_the_gate_refuses_to_mint_an_approval_for_a_parked_draft() -> None:
    clock = FakeClock(T0)
    clock.advance(8 * 3600)
    gate = gate_at(clock)
    decision = decide(gate)
    with pytest.raises(ValueError, match="not auto_send"):
        gate.approval_for(decision)


def test_a_human_can_release_a_parked_draft_under_a_name() -> None:
    clock = FakeClock(T0)
    clock.advance(8 * 3600)
    gate = gate_at(clock)
    decision = decide(gate)
    approval = approve_draft(decision, approver="sam@riverbend.example", clock=clock)
    assert approval.mode == "human"
    assert approval.approved_by == "sam@riverbend.example"
    assert approval.content_sha256 == content_fingerprint(SAFE_DRAFT)


def test_a_blocked_decision_has_no_human_release_path() -> None:
    gate = gate_at(FakeClock(T0))
    decision = decide(gate, inbound="STOP")
    with pytest.raises(ValueError, match="no.*release|not offered|blocked"):
        approve_draft(decision, approver="sam", clock=FakeClock(T0))


def test_an_anonymous_approver_is_refused() -> None:
    clock = FakeClock(T0)
    clock.advance(8 * 3600)
    gate = gate_at(clock)
    decision = decide(gate)
    with pytest.raises(ValueError, match="named approver"):
        approve_draft(decision, approver="   ", clock=clock)
