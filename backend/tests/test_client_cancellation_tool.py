"""Client cancellation in the verified client's tool loop (#342), with a scripted model.

Boundary: the conversation service, the in-memory calendar, confirmation memory, lifecycle
service, and a scripted model that stands in for the model's tool choices and final text;
the receipt processor for replay. It proves what the backend binds, rechecks, writes once,
and checks in the final text, not how the real model reads a yes or no. No live model, SMS,
or AWS is used.
"""

from datetime import timedelta
from typing import Any

from test_client_tool_loop import LoopScript, Tools, chat_with, second_client
from test_conversation_flow import NOW, THURSDAY, TOMORROW, ZONE, Harness, ask
from test_sms_processing import Reader

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_tools import check_reply
from scheduling.domain.conversation import ConversationOutcome
from scheduling.domain.conversation_state import PromptKind
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

SENDER = "+14155550101"
ASK_TEXT = "Cancel your Thu Oct 1 at 9:00 AM visit? Reply YES to cancel; this lasts 30 minutes."


def state(chat: Harness) -> Any:
    return chat.states.read_state("pilot", SENDER)


def prepare(tools: Tools, ref: str | None = None) -> dict[str, Any]:
    if ref is None:
        ref = tools("get_my_bookings", {"from": None, "to": None})["bookings"][0]["ref"]
    return tools("prepare_cancellation", {"ref": ref})


def asked(plan_text: str = ASK_TEXT) -> tuple[Harness, LoopScript, str]:
    chat, model = chat_with(lambda tools: (prepare(tools), plan_text)[1])
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I can't make Thursday"] = ask("cancel", target_date="2026-10-01")
    reply = chat.text("I can't make Thursday")
    assert not reply.committed and model.rejected == [None]
    return chat, model, visit


def cancelled_text(visit: str) -> str:
    return f"Cancelled your Thu Oct 1 at 9:00 AM visit (ref {visit[:8]})."


def test_prepare_binds_one_visit_and_version_and_a_later_yes_cancels_it_once() -> None:
    chat, model, visit = asked()
    saved = state(chat)
    assert saved.kind == PromptKind.CONFIRM_CANCEL and saved.appointment_id == visit
    assert saved.appointment_version == 2 and saved.expires_at - saved.created_at == timedelta(
        minutes=30)
    assert chat.status(visit) == CalendarStatus.CONFIRMED  # Asking cancels nothing.
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("confirm_cancellation", {})),
                                cancelled_text(visit))[1]
    done = chat.text("yes please")
    assert model.rejected[-1] is None and done.committed
    assert done.text == cancelled_text(visit) and done.client_outbox_id is not None
    assert results[0]["cancelled"] is True and results[0]["ref"] == visit[:8]
    assert chat.status(visit) == CalendarStatus.CANCELLED and state(chat) is None
    model.plan = lambda tools: (results.append(tools("confirm_cancellation", {})),
                                "Nothing was cancelled; no cancellation is waiting.")[1]
    again = chat.text("yes")
    assert not again.committed and results[-1]["reason"] == "no_open_confirmation"
    version = chat.store.read_appointment(visit)
    assert version is not None and version.version == 3  # One cancellation, not two.


def test_declining_closes_the_question_and_the_visit_stays_booked() -> None:
    chat, model, visit = asked()
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("dismiss_cancellation", {})),
                                ("OK, your Thu Oct 1 at 9:00 AM visit stays booked. "
                                 "Nothing was cancelled."))[1]
    kept = chat.text("no, keep it")
    assert model.rejected[-1] is None and not kept.committed
    assert results[0]["dismissed"] is True and results[0]["still_booked"] is True
    assert state(chat) is None and chat.status(visit) == CalendarStatus.CONFIRMED
    model.plan = lambda tools: (results.append(tools("confirm_cancellation", {})),
                                "Nothing was cancelled.")[1]
    chat.text("yes")  # The closed question cannot be confirmed later.
    assert results[-1]["reason"] == "no_open_confirmation"
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_an_unknown_other_or_ambiguous_visit_asks_nothing_and_discloses_nothing() -> None:
    results: list[dict[str, Any]] = []
    chat, model = chat_with(lambda tools: (
        results.append(tools("prepare_cancellation", {"ref": "deadbeef"})),
        results.append(prepare(tools, other[0][:8])),
        "I could not tell which visit. Nothing was cancelled. Which day is it?")[2])
    mine = chat.hold(THURSDAY, "mine", confirm=True)
    second_client(chat)
    other = [HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "theirs", THURSDAY + timedelta(days=1), 120),
        NOW).hold_id]
    chat.model.replies["cancel it"] = ask("cancel")
    reply = chat.text("cancel it")
    assert model.rejected == [None] and not reply.committed
    assert [item["reason"] for item in results] == ["visit_not_found", "visit_not_found"]
    assert results[1]["visits"] == [{"ref": mine[:8], "when": "Thu Oct 1 at 9:00 AM",
                                     "status": "confirmed"}]  # Only the sender's own.
    assert other[0][:8] not in str(results) and state(chat) is None
    assert chat.status(mine) == CalendarStatus.CONFIRMED


