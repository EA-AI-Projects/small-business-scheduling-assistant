"""The evaluation cannot mutate scheduling state or accept malformed tool calls."""

import json

import pytest

from evals.scheduling_messages import (
    CASES,
    TOOL,
    case_passes,
    interactive,
    parse_proposal,
    request_payload,
    safe_preview_message,
)


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
    assert not case_passes(case, {**proposal, "date_text": "2026-10-02T15:00:00"})


def test_exact_owner_reference_needs_correct_intent_target_and_decision() -> None:
    case = next(case for case in CASES if case.name == "explicit-owner-reference")
    proposal = {
        "intent": "owner_decision", "request_reference": "A-101", "date_text": None,
        "owner_decision": "approve", "needs_clarification": False, "question": None,
    }
    assert case_passes(case, proposal)
    assert not case_passes(case, {**proposal, "intent": "unsupported"})
    assert not case_passes(case, {**proposal, "request_reference": "B-202"})


def test_ambiguous_date_cannot_pass_with_a_guessed_calendar_time() -> None:
    case = next(case for case in CASES if case.name == "broad-window")
    proposal = {
        "intent": "clarify", "request_reference": None, "date_text": None,
        "owner_decision": None, "needs_clarification": True,
        "question": "What start time do you prefer?",
    }
    assert case_passes(case, proposal)
    assert not case_passes(case, {**proposal, "date_text": "2026-10-02 15:00"})
    assert not case_passes(case, {
        **proposal, "question": "Do you mean October 2 or October 9?",
    })


def test_preview_rejects_recognizable_sensitive_input() -> None:
    assert safe_preview_message("Can I book next Friday afternoon?")
    assert not safe_preview_message("Call me at (415) 555-0123")
    assert not safe_preview_message("My gate code is 1234")


def test_preview_does_not_send_rejected_input(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = iter(["Call me at (415) 555-0123", "My gate code is 1234", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(messages))
    monkeypatch.setattr("evals.scheduling_messages.propose", lambda *_: pytest.fail(
        "Rejected preview input reached the API call"))
    assert interactive("unused-key") == 0
