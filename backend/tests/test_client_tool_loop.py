"""The verified client's bounded read-tool loop (#340), with a scripted model.

Boundary: the conversation service, the in-memory calendar and offer memory, and a
scripted model that stands in for the model's tool choices and final text; the receipt
processor for replay. A scripted model proves what the backend allows and checks, not how
the real model chooses among tools or words an answer. No live model, SMS, or AWS is used.
"""

import json
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from test_conversation_flow import NOW, THURSDAY, TOMORROW, ZONE, Harness, Script, ask
from test_sms_processing import Reader

from scheduling.domain.availability import LocalWindow, pilot_policy
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.client_tools import MAX_TOOL_CALLS, check_reply
from scheduling.domain.conversation import ConversationOutcome
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.conversation_state import ConversationState, PromptKind
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

Tools = Callable[[str, dict[str, Any]], dict[str, Any]]


class LoopScript(Script):
    """Runs ``plan(tools)`` as the model's tool use and final text for each client message."""

    def __init__(self, plan: Callable[[Tools], str] | None = None) -> None:
        super().__init__()
        self.plan = plan or (lambda _tools: "Hello.")
        self.loops: list[tuple[str, datetime, str, tuple[HistoryMessage, ...]]] = []
        self.rejected: list[str | None] = []

    def run_client_loop(self, body: str, now: datetime, timezone: str,
                        history: tuple[HistoryMessage, ...], tool: Tools,
                        check: Callable[[str], str | None]) -> str:
        self.loops.append((body, now, timezone, history))
        text = self.plan(tool)
        self.rejected.append(check(text))
        return text


def chat_with(plan: Callable[[Tools], str]) -> tuple[Harness, LoopScript]:
    model = LoopScript(plan)
    return Harness(model), model


def second_client(chat: Harness) -> None:
    chat.store.save_profile(ClientProfile(
        "pilot", "client-2", "Blake Example", "+14155550102", "2 Test Street",
        True, 1, NOW, NOW, NOW), 0, None)


def untouched(chat: Harness) -> None:
    assert chat.calendar() == ()
    assert chat.states.read_state("pilot", "+14155550101") is None


def test_business_question_reads_the_saved_profile_and_hours_without_writing() -> None:
    seen: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        info = tools("get_business_info", {})
        seen.append(info)
        return ("We offer Standard cleaning, about 2 hours. Mon-Fri 8:00 AM-5:00 PM Pacific. "
                "The owner approves every request.")

    chat, model = chat_with(plan)
    reply = chat.text("What do you do and when are you open?")
    assert reply.text.startswith("We offer Standard cleaning")
    assert not reply.committed and model.rejected == [None]
    assert model.calls == ["What do you do and when are you open?"]  # Classified once.
    info = seen[0]
    assert info["service"]["name"] == "Standard cleaning"
    assert info["service"]["duration_minutes"] == 120
    assert info["business"]["name"] == "Example Cleaning Co."
    assert info["hours"]["weekly"][0] == {"weekday": "Monday", "hours": ["8:00 AM-5:00 PM"]}
    assert info["hours"]["weekly"][5]["hours"] == []
    # Columbus Day is inside the 14-day booking range from Tue Sep 29.
    assert {"date": "2026-10-12", "weekday": "Monday",
            "reason": "holiday"} in info["hours"]["closures_in_booking_range"]
    assert info["booking"]["owner_approval_required"] is True
    untouched(chat)


def test_loop_receives_local_time_and_the_sent_aware_history() -> None:
    chat, model = chat_with(lambda tools: (tools("get_business_info", {}), "Open weekdays.")[1])
    chat.service._history_reader = type("H", (), {
        "read_conversation_history": lambda _self, _receipt, _now: (
            HistoryMessage("SM-0", "assistant", NOW, "Earlier reply"),)})()
    chat.text("hours?")
    _, now, timezone, history = model.loops[0]
    assert (now.utcoffset(), timezone) == (timedelta(hours=-7), "America/Los_Angeles")
    assert now.hour == 10 and history[0].text == "Earlier reply"


