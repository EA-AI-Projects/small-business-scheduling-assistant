"""The model adapter proposes bounded intent and never receives customer context."""

import json
from datetime import date
from io import BytesIO
from typing import Any
from urllib.request import Request

import pytest

from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.domain.conversation import MessageContext
from scheduling.domain.sms_ingress import SenderRole


def response(arguments: dict[str, Any]) -> BytesIO:
    return BytesIO(json.dumps({"output": [{
        "type": "function_call", "name": "propose_message",
        "arguments": json.dumps(arguments),
    }]}).encode())


def proposal(**changes: Any) -> dict[str, Any]:
    result = {
        "intent": "owner_decision", "request_reference": "abc12345",
        "date_text": None, "date_from": None, "date_to": None, "time_from": None,
        "time_to": None, "target_date": None, "owner_decision": "approve",
        "needs_clarification": False, "question": None,
    }
    result.update(changes)
    return result


def test_bounded_model_call_contains_only_refs_actor_and_text(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, Any]] = []

    def fake_urlopen(request: Request, timeout: int) -> BytesIO:
        assert timeout == 15
        assert request.full_url == "https://api.openai.com/v1/responses"
        payload = json.loads(request.data or b"{}")
        requests.append(payload)
        return response(proposal())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    context = MessageContext(SenderRole.OWNER, date(2026, 9, 29),
                             "America/Los_Angeles", ("abc12345",))
    result = OpenAIMessageInterpreter("synthetic-key").propose("Approve abc12345", context)
    assert result.intent == "owner_decision"
    assert result.request_reference == "abc12345"
    assert requests[0]["store"] is False
    assert requests[0]["parallel_tool_calls"] is False
    assert requests[0]["max_output_tokens"] == 512
    assert "abc12345" in requests[0]["input"]
    assert "synthetic-key" not in json.dumps(requests[0])


def test_ambiguous_proposal_with_action_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: response(proposal(
                            intent="clarify", needs_clarification=True,
                            question="Which request?")))
    context = MessageContext(SenderRole.OWNER, date(2026, 9, 29),
                             "America/Los_Angeles", ("abc12345",))
    with pytest.raises(ValueError, match="Ambiguous proposal"):
        OpenAIMessageInterpreter("synthetic-key").propose("Yes", context)


def test_long_clarifying_question_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: response(proposal(
                            intent="clarify", request_reference=None, owner_decision=None,
                            needs_clarification=True, question="Which date? " * 20)))
    context = MessageContext(SenderRole.CLIENT, date(2026, 9, 29),
                             "America/Los_Angeles", ())
    result = OpenAIMessageInterpreter("synthetic-key").propose(
        "I would like to book an appointment for this week", context)
    assert result.intent == "clarify"
    assert result.needs_clarification


@pytest.mark.parametrize("field", ["request_reference", "date_text"])
def test_action_fields_keep_strict_length_bound(
    monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: response(proposal(**{field: "x" * 129})))
    context = MessageContext(SenderRole.OWNER, date(2026, 9, 29),
                             "America/Los_Angeles", ("abc12345",))
    with pytest.raises(ValueError, match="Model proposal values are invalid"):
        OpenAIMessageInterpreter("synthetic-key").propose("Approve abc12345", context)


def test_resolved_dates_and_window_are_returned_for_backend_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    def fake_urlopen(request: Request, timeout: int) -> BytesIO:
        requests.append(json.loads(request.data or b"{}"))
        return response(proposal(intent="availability", request_reference=None,
                                 owner_decision=None, date_from="2026-09-30",
                                 date_to="2026-09-30", time_from="08:00", time_to="12:00"))

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    context = MessageContext(SenderRole.CLIENT, date(2026, 9, 29), "America/Los_Angeles", ())
    result = OpenAIMessageInterpreter("synthetic-key").propose("Tomorrow morning?", context)
    assert (result.intent, result.date_from, result.date_to) == (
        "availability", "2026-09-30", "2026-09-30")
    assert (result.time_from, result.time_to) == ("08:00", "12:00")
    assert "Today: 2026-09-29 (Tuesday)" in requests[0]["input"]
    assert "Booking horizon: 14 days" in requests[0]["input"]


@pytest.mark.parametrize(("field", "value"), [
    ("date_from", "tomorrow"), ("date_to", "2026-9-30"), ("target_date", "2026-09-30T09:00"),
    ("time_from", "9am"), ("time_to", "9:00"), ("date_from", 20260930),
])
def test_resolved_fields_must_be_plain_iso_shapes(monkeypatch: pytest.MonkeyPatch,
                                                  field: str, value: object) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: response(proposal(
                            intent="availability", request_reference=None,
                            owner_decision=None, **{field: value})))
    context = MessageContext(SenderRole.CLIENT, date(2026, 9, 29), "America/Los_Angeles", ())
    with pytest.raises(ValueError, match="invalid"):
        OpenAIMessageInterpreter("synthetic-key").propose("Tomorrow?", context)


def test_clarification_cannot_carry_a_resolved_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: response(proposal(
                            intent="clarify", request_reference=None, owner_decision=None,
                            needs_clarification=True, question="Which day?",
                            date_from="2026-09-30")))
    context = MessageContext(SenderRole.CLIENT, date(2026, 9, 29), "America/Los_Angeles", ())
    with pytest.raises(ValueError, match="Ambiguous proposal"):
        OpenAIMessageInterpreter("synthetic-key").propose("Sometime?", context)
