"""Offline checks of the live client-loop runner (#357): a scripted model, no network.

These prove the runner's cases, tool-choice rules, and refusal to run without --live and a
key. They say nothing about how the real model chooses tools.
"""

import json
from datetime import datetime
from typing import Any

import pytest

from evals import client_tool_loop as runner
from evals.client_tool_loop import CASES, Step, run_case, tool_choice_ok
from scheduling.domain.conversation_history import HistoryMessage

REQUESTED = ("Requested Tue Oct 13 at {time}, ref d404d404. It is pending owner approval.")
ASK = "Please tell me which time you would like. Nothing was booked or changed."
NO_CANCEL = "Nothing was cancelled. Please tell me if you want to cancel your visit."

# case -> (tool calls the scripted model makes, its final text)
PLANS: dict[str, tuple[list[tuple[str, dict[str, Any]]], str]] = {
    "book-accepted-offer": ([("request_booking", {"date": "2026-10-13", "time": "13:00"})],
                            REQUESTED.format(time="1:00 PM")),
    "book-accepted-in-words": ([("request_booking", {"date": "2026-10-13", "time": "09:00"})],
                               REQUESTED.format(time="9:00 AM")),
    "book-unclear-acceptance": ([], ASK),
    "book-counter-proposal": ([], ASK),
    "book-question-about-offer": ([("get_business_info", {})],
                                  "Visits take about 120 minutes. Nothing was booked."),
    "cancel-one-clear-visit": (
        [("get_my_bookings", {"from": None, "to": None}),
         ("prepare_cancellation", {"ref": "b202b202"})],
        "Reply YES to cancel your Thu Oct 15 at 10:00 AM visit? Nothing is cancelled until you do."),
    "cancel-ambiguous-visit": ([("get_my_bookings", {"from": None, "to": None})],
                               "Which visit? " + NO_CANCEL),
    "cancel-yes": ([("confirm_cancellation", {})],
                   "Cancelled your Thu Oct 15 at 10:00 AM visit, ref b202b202."),
    "cancel-no": ([("dismiss_cancellation", {})],
                  "OK, your Thu Oct 15 at 10:00 AM visit stays booked. Nothing was cancelled."),
    "cancel-unclear": ([], NO_CANCEL),
    "cancel-unrelated": ([("get_business_info", {})],
                         "Please ask the business about supplies. " + NO_CANCEL),
    "move-asks-availability": (
        [("get_my_bookings", {"from": None, "to": None}),
         ("get_availability", {"from": "2026-10-16", "to": "2026-10-16", "time_from": None,
                               "time_to": "12:00", "ref": "b202b202"})],
        "OFFER"),
    "move-accepted-time": (
        [("request_reschedule", {"ref": "b202b202", "date": "2026-10-16", "time": "09:00"})],
        ("Requested Fri Oct 16 at 9:00 AM, ref d404d404. It is pending owner approval. Your "
        "Thu Oct 15 at 10:00 AM visit stays confirmed until the owner approves the move.")),
    "counteroffer-yes": ([("respond_to_counteroffer", {"decision": "accept"})],
                         ("Requested Wed Oct 14 at 10:00 AM, ref d404d404. It is pending "
                         "owner approval.")),
    "counteroffer-no": ([("respond_to_counteroffer", {"decision": "decline"})],
                        "OK, nothing was requested and your original request is unchanged."),
    "counteroffer-counter-proposal": ([], "Nothing was changed. Please tell me yes or no."),
    "counteroffer-question": ([], "Nothing was changed. Please tell me yes or no."),
    "mixed-service-question-and-booking": (
        [("get_business_info", {}),
         ("request_booking", {"date": "2026-10-13", "time": "09:00"})],
        ("Requested Tue Oct 13 at 9:00 AM, ref d404d404. It is pending owner approval. We "
        "offer Standard cleaning, about 120 minutes.")),
    "mixed-cancel-and-question": (
        [("prepare_cancellation", {"ref": "b202b202"}), ("get_business_info", {})],
        ("Reply YES to cancel your Thu Oct 15 at 10:00 AM visit? Nothing is cancelled until "
        "you do. We are closed Saturdays.")),
}


