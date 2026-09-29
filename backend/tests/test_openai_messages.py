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
        "date_text": None, "owner_decision": "approve",
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
