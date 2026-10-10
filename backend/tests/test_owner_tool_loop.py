"""Scripted owner tool loop at the conversation and receipt boundaries.

These checks do not call a live model, SMS provider, or DynamoDB.
"""

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from test_owner_counteroffer import (
    CLIENT_PHONE,
    NOW,
    OWNER,
    THURSDAY_9AM,
    Consent,
    Repository,
    profile,
)
from test_sms_processing import Reader

from scheduling.adapters.openai_messages import (
    OWNER_LOOP_BUDGET_SECONDS,
    OWNER_LOOP_MAX_CALLS,
    OpenAIMessageInterpreter,
)
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.conversation import (
    OWNER_FAILURE_TEXT,
    ConversationService,
    MessageContext,
    MessageProposal,
)
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor


class ScriptedModel:
    def __init__(self, action: Callable[[Callable[[str, dict[str, Any]], dict[str, Any]]], str]
                 ) -> None:
        self.action = action
        self.calls = 0
        self.contexts: list[tuple[date, str, tuple[HistoryMessage, ...]]] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        raise AssertionError("Owner should not use the client proposer")

    def run_owner_loop(self, body: str, today: date, timezone: str,
                       history: tuple[HistoryMessage, ...],
                       tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        self.calls += 1
        self.contexts.append((today, timezone, history))
        return self.action(tool)


def world(action: Callable[[Callable[[str, dict[str, Any]], dict[str, Any]]], str]
          ) -> tuple[Repository, ScriptedModel, ConversationService]:
    repository = Repository()
    repository.save_profile(profile("client-1", "Avery Sample", CLIENT_PHONE), 0, None)
    repository.save_profile(profile("client-2", "Blake Example", "+15005550007"), 0, None)
    model = ScriptedModel(action)
    holds = HoldService(repository)
    service = ConversationService(repository, model, holds,
                                  LifecycleService(repository, lambda: NOW), Consent(),
                                  lambda: NOW, OWNER)
    return repository, model, service


def pending(repository: Repository, key: str, client: str = "client-1",
            start: datetime = THURSDAY_9AM) -> str:
    return HoldService(repository).create(CreateHold(
        "pilot", client, client, key, start, 120), NOW).hold_id


def receipt(body: str, provider_id: str = "SM-owner-1") -> InboundReceipt:
    return InboundReceipt("pilot", provider_id, OWNER, "+14155550000", body, NOW,
                          SenderRole.OWNER, None, Keyword.OTHER, True)


def test_approve_and_hedged_reply_use_model_choice_without_allowlist() -> None:
    for body in ("Approve", "Yes, but please tell me when it is done"):
        def decide(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
            listed = tool("list_pending_requests", {})["requests"]
            assert listed[0]["client"] == "Avery"
            assert listed[0]["status"] == CalendarStatus.PENDING_APPROVAL.value
            assert tool("approve_request", {"ref": listed[0]["ref"],
                                            "version": listed[0]["version"]})["ok"]
            return "Approved Avery's request."

        repository, model, service = world(decide)
        ref = pending(repository, body)
        result = service.handle(receipt(body))
        assert result.text == "Approved Avery's request."
        assert result.committed and result.owner_reply_on_commit
        assert repository.read_appointment(ref).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]
        assert model.calls == 1


def test_stale_version_is_structured_and_changes_nothing() -> None:
    def decide(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        listed = tool("list_pending_requests", {})["requests"]
        failure = tool("approve_request", {"ref": listed[0]["ref"], "version": 99})
        assert failure == {"ok": False, "error": "stale_version", "ref": listed[0]["ref"],
                           "current_version": listed[0]["version"]}
        return "That request changed. Nothing changed here."

    repository, _, service = world(decide)
    ref = pending(repository, "stale")
    result = service.handle(receipt("approve it"))
    assert result.text == "That request changed. Nothing changed here."
    assert not result.committed
    assert repository.read_appointment(ref).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]


def test_named_request_among_several_and_ambiguous_reply() -> None:
    def decide(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        listed = tool("list_pending_requests", {})["requests"]
        assert len(listed) == 2
        selected = next(item for item in listed if item["client"] == "Blake")
        assert tool("decline_request", {"ref": selected["ref"],
                                        "version": selected["version"]})["ok"]
        return "Declined Blake's request."

    repository, _, service = world(decide)
    first = pending(repository, "a")
    second = pending(repository, "b", "client-2", THURSDAY_9AM + timedelta(days=1))
    assert service.handle(receipt("Decline Blake's request")).text == "Declined Blake's request."
    assert repository.read_appointment(first).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]
    assert repository.read_appointment(second).status == CalendarStatus.DECLINED  # type: ignore[union-attr]

    repository, _, service = world(lambda tool: (
        "Which request do you mean?" if len(tool("list_pending_requests", {})["requests"]) == 2
        else "Unexpected"))
    first = pending(repository, "c")
    second = pending(repository, "d", "client-2", THURSDAY_9AM + timedelta(days=1))
    assert service.handle(receipt("Yes")).text == "Which request do you mean?"
    assert all(repository.read_appointment(ref).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]
               for ref in (first, second))


