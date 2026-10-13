"""Client rescheduling in the verified client's tool loop (#343), with a scripted model.

Boundary: the conversation service, the in-memory calendar, offer memory, hold and lifecycle
services, and a scripted model standing in for the model's tool choices and final text; the
receipt processor for replay. It proves what the backend binds to the visit and its version,
rechecks, writes once, and checks in the final text, not how the real model chooses tools or
words an answer. No live model, SMS, or AWS is used.
"""

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from test_client_tool_loop import LoopScript, Tools, chat_with, second_client
from test_conversation_flow import NOW, THURSDAY, Harness, ask
from test_sms_processing import Reader

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_tools import check_reply
from scheduling.domain.conversation import ConversationOutcome
from scheduling.domain.conversation_state import ConversationState, PromptKind
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

SENDER = "+14155550101"
FRIDAY = "2026-10-02"
STAYS = "Your Thu Oct 1 at 9:00 AM visit stays confirmed until the owner approves."


def state(chat: Harness) -> Any:
    return chat.states.read_state("pilot", SENDER)


def offer_move(tools: Tools, ref: str | None = None, **extra: Any) -> dict[str, Any]:
    if ref is None:
        ref = tools("get_my_bookings", {"from": None, "to": None})["bookings"][0]["ref"]
    return tools("get_availability", {
        "from": FRIDAY, "to": FRIDAY, "time_from": None, "time_to": None, "ref": ref, **extra})


def listing(result: dict[str, Any], day: str = "Fri Oct 2") -> str:
    options = result["offer"]["options"]
    listed = ", ".join(f"{item['number']}) {item['time']}" for item in options)
    return f"{day} is open: {listed}. The owner approves every request."


def offer_text(result: dict[str, Any]) -> str:
    return f"To move your visit, {listing(result)} Reply with a number or time. {STAYS}"


def move_offered() -> tuple[Harness, LoopScript, str, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    chat, model = chat_with(lambda tools: (seen.append(offer_move(tools)),
                                           offer_text(seen[-1]))[1])
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["move my visit to Friday"] = ask("reschedule", target_date="2026-10-01")
    reply = chat.text("move my visit to Friday")
    assert not reply.committed and model.rejected == [None]
    return chat, model, visit, seen


def requested_text(ref: str, when: str = "Fri Oct 2 at 10:00 AM") -> str:
    return (f"Requested a move to {when} (ref {ref}). It is pending owner approval. {STAYS}")


def test_offer_binds_the_visit_and_version_then_a_request_keeps_the_original_confirmed() -> None:
    chat, model, visit, seen = move_offered()
    saved = state(chat)
    assert saved.kind == PromptKind.OFFER and saved.appointment_id == visit
    assert saved.appointment_version == 2 and saved.duration_minutes == 120
    assert seen[0]["replaces"] == {"ref": visit[:8], "status": "confirmed",
                                   "when": "Thu Oct 1 at 9:00 AM",
                                   "stays_confirmed_until_owner_approves": True}
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    assert len(chat.calendar()) == 1  # Offering booked nothing.
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": visit[:8], "date": FRIDAY, "time": "10:00"})),
        requested_text(results[0]["ref"]))[1]
    moved = chat.text("2")
    assert model.rejected[-1] is None and moved.committed
    assert results[0]["requested"] is True and results[0]["confirmed"] is False
    assert results[0]["replaces_ref"] == visit[:8]
    assert chat.status(visit) == CalendarStatus.CONFIRMED  # Until the owner approves.
    new = chat.store.read_appointment(moved.appointment_id or "")
    assert new is not None and new.status == CalendarStatus.PENDING_APPROVAL
    assert new.replaces_appointment_id == visit and state(chat) is None
    # The owner's approval is what swaps the visits.
    LifecycleService(chat.store, lambda: NOW).apply(AppointmentCommand(
        "pilot", new.appointment_id, "owner", ActorRole.OWNER, Action.APPROVE, "ok-move",
        new.version))
    assert chat.status(new.appointment_id) == CalendarStatus.CONFIRMED
    assert chat.status(visit) == CalendarStatus.CANCELLED


def test_an_unknown_other_pending_or_ambiguous_visit_stores_no_offer() -> None:
    results: list[dict[str, Any]] = []
    chat, model = chat_with(lambda tools: (
        results.append(offer_move(tools, "deadbeef")),
        results.append(offer_move(tools, other[0][:8])),
        results.append(offer_move(tools, pending[:8])),
        "I could not tell which visit to move. Nothing changed. Which one is it?")[3])
    chat.hold(THURSDAY, "mine", confirm=True)
    pending = chat.hold(THURSDAY + timedelta(days=7), "waiting")
    second_client(chat)
    other = [HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "theirs", THURSDAY + timedelta(days=1), 120),
        NOW).hold_id]
    chat.model.replies["move it"] = ask("reschedule")
    reply = chat.text("move it")
    assert model.rejected == [None] and not reply.committed
    assert [item["error"] for item in results] == [
        "visit_not_found", "visit_not_found", "visit_not_confirmed"]
    assert other[0][:8] not in str(results) and state(chat) is None