def test_a_visit_changed_after_the_question_is_not_cancelled() -> None:
    chat, model, visit = asked()
    LifecycleService(chat.store, lambda: NOW).apply(AppointmentCommand(
        "pilot", visit, "owner", ActorRole.OWNER, Action.EDIT, "owner-edit", 2,
        duration_minutes=90))
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("confirm_cancellation", {})),
                                ("That visit changed, so nothing was cancelled. Which visit "
                                 "do you mean?"))[1]
    reply = chat.text("yes")
    assert model.rejected[-1] is None and not reply.committed
    assert results[0]["cancelled"] is False and results[0]["reason"] == "visit_changed"
    assert chat.status(visit) == CalendarStatus.CONFIRMED and state(chat) is None


def test_an_expired_question_cancels_nothing_and_a_late_yes_does_not_reach_the_tool() -> None:
    chat, model, visit = asked()
    chat.now += timedelta(minutes=31)
    loops = len(model.loops)
    late = chat.text("yes")
    assert not late.committed and "expired" in late.text and len(model.loops) == loops
    assert chat.status(visit) == CalendarStatus.CONFIRMED and state(chat) is None


def test_a_newer_prompt_makes_the_old_confirmation_stale() -> None:
    chat, model, visit = asked()
    model.plan = lambda tools: (tools("get_availability", {
        "from": TOMORROW, "to": TOMORROW, "time_from": None, "time_to": None}),
        ("Open on Wed Sep 30: 1) 8:00 AM, 2) 10:00 AM, 3) 12:00 PM, 4) 1:00 PM, "
         "5) 3:00 PM. Reply with a number or time. The owner approves every request."))[1]
    chat.text("what about tomorrow?")
    assert state(chat).kind == PromptKind.OFFER
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("confirm_cancellation", {})),
                                "Nothing was cancelled; I have no cancellation waiting.")[1]
    reply = chat.text("yes")
    assert not reply.committed and results[0]["reason"] == "no_open_confirmation"
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    assert state(chat).kind == PromptKind.OFFER  # An unrelated open question is untouched.


def test_no_answer_leaves_the_visit_booked_and_the_question_open_until_it_expires() -> None:
    chat, model, visit = asked()
    model.plan = lambda _tools: "Just reply YES to cancel or NO to keep your visit."
    reply = chat.text("hmm let me think")
    assert not reply.committed and model.rejected[-1] is None
    assert state(chat).kind == PromptKind.CONFIRM_CANCEL
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    chat.now += timedelta(minutes=31)
    assert not chat.text("sure").committed
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_a_pending_request_is_cancelled_with_the_same_explicit_confirmation() -> None:
    pending = ""
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        results.append(prepare(tools, pending[:8]))
        return ("Cancel your Thu Oct 1 at 9:00 AM request (still pending owner approval)? "
                "Reply YES to cancel.")

    chat, model = chat_with(plan)
    pending = chat.hold(THURSDAY, "pending")
    chat.model.replies["cancel the request"] = ask("cancel")
    chat.text("cancel the request")
    assert results[0]["status"] == "pending_owner_approval"
    assert chat.status(pending) == CalendarStatus.PENDING_APPROVAL
    model.plan = lambda tools: (tools("confirm_cancellation", {}),
                                (f"Cancelled your Thu Oct 1 at 9:00 AM request "
                                 f"(ref {pending[:8]})."))[1]
    done = chat.text("yes")
    assert done.committed and chat.status(pending) == CalendarStatus.CANCELLED
    assert chat.calendar() == ()  # The held time is released.


def test_a_confirmed_visit_with_a_move_request_waiting_is_not_cancelled() -> None:
    chat, _ = chat_with(lambda tools: (
        results.append(prepare(tools)),
        "A move request is waiting, so nothing was cancelled. Withdraw it first.")[1])
    results: list[dict[str, Any]] = []
    original = chat.hold(THURSDAY, "visit", confirm=True)
    HoldService(chat.store).create(CreateHold(
        "pilot", "client-1", "client-1", "move", THURSDAY + timedelta(days=1), 120,
        replaces_appointment_id=original, replaces_confirmed_version=2), NOW)
    chat.model.replies["cancel Thursday"] = ask("cancel")
    chat.text("cancel Thursday")
    assert results[0]["reason"] == "move_request_pending" and state(chat) is None
    assert chat.status(original) == CalendarStatus.CONFIRMED