class ScriptedModel:
    """Stands in for the model: replays one planned tool sequence and final text per case."""

    def __init__(self, plans: dict[str, tuple[list[tuple[str, dict[str, Any]]], str]]) -> None:
        self.plans = plans
        self.seen: list[str] = []

    def run_client_loop(self, body: str, now: datetime, timezone: str,
                        history: tuple[HistoryMessage, ...], tool: Any, check: Any) -> str:
        case = next(item for item in CASES if item.message == body)
        self.seen.append(case.name)
        calls, text = self.plans[case.name]
        for name, args in calls:
            result = tool(name, args)
        if text == "OFFER":  # List the stored offer whole, as the reply check requires.
            listed = ", ".join(f"{item['number']}) {item['time']}"
                               for item in result["offer"]["options"])
            text = (f"Open Fri Oct 16: {listed}. The owner approves every request. Your Thu "
                    "Oct 15 at 10:00 AM visit stays confirmed until the owner approves the move.")
        problem = check(text)
        if problem is not None:
            raise ValueError(problem)
        return text


def test_every_case_has_a_plan_and_passes_with_a_correct_scripted_model() -> None:
    assert {case.name for case in CASES} == set(PLANS)
    model = ScriptedModel(PLANS)
    for case in CASES:
        result = run_case(model, case)
        assert result["error"] is None, (case.name, result["error"])
        assert result["tool_choice_passed"], case.name
        assert result["reply_passes_check_reply"], case.name
    assert len(model.seen) == len(CASES)


def test_cases_cover_the_acceptance_areas() -> None:
    expected = {step.name for case in CASES for step in case.expected}
    assert expected == {"request_booking", "prepare_cancellation", "confirm_cancellation",
                        "dismiss_cancellation", "get_availability", "request_reschedule",
                        "respond_to_counteroffer"}
    assert any(not case.expected for case in CASES)  # Neither-tool cases exist.
    assert len({case.name for case in CASES}) == len(CASES)


def test_wrong_tool_choices_fail() -> None:
    wrong = dict(PLANS)
    wrong["cancel-unclear"] = ([("confirm_cancellation", {})],
                               "Cancelled your Thu Oct 15 at 10:00 AM visit, ref b202b202.")
    wrong["book-counter-proposal"] = (
        [("request_booking", {"date": "2026-10-13", "time": "13:00"})],
        REQUESTED.format(time="1:00 PM"))
    wrong["counteroffer-yes"] = ([("respond_to_counteroffer", {"decision": "decline"})],
                                 ("OK, nothing was requested and your original request is "
                                 "unchanged."))
    model = ScriptedModel(wrong)
    by_name = {case.name: run_case(model, case) for case in CASES
               if case.name in ("cancel-unclear", "book-counter-proposal", "counteroffer-yes")}
    assert [item["tool_choice_passed"] for item in by_name.values()] == [False] * 3


def test_tool_choice_rules() -> None:
    book = Step("request_booking", (("time", "09:00"),))
    call = {"name": "request_booking", "args": {"date": "2026-10-13", "time": "09:00"}}
    assert tool_choice_ok((book,), [{"name": "get_business_info", "args": {}}, call])
    assert not tool_choice_ok((book,), [])
    assert not tool_choice_ok((), [call])
    assert not tool_choice_ok((book,), [call, call])
    assert not tool_choice_ok((book,), [call, {"name": "confirm_cancellation", "args": {}}])


def test_an_unsendable_reply_is_reported_not_hidden() -> None:
    bad = dict(PLANS)
    bad["book-accepted-offer"] = (PLANS["book-accepted-offer"][0], "Booked for Thu Oct 1 at 9:45 AM!")
    result = run_case(ScriptedModel(bad), CASES[0])
    assert result["reply"] is None and result["reply_passes_check_reply"] is False
    assert result["check_reply_rejections"] and "ValueError" in result["error"]


