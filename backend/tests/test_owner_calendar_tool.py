"""Production Responses continuation for the owner calendar read tool (offline)."""

import json
from datetime import date
from io import BytesIO
from typing import Any

import pytest
from test_owner_calendar_questions import Chat

from scheduling.adapters.openai_messages import OpenAIMessageInterpreter


@pytest.mark.parametrize("query", [
    {"from": "20261001", "to": "2026-10-01", "statuses": [], "offset": 0},
    {"from": "2026-10-01", "to": "2026-11-02", "statuses": [], "offset": 0},
    {"from": "2026-10-01", "to": "2026-10-01", "statuses": ["declined"], "offset": 0},
    {"from": "2026-10-01", "to": "2026-10-01", "statuses": [], "offset": -1},
])
def test_calendar_tool_rejects_invalid_scope_without_calendar_change(
        query: dict[str, Any]) -> None:
    chat = Chat()
    before = chat.store.read_revision("pilot")
    chat.model.queries["Check calendar"] = query
    assert not chat.ask("Check calendar").committed
    assert chat.model.results[-1] == {"ok": False, "error": "invalid_arguments"}
    assert chat.store.read_revision("pilot") == before


def test_get_calendar_tool_json_reaches_final_model_turn(
        monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([
        {"output": [{"type": "function_call", "name": "get_calendar",
                     "call_id": "calendar-1", "arguments": json.dumps({
                         "from": "2026-10-02", "to": "2026-10-02",
                         "statuses": ["pending"], "offset": 8})}]},
        {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Friday has one pending request."}]}]},
    ])
    payloads: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float) -> BytesIO:
        payloads.append(json.loads(request.data))
        return BytesIO(json.dumps(next(answers)).encode())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    seen: list[dict[str, Any]] = []
    result = {"ok": True, "from": "2026-10-02", "to": "2026-10-02",
              "timezone": "America/Los_Angeles", "statuses": ["pending"],
              "revision": 9, "counts": {"confirmed": 0, "pending": 1,
                                      "unavailable": 0},
              "total": 1, "offset": 8, "next_offset": None,
              "entries": [{"ref": "abcd1234", "status": "pending",
                           "start": "2026-10-02T09:00:00-07:00",
                           "end": "2026-10-02T11:00:00-07:00", "client": "Avery"}]}

    def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert name == "get_calendar"
        seen.append(args)
        return result

    text = OpenAIMessageInterpreter("synthetic-key", timeout_seconds=8).run_owner_loop(
        "More pending ones on Friday", date(2026, 10, 1),
        "America/Los_Angeles", (), tool)
    assert text == "Friday has one pending request."
    assert seen == [{"from": "2026-10-02", "to": "2026-10-02",
                     "statuses": ["pending"], "offset": 8}]
    assert any(item["name"] == "get_calendar" for item in payloads[0]["tools"])
    assert payloads[1]["input"][-1] == {
        "type": "function_call_output", "call_id": "calendar-1",
        "output": json.dumps(result, separators=(",", ":")),
    }