def test_a_visit_edited_after_the_offer_cannot_be_moved() -> None:
    chat, model, visit, _ = move_offered()
    LifecycleService(chat.store, lambda: NOW).apply(AppointmentCommand(
        "pilot", visit, "owner", ActorRole.OWNER, Action.EDIT, "owner-edit", 2,
        duration_minutes=90))
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": visit[:8], "date": FRIDAY, "time": "10:00"})),
        "That visit changed, so nothing was booked. Which visit do you want to move?")[1]
    reply = chat.text("2")
    assert model.rejected[-1] is None and not reply.committed
    assert results[0]["requested"] is False and results[0]["reason"] == "visit_changed"
    assert chat.status(visit) == CalendarStatus.CONFIRMED and state(chat) is None
    assert len(chat.calendar()) == 1


def test_a_replaced_offer_and_an_expired_offer_cannot_be_used() -> None:
    chat, model, visit, _ = move_offered()
    results: list[dict[str, Any]] = []
    # A newer offer for another day replaces the first one; its old times are not offered.
    model.plan = lambda tools: (results.append(tools("get_availability", {
        "from": "2026-10-05", "to": "2026-10-05", "time_from": None, "time_to": None,
        "ref": visit[:8]})), f"{listing(results[-1], 'Mon Oct 5')} {STAYS}")[1]
    chat.text("what about Monday")
    model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": visit[:8], "date": FRIDAY, "time": "10:00"})), "Nothing changed.")[1]
    chat.text("the Friday 10 one")
    assert results[-1]["reason"] == "not_an_offered_time"
    assert chat.status(visit) == CalendarStatus.CONFIRMED and len(chat.calendar()) == 1
    chat.now += timedelta(minutes=31)
    loops = len(model.loops)
    late = chat.text("1")
    assert not late.committed and len(model.loops) == loops  # Expired: no tool is reached.
    assert len(chat.calendar()) == 1


def test_a_taken_time_is_refused_without_naming_why_and_is_never_retried() -> None:
    chat, model, visit, _ = move_offered()
    second_client(chat)
    HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "taken", THURSDAY + timedelta(days=1, hours=1), 120),
        NOW)
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": visit[:8], "date": FRIDAY, "time": "10:00"})),
        "Booked Fri Oct 2 at 3:00 PM for you.")[1]  # The model then states a time no tool gave.
    reply = chat.text("2")
    assert model.rejected[-1] is not None and not reply.committed
    assert results[0]["requested"] is False and results[0]["reason"] == "reserved_by_owner"
    assert "client-2" not in str(results)
    assert "nothing was booked" in reply.text.lower() and "no longer open" in reply.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED and len(chat.calendar()) == 2
    assert len(model.loops) == 2  # The refusal is not retried by the fallback.


def test_another_move_request_waiting_refuses_a_second_move() -> None:
    chat, model, visit, _ = move_offered()
    HoldService(chat.store).create(CreateHold(
        "pilot", "client-1", "client-1", "portal", THURSDAY + timedelta(days=8), 120, visit,
        replaces_confirmed_version=2), NOW)
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": visit[:8], "date": FRIDAY, "time": "10:00"})),
        "That visit has a move waiting, so nothing was booked.")[1]
    chat.text("2")
    assert results[0]["reason"] == "move_request_pending"
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_the_two_offer_kinds_cannot_stand_in_for_each_other() -> None:
    chat, model, visit, _ = move_offered()
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_booking", {
        "date": FRIDAY, "time": "10:00"})), "Nothing was booked.")[1]
    chat.text("2")
    assert results[0]["reason"] == "offer_is_for_a_move" and state(chat) is not None
    other = Harness(LoopScript())
    other.model.plan = lambda tools: (results.append(tools("get_availability", {
        "from": "2026-09-30", "to": "2026-09-30", "time_from": None, "time_to": None})),
        listing(results[-1], "Wed Sep 30"))[1]
    other.hold(THURSDAY, "visit", confirm=True)
    other.text("anything tomorrow?")
    other.model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": visit[:8], "date": "2026-09-30", "time": "08:00"})), "Nothing changed.")[1]
    other.text("1")
    assert results[-1]["reason"] == "no_move_offer_to_accept"


def test_a_wrong_visit_reference_is_not_the_offered_visit() -> None:
    chat, model, visit, _ = move_offered()
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": "deadbeef", "date": FRIDAY, "time": "10:00"})), "Nothing changed.")[1]
    chat.text("2")
    assert results[0]["reason"] == "not_the_visit_offered_for"
    assert state(chat) is not None and chat.status(visit) == CalendarStatus.CONFIRMED