def test_unconfigured_profile_is_reported_not_invented() -> None:
    seen: list[dict[str, Any]] = []
    chat, _ = chat_with(lambda tools: (seen.append(tools("get_business_info", {})),
                                       "I do not have those details yet.")[1])
    chat.store.set_business_profile_for_test("other-business")  # Not this business.
    chat.store._profiles.pop("pilot")
    chat.text("What services do you offer?")
    assert seen[0]["configured"] is False and seen[0]["service"] is None
    avail: list[dict[str, Any]] = []
    chat2, _ = chat_with(lambda tools: (avail.append(tools("get_availability", {
        "from": TOMORROW, "to": TOMORROW, "time_from": None, "time_to": None})),
        "I can't check times right now.")[1])
    chat2.store._profiles.pop("pilot")
    chat2.text("Anything tomorrow?")
    assert avail[0] == {"ok": False, "error": "service_not_configured"}
    untouched(chat2)


def test_availability_stores_an_offer_a_later_reply_can_accept_and_books_nothing_itself() -> None:
    offered: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        result = tools("get_availability", {"from": TOMORROW, "to": TOMORROW,
                                            "time_from": None, "time_to": None})
        offered.append(result)
        options = result["offer"]["options"]
        listed = ", ".join(f"{item['number']}) {item['time']}" for item in options)
        return (f"Open on {options[0]['when'].split(' at ')[0]}: {listed}. Reply with a "
                "number or time. The owner approves every request.")

    chat, model = chat_with(plan)
    offer = chat.text("Do you have anything tomorrow?")
    assert model.rejected == [None] and not offer.committed
    assert chat.calendar() == ()  # The read tool created no booking.
    state = chat.states.read_state("pilot", "+14155550101")
    assert state is not None and state.kind == PromptKind.OFFER
    assert state.duration_minutes == 120
    assert [start.astimezone(ZONE).strftime("%I:%M %p").lstrip("0") for start in state.options] \
        == [item["time"] for item in offered[0]["offer"]["options"]]
    assert offered[0]["offer"]["valid_minutes"] == 30
    assert offered[0]["days"][0]["open"] is True

    # A later accepting reply is read by the model, which may call request_booking (#341).
    model.plan = lambda tools: (
        tools("request_booking", {"date": TOMORROW, "time": "10:00"}),
        "Requested Wed Sep 30 at 10:00 AM (ref " + chat.calendar()[0][0][:8]
        + "). It is pending owner approval, not confirmed yet.")[1]
    booked = chat.text("10 works")
    assert booked.committed and "pending owner approval" in booked.text
    assert chat.status(booked.appointment_id or "") == CalendarStatus.PENDING_APPROVAL
    assert len(model.loops) == 2


def test_no_opening_reasons_are_truthful_for_weekend_holiday_and_past_days() -> None:
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        for first, last in (("2026-10-03", "2026-10-04"), ("2026-10-12", "2026-10-12"),
                            ("2026-09-28", "2026-09-28")):
            results.append(tools("get_availability", {
                "from": first, "to": last, "time_from": None, "time_to": None}))
        return "Nothing is open then."

    chat, _ = chat_with(plan)
    chat.text("Anything this weekend, Columbus Day, or Monday?")
    assert [day["reason"] for day in results[0]["days"]] == ["outside_business_hours"] * 2
    assert results[1]["days"][0]["reason"] == "holiday"
    assert results[2]["days"][0]["reason"] == "date_passed"
    assert all(result["offer"] is None for result in results)
    untouched(chat)


def test_a_fully_reserved_day_says_reserved_by_owner_without_detail() -> None:
    results: list[dict[str, Any]] = []
    chat, _ = chat_with(lambda tools: (results.append(tools("get_availability", {
        "from": "2026-10-01", "to": "2026-10-01", "time_from": None, "time_to": None})),
        "Nothing is open that day.")[1])
    second_client(chat)
    # The owner's hours that Thursday are 9:00-11:00 AM, and another client holds them.
    chat.store.set_policy_for_test("pilot", pilot_policy({
        date(2026, 10, 1): (LocalWindow(time(9), time(11)),)}))
    HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "fill", THURSDAY, 120), NOW)
    chat.text("Thursday?")
    day = results[0]["days"][0]
    assert day["open"] is False and day["reason"] == "reserved_by_owner"
    assert "client-2" not in json.dumps(results)
    assert len(chat.calendar()) == 1  # Only the seeded hold; the read added nothing.