def test_a_move_request_that_appears_after_the_question_blocks_the_write() -> None:
    chat, model, visit = asked()
    HoldService(chat.store).create(CreateHold(
        "pilot", "client-1", "client-1", "move", THURSDAY + timedelta(days=1), 120,
        replaces_appointment_id=visit, replaces_confirmed_version=2), NOW)
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(tools("confirm_cancellation", {})),
                                "A move request is waiting, so nothing was cancelled.")[1]
    reply = chat.text("yes")
    assert not reply.committed and results[0]["reason"] in ("move_request_pending",
                                                            "visit_changed")
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_only_one_action_takes_effect_in_a_message() -> None:
    chat, model, visit = asked()
    seen: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        seen.append(prepare(tools, visit[:8]))
        seen.append(tools("confirm_cancellation", {}))
        seen.append(tools("get_availability", {
            "from": TOMORROW, "to": TOMORROW, "time_from": None, "time_to": None}))
        return ASK_TEXT

    model.plan = plan
    chat.text("cancel it, yes cancel it")
    assert seen[1] == {"ok": False, "error": "cancellation_question_not_sent_yet"}
    assert seen[2] == {"ok": False, "error": "cancellation_in_progress"}
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    model.plan = lambda tools: (
        seen.append(tools("get_availability", {
            "from": TOMORROW, "to": TOMORROW, "time_from": None, "time_to": None})),
        seen.append(prepare(tools, visit[:8])), "x")[2]
    chat.text("anything tomorrow? and cancel Thursday")
    assert seen[-1] == {"ok": False, "error": "offer_not_sent_yet"}


def test_malformed_cancellation_arguments_are_rejected() -> None:
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        results.append(tools("prepare_cancellation", {}))
        results.append(tools("prepare_cancellation", {"ref": "ab"}))
        results.append(tools("confirm_cancellation", {"ref": "deadbeef"}))
        return "I could not check. Nothing was cancelled."

    chat, model = chat_with(plan)
    chat.hold(THURSDAY, "visit", confirm=True)
    chat.text("cancel it")
    assert results == [{"ok": False, "error": "invalid_arguments"}] * 3
    assert model.rejected == [None]


def test_a_loop_failure_after_the_write_still_sends_the_cancellation_notice_once() -> None:
    chat, model, visit = asked()

    def plan(tools: Tools) -> str:
        tools("confirm_cancellation", {})
        raise TimeoutError("budget")

    model.plan = plan
    loops = len(model.loops)
    reply = chat.text("yes")
    assert reply.committed and reply.text == cancelled_text(visit)
    assert len(model.loops) - loops == 1
    assert chat.status(visit) == CalendarStatus.CANCELLED


def test_a_refused_cancellation_whose_loop_then_fails_does_not_try_again() -> None:
    chat, model, visit = asked()
    LifecycleService(chat.store, lambda: NOW).apply(AppointmentCommand(
        "pilot", visit, "owner", ActorRole.OWNER, Action.EDIT, "owner-edit", 2,
        duration_minutes=90))
    attempts: list[Any] = []
    original = chat.service._lifecycle.apply
    chat.service._lifecycle.apply = lambda command: (  # type: ignore[method-assign]
        attempts.append(command), original(command))[1]

    def plan(tools: Tools) -> str:
        tools("confirm_cancellation", {})
        raise TimeoutError("budget")

    model.plan = plan
    reply = chat.text("yes")
    assert not reply.committed and "nothing was cancelled" in reply.text
    assert attempts == [] and chat.status(visit) == CalendarStatus.CONFIRMED


def test_a_failed_question_loop_falls_back_to_the_deterministic_confirmation() -> None:
    def plan(tools: Tools) -> str:
        prepare(tools)
        raise TimeoutError("budget")

    chat, model = chat_with(plan)
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I can't make Thursday"] = ask("cancel", target_date="2026-10-01")
    reply = chat.text("I can't make Thursday")
    assert reply.text == "Cancel your Thu Oct 1 at 9:00 AM visit? Reply YES to confirm."
    assert len(model.loops) == 1 and state(chat).appointment_id == visit
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_a_failed_confirmation_loop_without_a_tool_call_uses_the_deterministic_yes() -> None:
    chat, model, visit = asked()

    def plan(_tools: Tools) -> str:
        raise TimeoutError("budget")

    model.plan = plan
    reply = chat.text("yes")
    assert reply.committed and chat.status(visit) == CalendarStatus.CANCELLED


