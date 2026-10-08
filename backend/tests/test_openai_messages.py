"""The model adapter proposes bounded intent and never receives customer context."""

import json
from datetime import UTC, date, datetime, time
from io import BytesIO
from typing import Any
from urllib.request import Request

import pytest

from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.domain.client_replies import ClientReplyFact, ClientReplyResult
from scheduling.domain.conversation import MessageContext
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyContext,
    OwnerReplyIntent,
    PendingRef,
)
from scheduling.domain.sms_ingress import SenderRole


def test_client_reply_draft_uses_structured_result_and_rejects_malformed_output(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def reply(request: Request, timeout: int) -> BytesIO:
        sent.append(json.loads(request.data or b"{}"))
        return BytesIO(json.dumps({"output": [{"type": "function_call", "name": "draft_sms",
                                       "arguments": json.dumps({"text": "Pending owner approval. Ref a101a101."})}]}).encode())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", reply)
    model = OpenAIMessageInterpreter("synthetic-key")
    context = MessageContext(SenderRole.CLIENT, date(2026, 10, 12),
                             "America/Los_Angeles", ())
    result = ClientReplyResult(
        "request_created", "pending", "Requested Tue Oct 13 at 1:00 PM "
        "(ref a101a101). It's pending owner approval.",
        (ClientReplyFact("Tue Oct 13", "1:00 PM", "pending owner approval", "a101a101"),))
    assert model.draft_client_reply("yes", context, result).startswith("Pending")
    assert '"status": "pending"' in sent[0]["input"]
    assert '"reference": "a101a101"' in sent[0]["input"]
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: BytesIO(b'{"output":[]}'))
    with pytest.raises(ValueError, match="one SMS draft"):
        model.draft_client_reply("yes", context, result)


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
        "range_scope": None,
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


def test_date_only_proposal_sees_rolling_history_as_untrusted_data(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def fake_urlopen(request: Request, timeout: int) -> BytesIO:
        sent.append(json.loads(request.data or b"{}"))
        return response(proposal(intent="availability", request_reference=None,
                                 owner_decision=None, date_from="2026-10-13",
                                 date_to="2026-10-13"))

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    context = MessageContext(SenderRole.CLIENT, date(2026, 10, 12),
                             "America/Los_Angeles", (), 14, None, (
                                 HistoryMessage("invite", "assistant",
                                                datetime(2026, 10, 12, tzinfo=UTC),
                                                "Would you like a cleaning?", True),
                                 HistoryMessage("answer", "assistant",
                                                datetime(2026, 10, 12, 1, tzinfo=UTC),
                                                "1 pm is unavailable."),
                             ), "offer")
    result = OpenAIMessageInterpreter("synthetic-key").propose("Oct 13", context)
    assert result.intent == "availability" and result.date_from == "2026-10-13"
    assert "1 pm is unavailable" in sent[0]["input"]
    assert sent[0]["instructions"] != sent[0]["input"]
    assert "date-only reply" in sent[0]["instructions"]


def test_read_draft_requires_verbatim_authoritative_result(
        monkeypatch: pytest.MonkeyPatch) -> None:
    facts = "I don't have any openings on Tue Oct 13. Would another day work?"
    drafts = iter((f"Thanks for checking. {facts}",
                   "Yes, 1:00 PM is open on Tue Oct 13."))

    def fake_urlopen(request: Request, timeout: int) -> BytesIO:
        payload = json.loads(request.data or b"{}")
        assert payload["tool_choice"]["name"] == "draft_sms"
        assert payload["store"] is False and payload["parallel_tool_calls"] is False
        return BytesIO(json.dumps({"output": [{"type": "function_call",
                                                "name": "draft_sms",
                                                "arguments": json.dumps({"text": next(drafts)})}]}).encode())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    model = OpenAIMessageInterpreter("synthetic-key")
    context = MessageContext(SenderRole.CLIENT, date(2026, 10, 12),
                             "America/Los_Angeles", ())
    assert model.draft_read_reply("Oct 13", context, "list_available_slots", facts).startswith(
        "Thanks for checking.")
    with pytest.raises(ValueError, match="trusted result"):
        model.draft_read_reply("Oct 13", context, "list_available_slots", facts)


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
        "statuses": ["confirmed"], "date_from": None, "date_to": None,
        "request_version": None, "offer_date": None, "offer_time": None}
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
    pending = PendingRef("abc12345", "Blake", "Wed Oct 7 at 9:00 AM", 3)
    context = OwnerReplyContext(date(2026, 10, 4), "America/Los_Angeles", "approval_question",
                                "summary", date(2026, 10, 5), date(2026, 10, 11),
                                ("confirmed", "pending"), pending, (pending,))
    result = OpenAIMessageInterpreter("synthetic-key").classify_owner_reply(
        "yes please", context)
    assert result.intent == OwnerReplyIntent.CALENDAR_FOLLOWUP
    assert result.confidence == Confidence.HIGH and result.statuses is not None
    payload = sent[0]
    assert payload["store"] is False and payload["tool_choice"]["name"] == "classify_owner_reply"
    assert "ref abc12345, version 3, Blake, Wed Oct 7 at 9:00 AM" in payload["input"]
    assert "yes please" in payload["input"] and "synthetic-key" not in json.dumps(payload)