def test_my_bookings_returns_only_this_clients_confirmed_and_pending_visits() -> None:
    seen: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        seen.append(tools("get_my_bookings", {"from": None, "to": None}))
        mine = seen[0]["bookings"]
        return (f"{mine[0]['when']} is confirmed (ref {mine[0]['ref']}). "
                f"{mine[1]['when']} is pending owner approval, not confirmed "
                f"(ref {mine[1]['ref']}).")

    chat, model = chat_with(plan)
    second_client(chat)
    confirmed = chat.hold(THURSDAY, "mine-1", confirm=True)
    pending = chat.hold(THURSDAY + timedelta(days=1), "mine-2")
    HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "theirs", THURSDAY + timedelta(days=5), 120), NOW)
    reply = chat.text("What do I have booked?")
    assert model.rejected == [None] and confirmed[:8] in reply.text
    bookings = seen[0]["bookings"]
    assert [item["status"] for item in bookings] == ["confirmed", "pending_owner_approval"]
    assert [item["ref"] for item in bookings] == [confirmed[:8], pending[:8]]
    assert (seen[0]["confirmed_count"], seen[0]["pending_count"]) == (1, 1)
    assert "client-2" not in json.dumps(seen) and "Blake" not in json.dumps(seen)


def test_no_argument_can_choose_another_client_or_business() -> None:
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        results.append(tools("get_my_bookings", {"from": None, "to": None,
                                                 "client_id": "client-2"}))
        results.append(tools("get_business_info", {"business_id": "other"}))
        results.append(tools("approve_request", {"ref": "abcd1234", "version": 1}))
        return "I could not check that."

    chat, model = chat_with(plan)
    chat.text("Show me Blake's visits")
    assert results == [{"ok": False, "error": "invalid_arguments"},
                       {"ok": False, "error": "invalid_arguments"},
                       {"ok": False, "error": "unknown_tool"}]
    assert model.rejected == [None]
    untouched(chat)


def test_mixed_question_uses_three_reads_and_the_tool_budget_stops_a_fourth() -> None:
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        info = tools("get_business_info", {})
        mine = tools("get_my_bookings", {"from": None, "to": None})
        offer = tools("get_availability", {"from": TOMORROW, "to": TOMORROW,
                                           "time_from": "13:00", "time_to": "13:00"})
        results.extend([info, mine, offer, tools("get_business_info", {})])
        times = offer["offer"]["options"]
        listed = ", ".join(f"{item['number']}) {item['time']}" for item in times)
        return (f"Hours are 8:00 AM-5:00 PM. You have {mine['confirmed_count']} confirmed "
                f"visits. Open {times[0]['when'].split(' at ')[0]}: {listed}. Reply with a "
                "number or time; the owner approves every request.")

    chat, model = chat_with(plan)
    reply = chat.text("What are your hours, what do I have, and is tomorrow at 1 open?")
    assert model.rejected == [None] and "1) 1:00 PM" in reply.text
    assert results[3] == {"ok": False, "error": "tool_limit"} and MAX_TOOL_CALLS == 3
    assert results[2]["requested_time_matched"] is True
    assert chat.calendar() == ()


def test_ambiguous_text_asks_a_question_and_reads_or_stores_nothing() -> None:
    chat, model = chat_with(lambda _tools: "Do you want to check your visits or request a "
                                           "new one?")
    reply = chat.text("Appointment Monday?")
    assert reply.text.startswith("Do you want to check")
    assert model.rejected == [None] and not reply.committed
    untouched(chat)


@pytest.mark.parametrize("text", [
    "I booked you for Wed Sep 30 at 10:00 AM.",
    "Done! I've requested that time for you.",
    "Your visit has been cancelled.",
    "Another client has that time, so I put you down for the next one.",
    "Sorry, that time is booked by someone else.",
    "We added you to Fri Oct 2 at 10:00 AM.",
    "Your visit is now scheduled.",
])
def test_a_text_claiming_a_write_is_never_sent(text: str) -> None:
    chat, model = chat_with(lambda tools: (tools("get_availability", {
        "from": TOMORROW, "to": TOMORROW, "time_from": None, "time_to": None}), text)[1])
    chat.model.replies["Can you book tomorrow?"] = ask("availability", TOMORROW)
    reply = chat.text("Can you book tomorrow?")
    assert reply.text != text and model.rejected[0] is not None
    assert reply.text.startswith("Open times on Wed Sep 30: 1) ")  # Deterministic fallback.
    assert chat.calendar() == ()