def test_a_premature_or_unbacked_cancellation_claim_is_replaced_before_sending() -> None:
    chat, model, visit = asked()
    model.plan = lambda tools: (prepare(tools), "Done, your Thu Oct 1 visit is cancelled.")[1]
    chat.model.replies["cancel Thursday"] = ask("cancel", target_date="2026-10-01")
    reply = chat.text("cancel Thursday")  # The checked text was refused, so none was sent.
    assert model.rejected[-1] is not None and "cancelled" not in reply.text.lower().replace(
        "nothing was cancelled", "")
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_a_repeated_receipt_replays_one_cancellation_and_one_reply() -> None:
    chat, model, visit = asked()
    model.plan = lambda tools: (tools("confirm_cancellation", {}), cancelled_text(visit))[1]
    inbound = InboundReceipt("pilot", "SM-yes-1", SENDER, "+14155550000", "yes", NOW,
                             SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    store = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(store, chat.service, "pilot", lambda: NOW)
    first = processor.process("pilot", inbound.provider_id)
    assert isinstance(first, ConversationOutcome) and first.committed
    assert processor.process("pilot", inbound.provider_id) is None
    assert len(store.committed_drafts) == 1
    version = chat.store.read_appointment(visit)
    assert version is not None and version.version == 3


def test_a_business_question_and_a_cancellation_answer_in_one_text() -> None:
    chat, model, visit = asked()
    seen: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        seen.append(tools("get_business_info", {}))
        seen.append(tools("confirm_cancellation", {}))
        return ("The Standard cleaning visit is about 2 hours. "
                + cancelled_text(visit))

    model.plan = plan
    reply = chat.text("How long is a visit? Anyway yes, cancel it.")
    assert model.rejected[-1] is None and reply.committed
    assert seen[1]["cancelled"] is True and chat.status(visit) == CalendarStatus.CANCELLED


# -- the final-text check ----------------------------------------------------

ASKED = {"ok": True, "prepared": True, "ref": "abcd1234", "date": "2026-10-01",
         "time": "9:00 AM", "when": "Thu Oct 1 at 9:00 AM"}
DONE = {"ok": True, "cancelled": True, "ref": "abcd1234", "date": "2026-10-01",
        "time": "9:00 AM", "when": "Thu Oct 1 at 9:00 AM"}
KEPT = {"ok": True, "dismissed": True, "still_booked": True, "ref": "abcd1234",
        "when": "Thu Oct 1 at 9:00 AM"}
REFUSED = {"ok": True, "cancelled": False, "nothing_changed": True, "reason": "visit_changed"}


def verdict(text: str, *results: dict[str, Any]) -> str | None:
    return check_reply(text, list(results), (), ZONE)


def test_reply_check_distinguishes_cancelled_still_booked_and_unchanged() -> None:
    assert verdict("Cancel your Thu Oct 1 at 9:00 AM visit? Reply YES to cancel.", ASKED) is None
    assert verdict("I cancelled your Thu Oct 1 at 9:00 AM visit.", ASKED)  # Not yet.
    assert verdict("Your Thu Oct 1 visit is cancelled. Reply YES to cancel?", ASKED)
    assert verdict("Do you want to cancel your visit? Reply YES.", ASKED)  # No time named.
    assert verdict("Cancel your Thu Oct 1 at 9:00 AM visit. Reply YES.", ASKED)  # No question.
    assert verdict("Cancelled your Thu Oct 1 at 9:00 AM visit (ref abcd1234).", DONE) is None
    assert verdict("I cancelled your Thu Oct 1 at 9:00 AM visit (ref abcd1234).", DONE) is None
    assert verdict("Cancelled your Thu Oct 1 at 9:00 AM visit.", DONE)  # No reference.
    assert verdict("Your visit (ref abcd1234) is still booked. Cancelled.", DONE)
    assert verdict("Your Thu Oct 1 at 9:00 AM visit stays booked. Nothing was cancelled.",
                   KEPT) is None
    assert verdict("Your Thu Oct 1 at 9:00 AM visit stays booked.", KEPT)  # Not said.
    assert verdict("That visit changed, so nothing was cancelled.", REFUSED) is None
    assert verdict("That visit changed, so it is cancelled.", REFUSED)
    assert verdict("Nothing was cancelled and your visit is still booked.", REFUSED)
    assert verdict("Sorry, that did not work.", REFUSED)  # Does not say nothing changed.
    assert verdict("All set, your visit is cancelled.")  # No tool made a cancellation.
    assert verdict("Nothing was cancelled.") is None


def test_the_model_cannot_claim_a_confirmed_booking_after_a_cancellation() -> None:
    assert verdict("I booked you in for Thu Oct 1 at 9:00 AM. Cancelled ref abcd1234.", DONE)