def test_receipt_redelivery_reuses_stored_model_reply() -> None:
    def decide(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        target = tool("list_pending_requests", {})["requests"][0]
        tool("approve_request", {"ref": target["ref"], "version": target["version"]})
        return "Approved once."

    repository, model, service = world(decide)
    ref = pending(repository, "replay")
    inbound = receipt("Approve")
    store = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(store, service, "pilot", lambda: NOW)
    assert processor.process("pilot", inbound.provider_id).text == "Approved once."  # type: ignore[union-attr]
    assert processor.process("pilot", inbound.provider_id) is None
    assert model.calls == 1
    assert store.replies == [(inbound.provider_id, "Approved once.")]
    assert repository.read_appointment(ref).version == 2  # type: ignore[union-attr]


def test_crash_after_commit_stores_fixed_owner_failure_once() -> None:
    class CommittedReader(Reader):
        def has_committed_owner_reply_command(self, receipt: InboundReceipt) -> bool:
            return True

    inbound = receipt("Approve", "SM-crash")
    store = CommittedReader({("pilot", inbound.provider_id): inbound})
    store.committed.add(inbound.provider_id)
    _, model, service = world(lambda _tool: "should not run")
    processor = ReceiptProcessor(store, service, "pilot", lambda: NOW)
    assert processor.process("pilot", inbound.provider_id).text == OWNER_FAILURE_TEXT  # type: ignore[union-attr]
    assert processor.process("pilot", inbound.provider_id) is None
    assert store.replies == [(inbound.provider_id, OWNER_FAILURE_TEXT)]
    assert model.calls == 0


def test_model_timeout_has_fixed_failure_reply() -> None:
    repository, _, service = world(lambda _tool: (_ for _ in ()).throw(TimeoutError()))
    ref = pending(repository, "timeout")
    assert service.handle(receipt("Approve")).text == OWNER_FAILURE_TEXT
    assert repository.read_appointment(ref).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]


def test_invalid_scripted_reply_uses_failure_line() -> None:
    repository, _, service = world(lambda _tool: "Please approve this request 😀")
    pending(repository, "invalid")
    assert service.handle(receipt("Approve")).text == OWNER_FAILURE_TEXT


@pytest.mark.parametrize("bad", [{"ref": "deadbeef", "version": 1},
                                     {"ref": "x", "version": True}])
def test_tool_rejects_unknown_or_invalid_target(bad: dict[str, Any]) -> None:
    def decide(tool: Callable[[str, dict[str, Any]], dict[str, Any]]) -> str:
        failure = tool("approve_request", bad)
        assert failure["ok"] is False
        return "Nothing changed."

    repository, _, service = world(decide)
    pending(repository, "unknown")
    assert service.handle(receipt("Approve it")).text == "Nothing changed."


def test_openai_loop_continues_after_tool_json_and_uses_final_text(monkeypatch: pytest.MonkeyPatch
                                                                    ) -> None:
    responses = iter([
        {"output": [{"type": "function_call", "name": "list_pending_requests",
                     "call_id": "call-1", "arguments": "{}"}]},
        {"output": [{"type": "function_call", "name": "approve_request",
                     "call_id": "call-2", "arguments": '{"ref":"abcd1234","version":1}'}]},
        {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Approved Avery's request."}]}]},
    ])
    payloads: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: int) -> BytesIO:
        payloads.append(json.loads(request.data))
        return BytesIO(json.dumps(next(responses)).encode())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)

    def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "list_pending_requests":
            return {"ok": True, "requests": [{"ref": "abcd1234", "version": 1,
                                              "client": "Avery", "time": "Thursday 9 AM",
                                              "status": "pending_approval"}]}
        assert name == "approve_request" and args == {"ref": "abcd1234", "version": 1}
        return {"ok": True, "ref": "abcd1234", "status": "confirmed"}

    reply = OpenAIMessageInterpreter("synthetic-key").run_owner_loop(
        "Approve", date(2026, 10, 1), "America/Los_Angeles", (), tool)
    assert reply == "Approved Avery's request."
    assert len(payloads) == 3
    assert payloads[0]["store"] is False
    assert payloads[1]["input"][-1] == {"type": "function_call_output",
                                       "call_id": "call-1", "output": json.dumps(
                                           {"ok": True, "requests": [{"ref": "abcd1234",
                                            "version": 1, "client": "Avery",
                                            "time": "Thursday 9 AM",
                                            "status": "pending_approval"}]}, separators=(",", ":"))}
    assert payloads[2]["input"][-1]["call_id"] == "call-2"