def test_a_time_no_tool_returned_is_never_sent_and_leaves_no_stale_offer() -> None:
    def plan(tools: Tools) -> str:
        tools("get_availability", {"from": TOMORROW, "to": TOMORROW,
                                   "time_from": None, "time_to": None})
        return ("Open Wed Sep 30: 1) 4:45 PM. Reply with a number. The owner approves "
                "every request.")

    chat, model = chat_with(plan)
    chat.model.replies["tomorrow?"] = ask("availability", TOMORROW)
    reply = chat.text("tomorrow?")
    assert "4:45 PM" not in reply.text and model.rejected[0] is not None
    state = chat.states.read_state("pilot", "+14155550101")
    # The fallback stored its own offer, which its own text lists; the loop's was cleared.
    assert state is not None and state.kind == PromptKind.OFFER
    assert all(start.astimezone(ZONE).strftime("%I:%M %p").lstrip("0") in reply.text
               for start in state.options)


def test_an_offer_must_be_listed_whole_in_order_numbered_and_pending_approval() -> None:
    def plan_with(text_for: Callable[[list[dict[str, Any]]], str]) -> LoopScript:
        def plan(tools: Tools) -> str:
            result = tools("get_availability", {"from": TOMORROW, "to": TOMORROW,
                                                "time_from": None, "time_to": None})
            return text_for(result["offer"]["options"])
        return LoopScript(plan)

    cases = {
        "dropped": lambda o: f"Open: 1) {o[0]['time']}. The owner approves every request.",
        "unnumbered": lambda o: "Open Sep 30: " + ", ".join(i["time"] for i in o)
        + ". The owner approves every request.",
        "reversed": lambda o: "Open Sep 30: " + ", ".join(
            f"{i['number']}) {i['time']}" for i in reversed(o))
        + ". The owner approves every request.",
        "not_pending": lambda o: "Open Sep 30: " + ", ".join(
            f"{i['number']}) {i['time']}" for i in o) + ". Reply with a number.",
    }
    for name, text_for in cases.items():
        model = plan_with(text_for)
        chat = Harness(model)
        chat.text("anything tomorrow?")
        assert model.rejected[0] is not None, name


def test_tool_errors_are_structured_and_the_reply_may_only_say_so() -> None:
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        results.append(tools("get_availability", {"from": "2026-10-05", "to": "2026-09-30",
                                                  "time_from": None, "time_to": None}))
        results.append(tools("get_availability", {"from": "2026-09-01", "to": "2026-10-30",
                                                  "time_from": None, "time_to": None}))
        return "I could not check those days. Nothing changed."

    chat, _ = chat_with(plan)
    reply = chat.text("Anything in the next month?")
    assert results == [{"ok": False, "error": "invalid_arguments"}] * 2
    assert reply.text == "I could not check those days. Nothing changed."
    untouched(chat)

    broken, _ = chat_with(lambda tools: (tools("get_availability", {
        "from": TOMORROW, "to": TOMORROW, "time_from": None, "time_to": None}),
        "Open Wed Sep 30: 1) 10:00 AM.")[1])

    def fail(_business: str) -> Any:
        raise OSError("profile unavailable")

    broken.store.read_business_profile = fail  # type: ignore[method-assign]
    broken.model.replies["tomorrow?"] = ask("availability", TOMORROW)
    answer = broken.text("tomorrow?")  # The tool error leaves a time with no source.
    assert answer.text == "I can't check current openings right now. Please try again later."


def test_a_model_failure_falls_back_to_the_deterministic_answer() -> None:
    def plan(tools: Tools) -> str:
        raise TimeoutError("budget")

    chat, _ = chat_with(plan)
    chat.model.replies["tomorrow?"] = ask("availability", TOMORROW)
    reply = chat.text("tomorrow?")
    assert reply.text.startswith("Open times on Wed Sep 30: 1) ") and not reply.committed
    assert chat.calendar() == ()


