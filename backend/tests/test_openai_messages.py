"""The model adapter proposes bounded intent and never receives customer context."""

import json
from datetime import date
from io import BytesIO
from typing import Any
from urllib.request import Request

import pytest

from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.domain.conversation import MessageContext
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyContext,
    OwnerReplyIntent,
    PendingRef,
)
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
        "needs_clarification": False, "question": None, "statuses": None, "view": None,
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


def owner_reply_response(**changes: Any) -> BytesIO:
    arguments: dict[str, Any] = {
        "intent": "calendar_followup", "request_reference": None, "confidence": "high",
        "statuses": ["confirmed"], "date_from": None, "date_to": None}
    arguments.update(changes)
    return BytesIO(json.dumps({"output": [{
        "type": "function_call", "name": "classify_owner_reply",
        "arguments": json.dumps(arguments),
    }]}).encode())


def test_owner_reply_classification_sends_only_bounded_context(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def fake_urlopen(request: Request, timeout: int) -> BytesIO:
        sent.append(json.loads(request.data or b"{}"))
        return owner_reply_response()

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    pending = PendingRef("abc12345", "Blake", "Wed Oct 7 at 9:00 AM")
    context = OwnerReplyContext(date(2026, 10, 4), "America/Los_Angeles", "approval_question",
                                "summary", date(2026, 10, 5), date(2026, 10, 11),
                                ("confirmed", "pending"), pending, (pending,))
    result = OpenAIMessageInterpreter("synthetic-key").classify_owner_reply(
        "yes please", context)
    assert result.intent == OwnerReplyIntent.CALENDAR_FOLLOWUP
    assert result.confidence == Confidence.HIGH and result.statuses is not None
    payload = sent[0]
    assert payload["store"] is False and payload["tool_choice"]["name"] == "classify_owner_reply"
    assert "ref abc12345, Blake, Wed Oct 7 at 9:00 AM" in payload["input"]
    assert "yes please" in payload["input"] and "synthetic-key" not in json.dumps(payload)


@pytest.mark.parametrize("changes", [
    {"intent": "approve_everything"}, {"confidence": "certain"}, {"statuses": ["bogus"]},
    {"date_from": "next friday"}, {"request_reference": "x" * 65}])
def test_invalid_owner_reply_classification_is_rejected(
        monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: owner_reply_response(**changes))
    context = OwnerReplyContext(date(2026, 10, 4), "America/Los_Angeles", "calendar_answer",
                                "summary", None, None, (), None, ())
    with pytest.raises(ValueError):
        OpenAIMessageInterpreter("synthetic-key").classify_owner_reply("yes", context)


def test_owner_reply_classification_describes_an_open_offer_and_accepts_offer_intents(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def fake_urlopen(request: Request, timeout: int) -> BytesIO:
        sent.append(json.loads(request.data or b"{}"))
        return owner_reply_response(intent="confirm_offer", statuses=None)

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    pending = PendingRef("abc12345", "Blake", "Wed Oct 7 at 9:00 AM")
    context = OwnerReplyContext(
        date(2026, 10, 4), "America/Los_Angeles", "offer_prompt", "none", None, None, (), None,
        (pending,), PendingRef("abc12345", "Blake", "Wed Oct 7 at 2:00 PM"))
    result = OpenAIMessageInterpreter("synthetic-key").classify_owner_reply("go ahead", context)
    assert result.intent == OwnerReplyIntent.CONFIRM_OFFER
    text = sent[0]["input"]
    assert "Last assistant message: offer_prompt" in text
    assert "Open offer: ref abc12345, Blake, new time Wed Oct 7 at 2:00 PM" in text
    enum = sent[0]["tools"][0]["parameters"]["properties"]["intent"]["enum"]
    assert {"confirm_offer", "cancel_offer", "how_to", "calendar_question"} <= set(enum)


def test_calendar_question_carries_statuses_view_and_the_open_answer(
        monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, Any]] = []

    def fake_urlopen(request: Request, timeout: int) -> BytesIO:
        requests.append(json.loads(request.data or b"{}"))
        return response(proposal(intent="calendar_question", request_reference=None,
                                 owner_decision=None, statuses=["confirmed"], view="count"))

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    context = MessageContext(SenderRole.CLIENT, date(2026, 9, 29), "America/Los_Angeles", (),
                             calendar_answer="2026-09-28 to 2026-10-04; statuses: confirmed, "
                                             "pending; view: list")
    result = OpenAIMessageInterpreter("synthetic-key").propose(
        "¿Cuántas visitas confirmadas tengo?", context)
    assert (result.intent, result.statuses, result.view) == (
        "calendar_question", ("confirmed",), "count")
    assert ("Last calendar answer: 2026-09-28 to 2026-10-04; statuses: confirmed, pending; "
            "view: list") in requests[0]["input"]


@pytest.mark.parametrize("change", [{"statuses": ["confirmed", "declined"]},
                                    {"statuses": "confirmed"}, {"view": "table"}])
def test_invalid_calendar_fields_are_rejected(monkeypatch: pytest.MonkeyPatch,
                                              change: dict[str, Any]) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: response(proposal(
                            intent="calendar_question", request_reference=None,
                            owner_decision=None, **change)))
    context = MessageContext(SenderRole.CLIENT, date(2026, 9, 29), "America/Los_Angeles", ())
    with pytest.raises(ValueError):
        OpenAIMessageInterpreter("synthetic-key").propose("Do I have visits?", context)
