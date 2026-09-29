"""The evaluation cannot mutate scheduling state or accept malformed tool calls."""

import json
from typing import Any

import pytest

from evals.scheduling_messages import (
    CASES,
    INSTRUCTIONS,
    TOOL,
    case_passes,
    interactive,
    parse_proposal,
    request_payload,
    safe_preview_message,
)


def test_every_case_uses_the_production_prompt_and_only_a_read_only_tool() -> None:
    assert len(CASES) >= 8
    assert all(case.actor in {"client", "owner"} for case in CASES)
    for case in CASES:
        payload = request_payload(case)
        assert payload["tools"] == [TOOL]
        assert payload["instructions"] == INSTRUCTIONS
        assert payload["tool_choice"] == {"type": "function", "name": "propose_message"}
        assert payload["store"] is False
        assert "Today: 2026-09-28 (Monday)" in payload["input"]
        assert "write" not in TOOL["name"]


def test_rejects_missing_or_multiple_tool_proposals() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        parse_proposal({"output": []})
    with pytest.raises(ValueError, match="exactly one"):
        parse_proposal({"output": [{"type": "function_call", "name": "propose_message"}] * 2})


def blank(**changes: Any) -> dict[str, Any]:
    proposal: dict[str, Any] = {name: None for name in TOOL["parameters"]["required"]}
    proposal.update(intent="clarify", needs_clarification=True, question="Which one?")
    proposal.update(changes)
    return proposal


def case(name: str) -> Any:
    return next(item for item in CASES if item.name == name)


def test_clarification_requires_a_question() -> None:
    with pytest.raises(ValueError, match="needs a question"):
        parse_proposal({"output": [{"type": "function_call", "name": "propose_message",
                                    "arguments": json.dumps(blank(question=None))}]})


def test_owner_yes_with_two_requests_cannot_pick_a_reference() -> None:
    yes = case("owner-yes-two-requests")
    assert case_passes(yes, blank())
    assert case_passes(yes, blank(intent="owner_decision", needs_clarification=False,
                                  owner_decision="approve", question=None))
    assert not case_passes(yes, blank(request_reference="a101a101"))
    assert not case_passes(yes, blank(date_text="2026-10-02 15:00"))


def test_exact_owner_reference_needs_correct_intent_target_and_decision() -> None:
    explicit = case("explicit-owner-reference")
    proposal = blank(intent="owner_decision", request_reference="a101a101",
                     owner_decision="approve", needs_clarification=False, question=None)
    assert case_passes(explicit, proposal)
    assert not case_passes(explicit, {**proposal, "intent": "unsupported"})
    assert not case_passes(explicit, {**proposal, "request_reference": "b202b202"})


def test_relative_day_must_resolve_to_the_right_date() -> None:
    tomorrow = case("tomorrow")
    good = blank(intent="availability", needs_clarification=False, question=None,
                 date_from="2026-09-29", date_to="2026-09-29")
    assert case_passes(tomorrow, good)
    assert not case_passes(tomorrow, {**good, "date_from": "2026-09-30", "date_to": "2026-09-30"})
    assert not case_passes(tomorrow, {**good, "request_reference": "a101a101"})
    assert not case_passes(tomorrow, blank())


def test_broad_window_may_clarify_or_offer_one_friday_afternoon() -> None:
    broad = case("broad-window")
    friday = blank(intent="availability", needs_clarification=False, question=None,
                   date_from="2026-10-02", date_to="2026-10-02",
                   time_from="12:00", time_to="17:00")
    assert case_passes(broad, blank())
    assert case_passes(broad, friday)
    assert case_passes(broad, {**friday, "date_from": "2026-10-09", "date_to": "2026-10-09"})
    assert not case_passes(broad, {**friday, "date_to": "2026-10-09"})
    assert not case_passes(broad, {**friday, "time_from": "08:00"})
    assert not case_passes(broad, {**friday, "date_text": "2026-10-02 15:00"})


def test_invalid_date_must_clarify_without_a_guessed_date() -> None:
    invalid = case("invalid-date")
    assert case_passes(invalid, blank())
    assert not case_passes(invalid, blank(date_from="2026-03-02"))


def test_cant_make_thursday_names_the_visit_day_not_a_reference() -> None:
    thursday = case("cant-make-thursday")
    good = blank(intent="cancel", needs_clarification=False, question=None,
                 target_date="2026-10-01")
    assert case_passes(thursday, good)
    assert not case_passes(thursday, {**good, "request_reference": "a101a101"})
    assert not case_passes(thursday, {**good, "target_date": "2026-10-08"})


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
