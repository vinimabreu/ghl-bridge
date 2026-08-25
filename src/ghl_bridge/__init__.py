"""ghl-bridge: a policy-gated bridge between a CRM location and a generator.

A CRM automation that guesses is a liability. This package is the set of
refusals that make one trustworthy: dedupe keys that cannot merge
strangers, webhook intake that cannot double-apply a redelivered event, a
policy gate that decides whether a draft leaves on its own or waits for a
named human, a guard that stops any outbound the gate never saw, pacing
that respects the platform's published limits, and a ledger that answers
"why did that message send at 14:03" with the full decision path.

The public names below are the intended surface; everything else is
implementation.
"""

from .bridge import (
    EVENT_INBOUND_MESSAGE,
    EVENT_NEW_LEAD,
    Bridge,
    GenerationRequest,
    Generator,
    PendingDraft,
)
from .clock import Clock, require_aware, system_clock
from .dedupe import ContactDeduper, DedupeResult, NeedsHumanReview, Resolved
from .guard import ApprovedSender, UnapprovedOutbound, content_fingerprint
from .identity import (
    InvalidEmail,
    NormalisedPhone,
    PhoneNeedsReview,
    PhoneResult,
    normalise_email,
    normalise_phone,
)
from .ledger import AuditLedger, LedgerRecord
from .limits import BURST_LIMIT, BURST_WINDOW_SECONDS, DAILY_LIMIT
from .models import (
    Appointment,
    Approval,
    BookingRequest,
    BusinessHours,
    CalendarSlot,
    Contact,
    ContactUpsert,
    Conversation,
    Location,
    Message,
    MessageDirection,
    Opportunity,
    OpportunityCreate,
    OutboundSend,
    Pipeline,
    PipelineStage,
    WebhookEvent,
    WebhookRegistration,
    WebhookSubscription,
)
from .policy import (
    GateDecision,
    Outcome,
    PolicyGate,
    PolicyResult,
    approve_draft,
    rule_intent,
)
from .ports import (
    CrossLocationDenied,
    HighLevelError,
    HighLevelPort,
    InvalidRequest,
    NotFound,
    RateLimited,
    SlotTaken,
    StageNotInPipeline,
    Unauthorized,
)
from .ratelimit import (
    LocationPacer,
    PacedCaller,
    PacedPort,
    RetryBudgetExhausted,
    Sleeper,
    system_sleeper,
)
from .webhook import (
    Accepted,
    Duplicate,
    HmacSha256Scheme,
    IntakeResult,
    Rejected,
    SignatureRejected,
    SignatureScheme,
    WebhookIntake,
    event_key,
)

__all__ = [
    "BURST_LIMIT",
    "BURST_WINDOW_SECONDS",
    "DAILY_LIMIT",
    "EVENT_INBOUND_MESSAGE",
    "EVENT_NEW_LEAD",
    "Accepted",
    "Appointment",
    "Approval",
    "ApprovedSender",
    "AuditLedger",
    "BookingRequest",
    "Bridge",
    "BusinessHours",
    "CalendarSlot",
    "Clock",
    "Contact",
    "ContactDeduper",
    "ContactUpsert",
    "Conversation",
    "CrossLocationDenied",
    "DedupeResult",
    "Duplicate",
    "GateDecision",
    "GenerationRequest",
    "Generator",
    "HighLevelError",
    "HighLevelPort",
    "HmacSha256Scheme",
    "IntakeResult",
    "InvalidEmail",
    "InvalidRequest",
    "LedgerRecord",
    "Location",
    "LocationPacer",
    "Message",
    "MessageDirection",
    "NeedsHumanReview",
    "NormalisedPhone",
    "NotFound",
    "Opportunity",
    "OpportunityCreate",
    "Outcome",
    "OutboundSend",
    "PacedCaller",
    "PacedPort",
    "PendingDraft",
    "PhoneNeedsReview",
    "PhoneResult",
    "Pipeline",
    "PipelineStage",
    "PolicyGate",
    "PolicyResult",
    "RateLimited",
    "Rejected",
    "Resolved",
    "RetryBudgetExhausted",
    "SignatureRejected",
    "SignatureScheme",
    "SlotTaken",
    "Sleeper",
    "StageNotInPipeline",
    "Unauthorized",
    "UnapprovedOutbound",
    "WebhookEvent",
    "WebhookIntake",
    "WebhookRegistration",
    "WebhookSubscription",
    "approve_draft",
    "content_fingerprint",
    "event_key",
    "normalise_email",
    "normalise_phone",
    "require_aware",
    "rule_intent",
    "system_clock",
    "system_sleeper",
]
