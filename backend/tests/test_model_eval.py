"""The evaluation cannot mutate scheduling state or accept malformed tool calls."""

import json

import pytest

from evals.scheduling_messages import CASES, TOOL, case_passes, parse_proposal, request_payload


def test_every_case_uses_only_a_read_only_proposal_tool() -> None:
    assert len(CASES) >= 6
    assert all(case.actor in {"client", "owner"} for case in CASES)
    for case in CASES:
        payload = request_payload(case)
        assert payload["tools"] == [TOOL]
        assert payload["tool_choice"] == {"type": "function", "name": "propose_message"}
        assert payload["store"] is False
        assert "write" not in TOOL["name"]


def test_rejects_missing_or_multiple_tool_proposals() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        parse_proposal({"output": []})
    with pytest.raises(ValueError, match="exactly one"):
        parse_proposal({"output": [{"type": "function_call", "name": "propose_message"}] * 2})


def test_clarification_requires_a_question() -> None:
    proposal = {
        "intent": "clarify", "request_reference": None, "date_text": None,
        "owner_decision": None, "needs_clarification": True, "question": None,
    }
    with pytest.raises(ValueError, match="needs a question"):
        parse_proposal({"output": [{"type": "function_call", "name": "propose_message",
                                    "arguments": json.dumps(proposal)}]})


def test_ambiguous_owner_reply_cannot_pass_with_a_proposed_approval() -> None:
    case = next(case for case in CASES if case.name == "owner-yes-two-requests")
    proposal = {
        "intent": "clarify", "request_reference": None, "date_text": None,
        "owner_decision": None, "needs_clarification": True,
        "question": "Which request?",
    }
    assert case_passes(case, proposal)
    assert not case_passes(case, {**proposal, "owner_decision": "approve"})
    assert not case_passes(case, {**proposal, "request_reference": "A-101"})


def test_exact_owner_reference_needs_correct_intent_target_and_decision() -> None:
    case = next(case for case in CASES if case.name == "explicit-owner-reference")
    proposal = {
        "intent": "owner_decision", "request_reference": "A-101", "date_text": None,
        "owner_decision": "approve", "needs_clarification": False, "question": None,
    }
    assert case_passes(case, proposal)
    assert not case_passes(case, {**proposal, "intent": "unsupported"})
    assert not case_passes(case, {**proposal, "request_reference": "B-202"})