@pytest.mark.parametrize("changes", [
    {"intent": "approve_everything"}, {"confidence": "certain"}, {"statuses": ["bogus"]},
    {"date_from": "next friday"}, {"request_reference": "x" * 65},
    {"offer_date": "Friday"}, {"offer_time": "2pm"}, {"request_version": "2"},
    {"request_version": True}])
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
                                    {"statuses": "confirmed"}, {"view": "table"},
                                    {"range_scope": "forever"}, {"range_scope": "dates"}])
def test_invalid_calendar_fields_are_rejected(monkeypatch: pytest.MonkeyPatch,
                                              change: dict[str, Any]) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: response(proposal(
                            intent="calendar_question", request_reference=None,
                            owner_decision=None, **change)))
    context = MessageContext(SenderRole.CLIENT, date(2026, 9, 29), "America/Los_Angeles", ())
    with pytest.raises(ValueError):
        OpenAIMessageInterpreter("synthetic-key").propose("Do I have visits?", context)


def test_calendar_fields_are_dropped_on_other_intents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: response(proposal(
                            intent="availability", request_reference=None, owner_decision=None,
                            date_from="2026-10-02", date_to="2026-10-02",
                            statuses=["confirmed"], view="count", range_scope="all_upcoming")))
    context = MessageContext(SenderRole.CLIENT, date(2026, 9, 29), "America/Los_Angeles", ())
    result = OpenAIMessageInterpreter("synthetic-key").propose("Friday?", context)
    assert (result.statuses, result.view, result.range_scope) == (None, None, None)


def test_a_stray_range_scope_does_not_break_a_booking_request(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: response(proposal(
                            intent="availability", request_reference=None, owner_decision=None,
                            range_scope="dates")))
    context = MessageContext(SenderRole.CLIENT, date(2026, 9, 29), "America/Los_Angeles", ())
    result = OpenAIMessageInterpreter("synthetic-key").propose("A cleaning soon?", context)
    assert result.intent == "availability" and result.range_scope is None


def test_owner_counteroffer_tool_arguments_are_typed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scheduling.adapters.openai_messages.urlopen",
        lambda *_args, **_kwargs: owner_reply_response(
            intent="prepare_counteroffer", request_reference="abc12345", statuses=None,
            request_version=2, offer_date="2026-10-09", offer_time="14:00"))
    context = OwnerReplyContext(date(2026, 10, 4), "America/Los_Angeles", "none", "none",
                                None, None, (), None, ())
    result = OpenAIMessageInterpreter("synthetic-key").classify_owner_reply(
        "could Blake do Friday at two", context)
    assert result.intent == OwnerReplyIntent.PREPARE_COUNTEROFFER
    assert (result.request_version, result.offer_date, result.offer_time) == (
        2, date(2026, 10, 9), time(14, 0))