def test_an_old_open_offer_is_closed_by_a_loop_answer_that_offers_nothing() -> None:
    chat, _ = chat_with(lambda tools: (tools("get_business_info", {}), "We are open weekdays.")[1])
    chat.model.replies["Anything tomorrow?"] = ask("availability", TOMORROW)
    chat.text("Anything tomorrow?")  # Loop plan reads business info only: no offer stored.
    chat.service._interpreter = LoopScript(
        lambda tools: (tools("get_business_info", {}), "We are open weekdays.")[1])
    chat.states.put_state(ConversationState(
        "pilot", "+14155550101", "SM-old", PromptKind.OFFER, NOW, NOW + timedelta(minutes=30),
        (THURSDAY,), client_id="client-1", duration_minutes=120))
    reply = chat.text("What are your hours?")
    assert reply.text.endswith("I closed my earlier question, so nothing was booked or cancelled.")
    assert chat.states.read_state("pilot", "+14155550101") is None
    assert not chat.text("yes").committed  # A bare yes cannot reach the closed offer.
    assert chat.calendar() == ()


def test_booking_cancel_and_reschedule_requests_keep_their_existing_paths() -> None:
    chat, model = chat_with(lambda _tools: "should not run")
    confirmed = chat.hold(THURSDAY, "keep", confirm=True)
    chat.model.replies["cancel my visit"] = ask("cancel", target_date="2026-10-01")
    prompt = chat.text("cancel my visit")
    assert "Reply YES" in prompt.text or "cancel" in prompt.text.lower()
    assert model.loops == []
    assert chat.status(confirmed) == CalendarStatus.CONFIRMED


def test_replay_of_one_receipt_runs_the_loop_and_stores_the_reply_once() -> None:
    chat, model = chat_with(lambda tools: (tools("get_business_info", {}), "Open weekdays.")[1])
    inbound = InboundReceipt("pilot", "SM-loop-1", "+14155550101", "+14155550000", "hours?",
                             NOW, SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    store = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(store, chat.service, "pilot", lambda: NOW)
    first = processor.process("pilot", inbound.provider_id)
    assert isinstance(first, ConversationOutcome) and first.text == "Open weekdays."
    assert processor.process("pilot", inbound.provider_id) is None
    assert store.replies == [(inbound.provider_id, "Open weekdays.")]
    assert len(model.loops) == 1


def test_opted_out_client_never_reaches_the_model() -> None:
    chat, model = chat_with(lambda _tools: "should not run")
    chat.service._consent.is_opted_out = lambda *_args: True  # type: ignore[method-assign]
    assert chat.text("hours?").text == "This sender is opted out of scheduling texts."
    assert model.loops == [] and model.calls == []


def test_custom_policy_hours_and_closures_are_reported_as_saved() -> None:
    seen: list[dict[str, Any]] = []
    chat, _ = chat_with(lambda tools: (seen.append(tools("get_business_info", {})),
                                       "We close Friday Oct 2.")[1])
    chat.store.set_policy_for_test("pilot", pilot_policy({
        date(2026, 10, 2): (), date(2026, 10, 3): (LocalWindow(
            time(9), time(12)),)}))
    reply = chat.text("Are you open Friday Oct 2?")
    hours = seen[0]["hours"]
    assert {"date": "2026-10-02", "weekday": "Friday", "reason": "closed"} \
        in hours["closures_in_booking_range"]
    assert hours["special_hours_in_booking_range"] == [
        {"date": "2026-10-03", "weekday": "Saturday", "hours": ["9:00 AM-12:00 PM"]}]
    assert reply.text == "We close Friday Oct 2."


def _message(text: str) -> dict[str, Any]:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def _call(name: str, call_id: str, arguments: str = "{}") -> dict[str, Any]:
    return {"output": [{"type": "function_call", "name": name, "call_id": call_id,
                        "arguments": arguments}]}


def test_openai_client_loop_bounds_tools_and_rewrites_once_when_the_check_fails(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from io import BytesIO

    from scheduling.adapters.openai_messages import (
        CLIENT_LOOP_BUDGET_SECONDS,
        CLIENT_LOOP_MAX_CALLS,
        CLIENT_LOOP_TOOLS,
        OpenAIMessageInterpreter,
    )

    responses = iter([_call("get_business_info", "a"), _call("get_business_info", "b"),
                      _call("get_my_bookings", "c", '{"from":null,"to":null}'),
                      _message("Booked it for 4:45 PM."), _message("Open weekdays.")])
    payloads: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float) -> BytesIO:
        payloads.append(json.loads(request.data))
        return BytesIO(json.dumps(next(responses)).encode())

    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen", fake_urlopen)
    names: list[str] = []
    reply = OpenAIMessageInterpreter("synthetic-key", timeout_seconds=8).run_client_loop(
        "hours?", datetime(2026, 9, 29, 10, tzinfo=ZONE), "America/Los_Angeles", (),
        lambda name, _args: names.append(name) or {"ok": True},
        lambda text: "claims a change" if "Booked" in text else None)
    assert reply == "Open weekdays." and len(payloads) == CLIENT_LOOP_MAX_CALLS
    assert [item["tool_choice"] for item in payloads] == ["auto"] * 3 + ["none"] * 2
    assert names == ["get_business_info", "get_business_info", "get_my_bookings"]
    assert {tool["name"] for tool in CLIENT_LOOP_TOOLS} == {
        "get_business_info", "get_availability", "get_my_bookings",
        "request_booking"}  # The one write; the backend decides whether it books (#341).
    assert all(payload["store"] is False for payload in payloads)
    assert "Local time: 10:00" in payloads[0]["input"][0]["content"]
    assert CLIENT_LOOP_BUDGET_SECONDS + 8 < 60  # Classification call plus loop fit the worker.


def test_openai_client_loop_rejects_an_unknown_tool_and_a_second_bad_text(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from io import BytesIO

    from scheduling.adapters.openai_messages import OpenAIMessageInterpreter

    model = OpenAIMessageInterpreter("synthetic-key")
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_a, **_k: BytesIO(json.dumps(
                            _call("cancel_visit", "x")).encode()))
    with pytest.raises(ValueError, match="malformed"):
        model.run_client_loop("hi", datetime(2026, 9, 29, 10, tzinfo=ZONE), "UTC", (),
                              lambda _n, _a: {}, lambda _t: None)
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_a, **_k: BytesIO(json.dumps(_message("No.")).encode()))
    with pytest.raises(ValueError, match="revision"):
        model.run_client_loop("hi", datetime(2026, 9, 29, 10, tzinfo=ZONE), "UTC", (),
                              lambda _n, _a: {}, lambda _t: "bad")