def test_refuses_to_call_a_model_without_live_and_a_key(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def forbidden(_key: str) -> ScriptedModel:
        raise AssertionError("a model must not be built")

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    assert runner.main([], forbidden) == 0  # Lists cases, no model.
    assert "No model call made" in capsys.readouterr().out
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(SystemExit) as exit_info:
        runner.main(["--live"], forbidden)
    assert exit_info.value.code == 2
    assert "OPENAI_API_KEY is required" in capsys.readouterr().err


def test_live_summary_exit_code_follows_tool_choice(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    assert runner.main(["--live"], lambda _key: ScriptedModel(PLANS)) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["cases"] == len(CASES) and summary["tool_choice_failures"] == []
    assert "synthetic-key" not in json.dumps(summary)
    wrong = dict(PLANS)
    wrong["cancel-unclear"] = ([("dismiss_cancellation", {})],
                               ("OK, your Thu Oct 15 at 10:00 AM visit stays booked. "
                               "Nothing was cancelled."))
    assert runner.main(["--live", "--case", "cancel-unclear"],
                       lambda _key: ScriptedModel(wrong)) == 1
    assert json.loads(capsys.readouterr().out)["tool_choice_failures"] == ["cancel-unclear"]


class RaisingModel:
    def run_client_loop(self, *_args: Any) -> str:
        raise RuntimeError("Model API HTTP 401")


def test_a_failed_model_call_is_an_error_and_exits_nonzero(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    case = next(item for item in CASES if not item.expected)
    result = run_case(RaisingModel(), case)
    assert result["tool_choice_passed"] is None and "HTTP 401" in result["error"]
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    assert runner.main(["--live", "--case", case.name], lambda _key: RaisingModel()) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["tool_choice_failures"] == []
    assert summary["errors"] == [{"case": case.name, "error": result["error"]}]


def test_an_unsendable_reply_makes_the_exit_code_nonzero(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    bad = dict(PLANS)
    bad["cancel-unclear"] = ([], "Nothing was cancelled. Your visit is Thu Oct 15 at 9:45 AM.")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    assert runner.main(["--live", "--case", "cancel-unclear"],
                       lambda _key: ScriptedModel(bad)) == 1
    assert json.loads(capsys.readouterr().out)["reply_check_failures"] == ["cancel-unclear"]


def test_refs_match_case_insensitively_and_by_full_appointment_id() -> None:
    want = Step("prepare_cancellation", (("ref", "b202b202"),))
    for ref in ("B202B202", "b202b202-0000-4000-8000-000000000000"):
        assert tool_choice_ok((want,), [{"name": "prepare_cancellation", "args": {"ref": ref}}])
    assert not tool_choice_ok((want,), [{"name": "prepare_cancellation",
                                         "args": {"ref": "c303c303"}}])
    case = next(item for item in CASES if item.name == "cancel-yes")
    full = runner.build_tools(case)("prepare_cancellation", {
        "ref": "B202B202-0000-4000-8000-000000000000"})
    assert full["prepared"] is True


def test_stand_in_refusals_have_production_shapes() -> None:
    by_name = {case.name: case for case in CASES}
    prepared = runner.build_tools(by_name["cancel-ambiguous-visit"])(
        "prepare_cancellation", {"ref": "zzzz9999"})
    assert prepared["reason"] == "visit_not_found" and len(prepared["visits"]) == 2
    assert set(prepared["visits"][0]) == {"ref", "when", "status"}
    booked = runner.build_tools(by_name["book-accepted-offer"])(
        "request_booking", {"date": "2026-10-13", "time": "15:00"})
    assert booked["reason"] == "not_an_offered_time" and len(booked["offered"]) == 2
    move = runner.build_tools(by_name["move-accepted-time"])(
        "request_booking", {"date": "2026-10-16", "time": "09:00"})
    assert move["reason"] == "offer_is_for_a_move"
