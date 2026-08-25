"""The demo is deterministic, honest, and pinned to the README.

The README pastes the demo output verbatim. These tests keep that section
true: the capture in the README is compared byte-for-byte against a fresh
run, so a change that moves a number fails CI instead of quietly turning
the documentation into fiction.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import re

import pytest
from examples.bridge_demo import main

README = pathlib.Path(__file__).resolve().parent.parent / "README.md"


def run_demo() -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        main([])
    return buffer.getvalue()


@pytest.fixture(scope="module")
def output() -> str:
    return run_demo()


def test_the_demo_is_byte_for_byte_deterministic(output: str) -> None:
    assert run_demo() == output


def test_the_readme_block_is_a_verbatim_capture_of_the_demo(output: str) -> None:
    text = README.read_text()
    match = re.search(r"<!-- demo:begin -->\n```\n(.*?)\n```\n<!-- demo:end -->", text, re.DOTALL)
    assert match is not None, "the README demo markers are missing"
    assert match.group(1) == output.rstrip("\n")


def test_the_lead_merges_instead_of_duplicating(output: str) -> None:
    section = output.split("1. 14:00", 1)[1].split("2. 14:03", 1)[0]
    assert "merged into existing con-0001 on email" in section
    assert "'dana@riverbend.example'" in section


def test_the_opportunity_lands_in_the_named_stage(output: str) -> None:
    assert "opp-0001 filed in stage 'New Lead'" in output


def test_the_afternoon_reply_auto_sends_with_every_policy_on_the_record(output: str) -> None:
    section = output.split("2. 14:03", 1)[1].split("3. 21:40", 1)[0]
    assert "AUTO_SEND" in section
    for policy in (
        "contact_not_opted_out",
        "within_business_hours",
        "no_price_commitment",
        "draft_length",
    ):
        assert policy in section
    assert "FAIL" not in section


def test_the_evening_reply_parks_with_the_reason_named(output: str) -> None:
    section = output.split("3. 21:40", 1)[1].split("4. 21:41", 1)[0]
    assert "DRAFT_FOR_HUMAN" in section
    assert "within_business_hours" in section
    assert "outside the answering window" in section


def test_the_evening_release_is_a_named_human(output: str) -> None:
    assert "sam@riverbend.example approved it unchanged" in output
    assert "under a human approval" in output


def test_the_replay_changes_nothing(output: str) -> None:
    section = output.split("4. 21:41", 1)[1].split("5. rate", 1)[0]
    assert "contacts merged 1" in section
    assert "opportunities created 1" in section
    assert "never the delivery" in section


def test_the_burst_shows_computed_waits_and_no_429(output: str) -> None:
    section = output.split("5. rate", 1)[1].split("6. the audit", 1)[0]
    assert "call  5: waited  10.0s" in section
    assert "call  9: waited  10.0s" in section
    assert "zero 429s surfaced to the caller" in section


def test_the_audit_chain_answers_the_1403_question(output: str) -> None:
    section = output.split("6. the audit", 1)[1]
    for kind in ("webhook_received", "draft_generated", "gate_decision", "message_sent"):
        assert kind in section


def test_the_run_ends_with_zero_guard_breaches(output: str) -> None:
    assert "guard breaches: 0" in output