def _offer_check(starts: tuple[datetime, ...], text: str) -> str | None:
    facts = {"ok": True, "text": [
        f"{start.astimezone(ZONE).strftime('%a %b')} {start.astimezone(ZONE).day} "
        f"at {start.astimezone(ZONE).strftime('%I:%M %p').lstrip('0')}" for start in starts]}
    return check_reply(text, [facts], starts, ZONE)


def test_option_numbers_and_dates_must_match_the_stored_options() -> None:
    day = datetime(2026, 10, 16, 17, tzinfo=ZONE)  # Fri Oct 16, 10:00 AM local.
    two_days = (day.replace(hour=10), day.replace(day=17, hour=9))
    two_times = (day.replace(hour=10), day.replace(hour=13))
    ok = " The owner approves every request."
    assert _offer_check(two_days, "1) Fri Oct 16 at 10:00 AM 2) Sat Oct 17 at 9:00 AM." + ok) is None
    assert _offer_check(two_days, "1) Sat Oct 17 at 10:00 AM 2) Fri Oct 16 at 9:00 AM." + ok)
    assert _offer_check(two_days, "Fri Oct 16: 1) 10:00 AM. Sat Oct 17: 2) 9:00 AM." + ok) is None
    assert _offer_check(two_days, "Fri Oct 16: 1) 10:00 AM, 2) 9:00 AM." + ok)
    assert _offer_check(two_times, "Fri Oct 16: 1) 10:00 AM or 2) 1:00 PM." + ok) is None
    assert _offer_check(two_times, "Fri Oct 16: 2) 10:00 AM or 1) 1:00 PM." + ok)
    assert _offer_check(two_times, "Fri Oct 16: 1) 1:00 PM or 2) 10:00 AM." + ok)
    assert _offer_check(two_times[:1], "Fri Oct 16 at 10:00 AM." + ok) is None
    assert _offer_check(two_times[:1], "Sat Oct 17 at 10:00 AM." + ok)