@pytest.mark.parametrize("invalid", ["x" * 481, "Approved 😀"])
def test_openai_loop_reasks_once_for_invalid_sms(monkeypatch: pytest.MonkeyPatch,
                                                  invalid: str) -> None:
    texts = iter([invalid, "Nothing changed."])
    payloads: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: int) -> BytesIO:
        payloads.append(json.loads(request.data))
        return BytesIO(json.dumps({"output": [{"type": "message", "content": [
            {"type": "output_text", "text": next(texts)}]}]}).encode())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    assert OpenAIMessageInterpreter("synthetic-key").run_owner_loop(
        "Yes", date(2026, 10, 1), "America/Los_Angeles", (),
        lambda _name, _args: {}) == "Nothing changed."
    assert len(payloads) == 2
    assert payloads[1]["tool_choice"] == "none"


def test_owner_loop_can_rewrite_after_two_tools_within_worker_bound(
        monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter([
        {"output": [{"type": "function_call", "name": "list_pending_requests",
                     "call_id": "list", "arguments": "{}"}]},
        {"output": [{"type": "function_call", "name": "approve_request",
                     "call_id": "approve", "arguments": '{"ref":"abcd1234","version":1}'}]},
        {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Approved 😀"}]}]},
        {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Approved Avery's request."}]}]},
    ])
    calls: list[tuple[dict[str, Any], float]] = []

    def fake_urlopen(request: Any, timeout: float) -> BytesIO:
        calls.append((json.loads(request.data), timeout))
        return BytesIO(json.dumps(next(responses)).encode())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    reply = OpenAIMessageInterpreter("synthetic-key", timeout_seconds=8).run_owner_loop(
        "Approve", date(2026, 10, 1), "America/Los_Angeles", (),
        lambda name, _args: {"ok": True, "requests": [{"ref": "abcd1234", "version": 1}]}
        if name == "list_pending_requests" else {"ok": True, "status": "confirmed"})
    assert reply == "Approved Avery's request."
    assert len(calls) == 4 < OWNER_LOOP_MAX_CALLS
    assert all(0 < timeout <= 8 for _, timeout in calls)
    assert calls[-1][0]["tool_choice"] == "none"
    worker = Path(__file__).resolve().parents[2].joinpath("template.yaml").read_text()
    worker = worker.split("  SmsConversationFunction:\n", 1)[1].split(
        "  SmsConversationLogGroup:\n", 1)[0]
    assert "      Timeout: 60\n" in worker
    assert OWNER_LOOP_BUDGET_SECONDS < 60


def test_owner_loop_reserves_rewrite_after_three_tool_calls(
        monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter([
        {"output": [{"type": "function_call", "name": "list_pending_requests",
                     "call_id": "first", "arguments": "{}"}]},
        {"output": [{"type": "function_call", "name": "list_pending_requests",
                     "call_id": "second", "arguments": "{}"}]},
        {"output": [{"type": "function_call", "name": "approve_request",
                     "call_id": "decision", "arguments": '{"ref":"abcd1234","version":1}'}]},
        {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Approved 😀"}]}]},
        {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Approved Avery's request."}]}]},
    ])
    payloads: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float) -> BytesIO:
        payloads.append(json.loads(request.data))
        return BytesIO(json.dumps(next(responses)).encode())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    reply = OpenAIMessageInterpreter("synthetic-key", timeout_seconds=8).run_owner_loop(
        "Approve", date(2026, 10, 1), "America/Los_Angeles", (),
        lambda name, _args: {"ok": True, "requests": [{"ref": "abcd1234", "version": 1}]}
        if name == "list_pending_requests" else {"ok": True, "status": "confirmed"})
    assert reply == "Approved Avery's request."
    assert len(payloads) == OWNER_LOOP_MAX_CALLS == 5
    assert [payload["tool_choice"] for payload in payloads] == [
        "auto", "auto", "auto", "none", "none"]


def test_owner_loop_stops_at_total_deadline_for_fixed_failure(
        monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = [0.0]
    calls: list[float] = []

    def fake_urlopen(_request: Any, timeout: float) -> BytesIO:
        calls.append(timeout)
        elapsed[0] += timeout
        return BytesIO(json.dumps({"output": [{"type": "function_call",
            "name": "list_pending_requests", "call_id": f"call-{len(calls)}",
            "arguments": "{}"}]}).encode())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    monkeypatch.setattr("scheduling.adapters.openai_messages.monotonic", lambda: elapsed[0])
    with pytest.raises(TimeoutError, match="time budget"):
        OpenAIMessageInterpreter("synthetic-key", timeout_seconds=15).run_owner_loop(
            "Approve", date(2026, 10, 1), "America/Los_Angeles", (),
            lambda _name, _args: {"ok": True, "requests": []})
    assert calls == [15, 15, 6]
    assert sum(calls) == OWNER_LOOP_BUDGET_SECONDS
