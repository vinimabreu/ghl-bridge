"""The policy gate: the decision between a draft and a customer.

The generator is an injected callable and this package holds no opinion
about what produces the draft. It holds a strong opinion about what
happens next: a draft leaves on its own only when every policy passes,
and every other outcome is named. The three outcomes are deliberate:

- ``AUTO_SEND``: all policies passed; the gate mints the approval itself
  and the ledger shows exactly which checks vouched for the send.
- ``DRAFT_FOR_HUMAN``: nothing dangerous, but outside what the automation
  is trusted to do alone (after hours, an uncovered intent, money talk, a
  draft too long). The draft is parked with the failing policies named,
  and :func:`approve_draft` is the human path to release it unchanged.
- ``BLOCKED``: the draft must not leave at all (a reply to an opt-out, a
  request for payment details, an empty body). Escalating a block to a
  human "just to be safe" would train people to approve blocks.

Every policy is deterministic: string rules and clock arithmetic, no
model in the loop. A safety decision that needs a second model to check
the first model is a coin flip stacked on a coin flip. The full result
list, passes included, goes on the decision and into the ledger, because
"which checks did this send clear" is an audit question and recomputing
it later against changed rules is not an answer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .clock import Clock
from .guard import content_fingerprint
from .models import Approval, Contact, Location, OutboundSend


class Outcome(StrEnum):
    AUTO_SEND = "auto_send"
    DRAFT_FOR_HUMAN = "draft_for_human"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class PolicyResult:
    """One policy's verdict on one draft. ``severity`` states what a
    failure means: ``"block"`` refuses the content, ``"draft"`` demands a
    human. A pass carries the severity too, so the audit line shows what
    the check would have done."""

    name: str
    passed: bool
    severity: str
    detail: str


@dataclass(frozen=True)
class GateDecision:
    decision_id: str
    outcome: Outcome
    results: tuple[PolicyResult, ...]
    draft: OutboundSend

    @property
    def failed(self) -> tuple[PolicyResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.failed)


IntentClassifier = Callable[[str], str]
"""Maps an inbound text to an intent name. Deterministic by contract."""

_OPT_OUT = re.compile(r"\b(stop|unsubscribe|opt out)\b", re.IGNORECASE)
_SCHEDULING = re.compile(
    r"\b(book|schedule|reschedule|appointment|slot|time|available|availability|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today)\b",
    re.IGNORECASE,
)
_PRICING = re.compile(
    r"\b(price|prices|pricing|cost|costs|quote|charge|charges|discount|how much)\b",
    re.IGNORECASE,
)
_HOURS = re.compile(r"\b(hours|open|opening|closed|closing|address|located|where)\b", re.IGNORECASE)


def rule_intent(text: str) -> str:
    """The default classifier: ordered keyword rules, opt-out first because
    it must win every tie."""
    if _OPT_OUT.search(text):
        return "opt_out"
    if _SCHEDULING.search(text):
        return "scheduling"
    if _PRICING.search(text):
        return "pricing"
    if _HOURS.search(text):
        return "hours"
    return "general"


_MONEY = re.compile(
    r"([$£€]\s?\d|\d+\s?(%|percent)|\b\d+\s?(dollars|pounds|euros)\b)",
    re.IGNORECASE,
)
_PROMISE = re.compile(
    r"\b(guarantee|guaranteed|promise|refund|free of charge|no charge|price match)\b",
    re.IGNORECASE,
)
_PAYMENT_DETAILS = re.compile(
    r"\b(card number|cvv|cvc|security code|routing number|account number|"
    r"social security|ssn)\b",
    re.IGNORECASE,
)


class PolicyGate:
    """Evaluates every policy, then decides. Order of decision, not of
    evaluation: any failed block policy makes the outcome ``BLOCKED``;
    otherwise any failed draft policy makes it ``DRAFT_FOR_HUMAN``;
    otherwise ``AUTO_SEND`` with the approval minted here."""

    def __init__(
        self,
        *,
        clock: Clock,
        covered_intents: frozenset[str] = frozenset({"scheduling", "hours", "general"}),
        max_draft_chars: int = 640,
        intent_classifier: IntentClassifier = rule_intent,
    ) -> None:
        if max_draft_chars < 1:
            raise ValueError("max_draft_chars must be positive")
        self._clock = clock
        self._covered = covered_intents
        self._max_chars = max_draft_chars
        self._intent = intent_classifier
        self._decisions = 0

    def evaluate(
        self,
        *,
        location: Location,
        contact: Contact,
        inbound_text: str,
        draft: OutboundSend,
    ) -> GateDecision:
        now_local = location.local_now(self._clock())
        intent = self._intent(inbound_text)
        results: list[PolicyResult] = []

        results.append(
            PolicyResult(
                name="contact_not_opted_out",
                passed=not contact.opted_out,
                severity="block",
                detail="the contact asked not to be messaged"
                if contact.opted_out
                else "the contact accepts messages",
            )
        )
        results.append(
            PolicyResult(
                name="not_a_reply_to_opt_out",
                passed=intent != "opt_out",
                severity="block",
                detail="the inbound is an opt-out request; the only correct "
                "reply is the platform's own confirmation"
                if intent == "opt_out"
                else f"inbound intent is {intent!r}",
            )
        )
        empty = not draft.body.strip()
        results.append(
            PolicyResult(
                name="draft_not_empty",
                passed=not empty,
                severity="block",
                detail="an empty draft is a generator failure, not a message"
                if empty
                else "the draft has content",
            )
        )
        payment = _PAYMENT_DETAILS.search(draft.body)
        results.append(
            PolicyResult(
                name="no_payment_details_request",
                passed=payment is None,
                severity="block",
                detail=f"the draft asks for {payment.group(0)!r} over SMS"
                if payment
                else "the draft asks for no payment details",
            )
        )

        inside = location.business_hours.contains(now_local)
        results.append(
            PolicyResult(
                name="within_business_hours",
                passed=inside,
                severity="draft",
                detail=f"local time is {now_local.strftime('%a %H:%M')} "
                f"({location.timezone}); "
                + ("inside the answering window" if inside else "outside the answering window"),
            )
        )
        results.append(
            PolicyResult(
                name="intent_covered",
                passed=intent in self._covered,
                severity="draft",
                detail=f"intent {intent!r} "
                + (
                    "is on the covered list"
                    if intent in self._covered
                    else "is not covered for automatic replies"
                ),
            )
        )
        money = _MONEY.search(draft.body)
        promise = _PROMISE.search(draft.body)
        offender = money or promise
        results.append(
            PolicyResult(
                name="no_price_commitment",
                passed=offender is None,
                severity="draft",
                detail=f"the draft commits to {offender.group(0)!r}; money talk "
                "is a human's call"
                if offender
                else "the draft makes no price commitment",
            )
        )
        results.append(
            PolicyResult(
                name="draft_length",
                passed=len(draft.body) <= self._max_chars,
                severity="draft",
                detail=f"{len(draft.body)} chars against a limit of {self._max_chars}",
            )
        )

        self._decisions += 1
        decision_id = f"dec-{self._decisions:04d}"
        failed = [r for r in results if not r.passed]
        if any(r.severity == "block" for r in failed):
            outcome = Outcome.BLOCKED
        elif failed:
            outcome = Outcome.DRAFT_FOR_HUMAN
        else:
            outcome = Outcome.AUTO_SEND
        return GateDecision(
            decision_id=decision_id,
            outcome=outcome,
            results=tuple(results),
            draft=draft,
        )

    def approval_for(self, decision: GateDecision) -> Approval:
        """The auto approval, minted only for an ``AUTO_SEND`` decision and
        bound to the exact draft the policies saw."""
        if decision.outcome is not Outcome.AUTO_SEND:
            raise ValueError(
                f"decision {decision.decision_id} is {decision.outcome.value}, "
                "not auto_send; the gate does not mint approvals for it"
            )
        passed = ",".join(r.name for r in decision.results if r.passed)
        return Approval(
            decision_id=decision.decision_id,
            mode="auto",
            approved_by=f"policy_gate[{passed}]",
            content_sha256=content_fingerprint(decision.draft),
            at=self._clock(),
        )


def approve_draft(
    decision: GateDecision, *, approver: str, clock: Clock
) -> Approval:
    """The human path: release a parked draft unchanged, under a name.

    Refuses a blocked decision. A block is not a stricter draft; it is the
    gate saying this content must not leave, and a release path for blocks
    would turn every block into a nag that eventually gets clicked through.
    """
    if decision.outcome is Outcome.BLOCKED:
        raise ValueError(
            f"decision {decision.decision_id} is blocked "
            f"({', '.join(decision.reasons)}); a human release path for "
            "blocked content is not offered"
        )
    if not approver.strip():
        raise ValueError("an approval needs a named approver")
    return Approval(
        decision_id=decision.decision_id,
        mode="human",
        approved_by=approver,
        content_sha256=content_fingerprint(decision.draft),
        at=clock(),
    )
