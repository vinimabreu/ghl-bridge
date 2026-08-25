"""The offline demo: one location, one afternoon, every decision named.

Everything here runs against the deterministic fakes that ship in
``ghl_bridge.fakes``: a fake HighLevel workspace modelling the documented
API 2.0 semantics and a scripted clock. No account, no card, no key, no
network. The generator is a template keyed on the same intent rules the
gate reads, which is the honest thing for a demo about *what happens to a
draft* to do; any callable from request to text plugs into the same seat.

Run it::

    python -m examples.bridge_demo

The scenario: Riverbend Detailing answers customers Monday to Friday,
09:00 to 18:00, Chicago time. A form lead arrives by webhook and turns out
to be an existing customer under a shoutier spelling of her email. She
asks a scheduling question at 14:03 and the reply leaves on its own,
inside policy. She asks another at 21:40 and that reply waits for a named
human. The lead webhook is then redelivered and changes nothing. A burst
of paced calls shows the rate limiter holding the line. The ledger closes
by answering the operator's question: why did that message send itself at
14:03?
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ghl_bridge import (
    AuditLedger,
    Bridge,
    BusinessHours,
    CalendarSlot,
    ContactDeduper,
    ContactUpsert,
    Duplicate,
    GenerationRequest,
    HmacSha256Scheme,
    LedgerRecord,
    Location,
    LocationPacer,
    PacedCaller,
    PacedPort,
    Pipeline,
    PipelineStage,
    PolicyGate,
    WebhookIntake,
    rule_intent,
)
from ghl_bridge.fakes import FakeClock, FakeHighLevel
from ghl_bridge.guard import ApprovedSender

WIDTH = 96
T0 = datetime(2026, 3, 3, 20, 0, tzinfo=UTC)  # Tuesday 14:00 in Chicago
CHICAGO = ZoneInfo("America/Chicago")
LOCATION_ID = "loc-riverbend"
PIPELINE_ID = "pipe-sales"
SECRET = b"riverbend-webhook-secret"


def template_generator(request: GenerationRequest) -> str:
    first = request.contact.first_name or "there"
    intent = rule_intent(request.inbound_text)
    if intent == "scheduling":
        return (
            f"Hi {first}, the next open detailing slots are Thursday morning. "
            "Reply with a time that suits you and I will book it."
        )
    if intent == "hours":
        return f"Hi {first}, the shop is open Monday to Friday, 9am to 6pm."
    return f"Hi {first}, thanks for reaching out. How can I help?"


def rule(title: str = "") -> None:
    if title:
        print(title)
    print("-" * WIDTH)


def local(at: datetime) -> str:
    return at.astimezone(CHICAGO).strftime("%H:%M")


def sign(body: dict[str, object]) -> tuple[bytes, str]:
    raw = json.dumps(body, sort_keys=True).encode()
    return raw, HmacSha256Scheme(secret=SECRET).sign(raw)


def evaluated_policies(record: LedgerRecord) -> list[dict[str, object]]:
    """The gate's per-policy results off a gate_decision ledger record,
    with the shape asserted rather than assumed."""
    evaluated = record.detail.get("evaluated")
    assert isinstance(evaluated, list)
    return [item for item in evaluated if isinstance(item, dict)]


def describe(record: LedgerRecord) -> str:
    detail = record.detail
    if record.kind == "webhook_received":
        return f"event {detail['event_key']} via delivery {detail['delivery_id']}"
    if record.kind == "draft_generated":
        return f"{detail['chars']} chars drafted for {detail['contact_id']}"
    if record.kind == "gate_decision":
        failed = detail["failed"]
        verdict = "all policies passed" if not failed else f"failed: {failed}"
        return f"{detail['decision_id']} -> {detail['outcome']} ({verdict})"
    if record.kind == "message_sent":
        return (
            f"{detail['message_id']} to {detail['contact_id']} "
            f"under {detail['mode']} approval"
        )
    return str(dict(detail))


def main(argv: list[str] | None = None) -> None:
    del argv  # no flags; one deterministic story
    clock = FakeClock(T0)
    location = Location(
        location_id=LOCATION_ID,
        name="Riverbend Detailing",
        timezone="America/Chicago",
        default_region="US",
        business_hours=BusinessHours(
            days=frozenset({0, 1, 2, 3, 4}),
            open_time=datetime(2026, 1, 1, 9, 0).time(),
            close_time=datetime(2026, 1, 1, 18, 0).time(),
        ),
    )

    server = FakeHighLevel(clock=clock)
    server.add_location(location)
    server.add_pipeline(
        LOCATION_ID,
        Pipeline(
            pipeline_id=PIPELINE_ID,
            name="Sales",
            stages=(
                PipelineStage(stage_id="stg-new", name="New Lead", position=1),
                PipelineStage(stage_id="stg-qualified", name="Qualified", position=2),
                PipelineStage(stage_id="stg-booked", name="Booked", position=3),
            ),
        ),
    )
    thursday = datetime(2026, 3, 5, 15, 0, tzinfo=UTC)
    server.add_calendar_slots(
        LOCATION_ID,
        "cal-detail",
        [
            CalendarSlot(
                calendar_id="cal-detail",
                start=thursday.replace(hour=15 + i),
                end=thursday.replace(hour=16 + i),
            )
            for i in range(3)
        ],
    )
    dana = server.seed_contact(
        LOCATION_ID,
        ContactUpsert(
            first_name="Dana",
            last_name="Whitfield",
            email="dana@riverbend.example",
            phone="+15005550100",
            source="walk-in",
        ),
    )

    token = server.issue_private_token(LOCATION_ID)
    port = server.port_for(token)
    ledger = AuditLedger()
    gate = PolicyGate(clock=clock)
    sender = ApprovedSender(port=port, ledger=ledger, clock=clock)
    deduper = ContactDeduper(port=port, ledger=ledger, clock=clock)
    bridge = Bridge(
        location=location,
        port=port,
        deduper=deduper,
        gate=gate,
        sender=sender,
        generator=template_generator,
        ledger=ledger,
        clock=clock,
        lead_pipeline_id=PIPELINE_ID,
        lead_stage_id="stg-new",
    )
    intake = WebhookIntake(
        scheme=HmacSha256Scheme(secret=SECRET),
        handler=bridge.handle_event,
        ledger=ledger,
        clock=clock,
    )

    print("ghl-bridge  |  policy-gated CRM automation, proven offline against the fake workspace")
    print("=" * WIDTH)
    print(
        f"Location {location.name} ({LOCATION_ID}), {location.timezone}, "
        "answers Mon-Fri 09:00-18:00. All data synthetic."
    )
    print(
        "Pipeline Sales: New Lead -> Qualified -> Booked. "
        f"Existing contact {dana.contact_id}: Dana Whitfield, "
        "dana@riverbend.example, +15005550100."
    )
    print(
        f"Private Integration token {token} is scoped to {LOCATION_ID}; "
        "every call below rides it."
    )
    print()

    # ---------------------------------------------------------- 1. the lead
    rule(f"1. {local(clock())}, a form lead arrives by webhook, shoutier spelling and all")
    lead_email = " DANA@Riverbend.example "
    lead_body: dict[str, object] = {
        "event_id": "evt-lead-001",
        "event_type": "ContactCreate",
        "location_id": LOCATION_ID,
        "resource_id": None,
        "occurred_at": clock().isoformat(),
        "payload": {
            "first_name": "Dana",
            "last_name": "Whitfield",
            "email": lead_email,
            "source": "website form",
        },
    }
    raw, signature = sign(lead_body)
    intake.receive(raw, signature=signature, delivery_id="dlv-001")
    merged = ledger.of_kind("contact_merged")[0]
    opportunity = ledger.of_kind("opportunity_created")[0]
    print(f"payload email:  {lead_email!r}")
    print(f"dedupe key:     {merged.detail['key']!r}  (ASCII fold + trim, never str.lower)")
    print(
        f"decision:       merged into existing {merged.detail['contact_id']} "
        f"on {merged.detail['matched_on']}; no duplicate contact created"
    )
    print(
        f"opportunity:    {opportunity.detail['opportunity_id']} filed in "
        f"stage 'New Lead' of pipeline Sales"
    )
    print()

    # ------------------------------------------- 2. 14:03, inside policy
    clock.advance(180)
    rule(f"2. {local(clock())}, Dana asks a scheduling question, inside business hours")
    server.seed_inbound(
        LOCATION_ID, dana.contact_id, "What times do you have on Thursday?", clock()
    )
    inbound_body_1: dict[str, object] = {
        "event_id": "evt-msg-001",
        "event_type": "InboundMessage",
        "location_id": LOCATION_ID,
        "resource_id": "msg-0001",
        "occurred_at": clock().isoformat(),
        "payload": {
            "contact_id": dana.contact_id,
            "body": "What times do you have on Thursday?",
        },
    }
    raw, signature = sign(inbound_body_1)
    intake.receive(raw, signature=signature, delivery_id="dlv-002")
    decision_1 = ledger.of_kind("gate_decision")[0]
    sent_1 = ledger.of_kind("message_sent")[0]
    print('inbound:  "What times do you have on Thursday?"')
    draft_text = template_generator(
        GenerationRequest(
            location=location,
            contact=dana,
            inbound_text="What times do you have on Thursday?",
        )
    )
    print(f'draft:    "{draft_text}"')
    print("the gate evaluates every policy, and every result goes on the record:")
    for item in evaluated_policies(decision_1):
        verdict = "pass" if item["passed"] else "FAIL"
        print(f"  {str(item['name']):<28} {verdict:<6} {item['detail']}")
    print(
        f"outcome:  AUTO_SEND under approval {decision_1.detail['decision_id']}; "
        f"{sent_1.detail['message_id']} left for {sent_1.detail['contact_id']}"
    )
    print()

    # ------------------------------------------ 3. 21:40, outside hours
    clock.advance(27420)
    rule(f"3. {local(clock())}, another question, outside business hours")
    server.seed_inbound(
        LOCATION_ID, dana.contact_id, "can you fit me in tomorrow?", clock()
    )
    inbound_body_2: dict[str, object] = {
        "event_id": "evt-msg-002",
        "event_type": "InboundMessage",
        "location_id": LOCATION_ID,
        "resource_id": "msg-0003",
        "occurred_at": clock().isoformat(),
        "payload": {
            "contact_id": dana.contact_id,
            "body": "can you fit me in tomorrow?",
        },
    }
    raw, signature = sign(inbound_body_2)
    intake.receive(raw, signature=signature, delivery_id="dlv-003")
    drafted = ledger.of_kind("message_drafted")[0]
    hours_result = next(
        item
        for item in evaluated_policies(ledger.of_kind("gate_decision")[1])
        if item["name"] == "within_business_hours"
    )
    print('inbound:  "can you fit me in tomorrow?"')
    print(f"outcome:  DRAFT_FOR_HUMAN, reason named: {drafted.detail['reasons']}")
    print(f"          {hours_result['detail']}")
    print(f"parked:   decision {drafted.detail['decision_id']} waits in the review queue")
    release = bridge.release_draft(
        str(drafted.detail["decision_id"]), approver="sam@riverbend.example"
    )
    sent_2 = ledger.of_kind("message_sent")[1]
    print(
        f"released: sam@riverbend.example approved it unchanged; {release.message_id} "
        f"left under a {sent_2.detail['mode']} approval"
    )
    print()

    # ------------------------------------------------- 4. the replay
    clock.advance(60)
    rule(
        f"4. {local(clock())}, the lead webhook is redelivered "
        "(sender timeout, fresh delivery id)"
    )
    raw, signature = sign(lead_body)
    result = intake.receive(raw, signature=signature, delivery_id="dlv-004")
    assert isinstance(result, Duplicate)
    print(
        f"delivery dlv-004 carries the same event {result.key}; "
        f"first processed under {result.first_delivery_id}"
    )
    print(
        f"effects:  contacts merged {len(ledger.of_kind('contact_merged'))}, "
        f"opportunities created {len(ledger.of_kind('opportunity_created'))} "
        "(both unchanged; idempotency keys on the EVENT, never the delivery)"
    )
    print()

    # ------------------------------------------------ 5. rate discipline
    rule("5. rate discipline, limits scaled down (burst 4 per 10s) so the arithmetic is visible")
    sandbox = FakeHighLevel(clock=clock, burst_limit=4)
    sandbox.add_location(
        location.model_copy(update={"location_id": "loc-sandbox"})
    )
    sandbox_token = sandbox.issue_private_token("loc-sandbox")
    waits: list[float] = []

    def sleeper(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)

    paced = PacedPort(
        inner=sandbox.port_for(sandbox_token),
        caller=PacedCaller(
            pacer=LocationPacer(clock=clock, burst_limit=4),
            sleeper=sleeper,
            ledger=ledger,
            clock=clock,
        ),
    )
    start = clock()
    for call_number in range(1, 11):
        before = len(waits)
        paced.list_pipelines("loc-sandbox")
        waited = waits[before] if len(waits) > before else 0.0
        print(f"  call {call_number:>2}: waited {waited:>5.1f}s before sending")
    elapsed = (clock() - start).total_seconds()
    print(
        f"ten calls against a burst of four: {len(waits)} computed waits, "
        f"{elapsed:.1f}s of scripted clock, zero 429s surfaced to the caller"
    )
    print()

    # ------------------------------------------------- 6. the audit answer
    message_id = str(sent_1.detail["message_id"])
    rule(f"6. the audit answer: why did {message_id} leave on its own at 14:03?")
    for record in ledger.explain_message(message_id):
        print(f"  {local(record.at)}  {record.kind:<18} {describe(record)}")
    print()
    print(
        f"records: {len(ledger.records())}    "
        f"guard breaches: {len(ledger.of_kind('guard_breach'))}    "
        f"pending drafts: {len(bridge.pending_drafts())}"
    )


if __name__ == "__main__":
    main()