def test_a_repeated_receipt_requests_the_move_once() -> None:
    chat, model, visit, _ = move_offered()
    refs: list[str] = []

    def plan(tools: Tools) -> str:
        result = tools("request_reschedule", {"ref": visit[:8], "date": FRIDAY, "time": "10:00"})
        refs.append(result["ref"])
        return requested_text(result["ref"])

    model.plan = plan
    inbound = InboundReceipt("pilot", "SM-move-1", SENDER, "+14155550000", "2", NOW,
                             SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    store = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(store, chat.service, "pilot", lambda: NOW)
    first = processor.process("pilot", inbound.provider_id)
    assert isinstance(first, ConversationOutcome) and first.committed
    assert processor.process("pilot", inbound.provider_id) is None
    assert len(store.committed_drafts) == 1 and len(chat.calendar()) == 2


def test_a_mixed_question_and_second_action_takes_only_one_action() -> None:
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        info = tools("get_business_info", {})
        offered = offer_move(tools, visit[:8])
        results.extend([info, offered, tools("prepare_cancellation", {"ref": visit[:8]})])
        return ("We are open weekdays 8:00 AM-5:00 PM Pacific. " + offer_text(offered)
                + " Nothing was cancelled.")

    chat, model = chat_with(plan)
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["hours? and move my visit to Friday, then cancel it"] = ask("reschedule")
    reply = chat.text("hours? and move my visit to Friday, then cancel it")
    assert model.rejected == [None] and not reply.committed
    assert results[2] == {"ok": False, "error": "offer_not_sent_yet"}
    assert state(chat).kind == PromptKind.OFFER and chat.status(visit) == CalendarStatus.CONFIRMED


def test_move_replies_keep_fact_checks_and_leave_status_wording_to_the_model() -> None:
    local = ZoneInfo("America/Los_Angeles")
    done = [{"ok": True, "requested": True, "status": "pending_owner_approval",
             "confirmed": False, "ref": "abcd1234", "replaces_ref": "feed5678",
             "date": FRIDAY, "weekday": "Friday", "time": "10:00 AM",
             "when": "Fri Oct 2 at 10:00 AM", "original_when": "Thu Oct 1 at 9:00 AM"}]
    good = ("Requested a move to Fri Oct 2 at 10:00 AM (ref abcd1234). It is pending owner "
            "approval. Your Thu Oct 1 at 9:00 AM visit stays confirmed until then.")
    assert check_reply(good, done, (), local) is None
    # Status wording is the model's (#359): none of these is judged.
    for free in ("Your visit was moved to Fri Oct 2 at 10:00 AM (ref abcd1234).",
                 "Requested Fri Oct 2 at 10:00 AM (ref abcd1234).",
                 "Your visit moves next Friday."):
        assert check_reply(free, done, (), local) is None, free
    # Absolute dates, times, and references must come from a tool result.
    for bad in (good.replace("Fri Oct 2", "Sat Oct 3"), good.replace("10:00 AM", "11:30 AM"),
                good.replace("abcd1234", "ffff9999"), good.replace("Thu Oct 1", "Wed Sep 30")):
        assert check_reply(bad, done, (), local) is not None, bad
    refused = [{"ok": True, "requested": False, "nothing_changed": True,
                "reason": "visit_changed"}]
    assert check_reply("That visit changed, so nothing was booked.", refused, (), local) is None


def test_a_loop_failure_falls_back_to_the_deterministic_reschedule_question() -> None:
    chat, model = chat_with(lambda tools: (offer_move(tools), "Moved it!")[1])
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["move my visit"] = ask("reschedule", target_date="2026-10-01")
    reply = chat.text("move my visit")
    assert "What day would you like instead" in reply.text and not reply.committed
    assert state(chat) is not None and state(chat).kind == PromptKind.RESCHEDULE_DAY
    assert chat.status(visit) == CalendarStatus.CONFIRMED and len(model.loops) == 1


def test_a_move_question_cannot_become_a_new_booking_offer() -> None:
    results: list[dict[str, Any]] = []
    chat, model = chat_with(lambda _tools: "unused")
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["move my visit"] = ask("reschedule", target_date="2026-10-01")
    chat.states.put_state(ConversationState(
        "pilot", SENDER, "SM-day", PromptKind.RESCHEDULE_DAY, NOW, NOW + timedelta(minutes=30),
        appointment_id=visit, appointment_version=2, client_id="client-1"))
    model.plan = lambda tools: (
        results.append(tools("get_availability", {
            "from": FRIDAY, "to": FRIDAY, "time_from": None, "time_to": None, "ref": None})),
        results.append(tools("get_availability", {
            "from": FRIDAY, "to": FRIDAY, "time_from": None, "time_to": None, "ref": "deadbeef"})),
        "Which visit do you want to move? Nothing changed.")[2]
    chat.text("Friday")
    assert [item["error"] for item in results[-2:]] == ["move_question_open"] * 2
    assert state(chat).kind == PromptKind.RESCHEDULE_DAY
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    model.plan = lambda tools: (results.append(offer_move(tools, visit[:8])),
                                offer_text(results[-1]))[1]
    chat.text("Friday")  # The same visit's ref is the move the question asked about.
    assert state(chat).kind == PromptKind.OFFER and state(chat).appointment_id == visit
