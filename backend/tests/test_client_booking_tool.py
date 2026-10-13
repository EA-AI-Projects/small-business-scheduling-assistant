"""request_booking in the verified client's tool loop (#341), with a scripted model.

Boundary: the conversation service, the in-memory calendar, offer memory, hold service, and a
scripted model that stands in for the model's tool choices and final text; the receipt
processor for replay. It proves what the backend allows, rechecks, and checks in the final
text, not how the real model reads an acceptance. No live model, SMS, or AWS is used.
"""

import json
from datetime import date, datetime, timedelta
from typing import Any

from test_client_tool_loop import LoopScript, Tools, chat_with, second_client
from test_conversation_flow import NOW, TOMORROW, ZONE, Harness, ask
from test_sms_processing import Reader

from scheduling.domain.availability import pilot_policy
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_tools import check_reply
from scheduling.domain.conversation import ConversationOutcome
from scheduling.domain.conversation_state import ConversationState, PromptKind
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

OFFER_TEXT = ("Open on Wed Sep 30: 1) 8:00 AM, 2) 10:00 AM, 3) 12:00 PM, 4) 1:00 PM, "
              "5) 3:00 PM. Reply with a number or time. The owner approves every request.")


def offer_plan(tools: Tools) -> str:
    tools("get_availability", {"from": TOMORROW, "to": TOMORROW,
                               "time_from": None, "time_to": None})
    return OFFER_TEXT


def accept(tools: Tools, clock: str = "10:00", day: str = TOMORROW) -> dict[str, Any]:
    return tools("request_booking", {"date": day, "time": clock})


def offered(chat: Harness, model: LoopScript) -> None:
    model.plan = offer_plan
    reply = chat.text("Anything tomorrow?")
    assert not reply.committed and model.rejected == [None]
    assert chat.calendar() == ()


def requested_text(chat: Harness) -> str:
    ref = chat.calendar()[0][0][:8]
    return (f"Requested Wed Sep 30 at 10:00 AM (ref {ref}). It is pending owner approval, "
            "not confirmed yet.")


def test_accepting_an_offered_time_creates_one_pending_request_and_says_pending() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        results.append(accept(tools))
        return requested_text(chat)

    model.plan = plan
    reply = chat.text("the 10 o'clock one please")
    assert model.rejected[-1] is None
    assert reply.committed and reply.client_outbox_id is not None
    assert reply.reply_result is None  # The checked loop text is delivered as written.
    assert reply.appointment_id is not None
    assert chat.status(reply.appointment_id) == CalendarStatus.PENDING_APPROVAL
    assert len(chat.calendar()) == 1
    assert results[0]["requested"] is True and results[0]["confirmed"] is False
    assert results[0]["ref"] == reply.appointment_id[:8]
    assert "pending owner approval" in reply.text and "confirmed yet" in reply.text
    assert chat.states.read_state("pilot", "+14155550101") is None  # The offer is used up.
    # Once booked, the same offer cannot be accepted a second time.
    model.plan = lambda tools: (results.append(accept(tools)), "Nothing was booked.")[1]
    again = chat.text("the 10 o'clock one please")
    assert not again.committed and results[-1]["reason"] == "no_offer_to_accept"
    assert len(chat.calendar()) == 1


def test_a_first_request_offers_times_and_cannot_book_in_the_same_message() -> None:
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        tools("get_availability", {"from": TOMORROW, "to": TOMORROW,
                                   "time_from": "10:00", "time_to": "10:00"})
        results.append(accept(tools))
        return ("Wed Sep 30 at 10:00 AM is open. Reply YES to request it. "
                "The owner approves every request.")

    chat, model = chat_with(plan)
    reply = chat.text("Can I get tomorrow at 10?")
    assert results == [{"ok": False, "error": "offer_not_sent_yet"}]
    assert not reply.committed and chat.calendar() == ()
    state = chat.states.read_state("pilot", "+14155550101")
    assert state is not None and state.kind == PromptKind.OFFER and model.rejected == [None]


def test_no_open_offer_means_nothing_changed() -> None:
    results: list[dict[str, Any]] = []
    chat, _ = chat_with(lambda tools: (results.append(accept(tools)),
                                       "Nothing was booked. Tell me a day.")[1])
    reply = chat.text("yes please")
    assert results[0]["requested"] is False and results[0]["reason"] == "no_offer_to_accept"
    assert not reply.committed and chat.calendar() == ()


def test_a_changed_visit_length_books_nothing_at_either_length() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    results: list[dict[str, Any]] = []
    chat.store.set_business_profile_for_test("pilot", 90)  # Owner changed it after the offer.
    model.plan = lambda tools: (results.append(accept(tools)),
                                "The visit length changed, so nothing was booked.")[1]
    reply = chat.text("10 works")
    assert results[0]["reason"] == "visit_length_changed"
    assert not reply.committed and model.rejected[-1] is None
    assert chat.calendar() == ()
    assert chat.states.read_state("pilot", "+14155550101") is None


def test_changed_hours_close_the_offered_time() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    results: list[dict[str, Any]] = []
    chat.store.set_policy_for_test("pilot", pilot_policy({date(2026, 9, 30): ()}))
    model.plan = lambda tools: (results.append(accept(tools)),
                                "That time is no longer open, so nothing was booked.")[1]
    reply = chat.text("10 works")
    assert results[0]["reason"] == "outside_current_hours"
    assert not reply.committed and chat.calendar() == ()


def test_a_taken_time_is_reserved_by_owner_and_discloses_no_other_client() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    second_client(chat)
    HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "taken", datetime(2026, 9, 30, 17, tzinfo=NOW.tzinfo),
        120), NOW)
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(accept(tools)),
                                "That time is reserved by owner, so nothing was booked.")[1]
    reply = chat.text("10 works")
    assert results[0]["reason"] == "reserved_by_owner"
    assert "client-2" not in json.dumps(results) and not reply.committed
    assert len(chat.calendar()) == 1  # Only the other client's hold.
    assert chat.states.read_state("pilot", "+14155550101") is None
    # A reply that blames another client is not deliverable.
    assert check_reply("Another client booked that, so nothing was booked.", results, (),
                       ZONE) is not None


def test_an_expired_offer_cannot_be_accepted() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    chat.now = NOW + timedelta(minutes=31)
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(accept(tools)),
                                "That offer expired, so nothing was booked.")[1]
    chat.model.replies["go with the late morning slot"] = ask(
        "request_booking", TOMORROW, TOMORROW, "10:00", "10:00")
    reply = chat.text("go with the late morning slot")
    assert results[0]["reason"] == "offer_expired"
    assert not reply.committed and chat.calendar() == ()
    assert chat.states.read_state("pilot", "+14155550101") is None


def test_an_ambiguous_acceptance_books_nothing_and_keeps_the_offer_open() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        results.append(accept(tools, "11:00"))  # Not one of the offered times.
        return "Which time do you mean, 1) 8:00 AM or 2) 10:00 AM? Nothing was booked."

    model.plan = plan
    reply = chat.text("the morning one")
    assert results[0]["reason"] == "not_an_offered_time"
    assert [item["time"] for item in results[0]["offered"]][:2] == ["8:00 AM", "10:00 AM"]
    assert not reply.committed and chat.calendar() == ()
    assert "closed my earlier question" not in reply.text
    state = chat.states.read_state("pilot", "+14155550101")
    assert state is not None and state.kind == PromptKind.OFFER
    # A clear answer to the follow-up question then books.
    model.plan = lambda tools: (accept(tools), requested_text(chat))[1]
    assert chat.text("10").committed and len(chat.calendar()) == 1


def test_a_model_that_cannot_choose_asks_and_calls_no_tool() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    model.plan = lambda tools: "Which offered time would you like? Nothing was booked."
    reply = chat.text("whichever is best")
    assert not reply.committed and chat.calendar() == ()
    state = chat.states.read_state("pilot", "+14155550101")
    assert state is not None and state.kind == PromptKind.OFFER


def test_another_clients_or_a_reschedule_offer_is_never_accepted_here() -> None:
    chat, model = chat_with(lambda tools: "unused")
    results: list[dict[str, Any]] = []
    model.plan = lambda tools: (results.append(accept(tools)), "Nothing was booked.")[1]
    start = datetime(2026, 9, 30, 17, tzinfo=NOW.tzinfo)
    chat.states.put_state(ConversationState(
        "pilot", "+14155550101", "SM-old", PromptKind.OFFER, NOW, NOW + timedelta(minutes=30),
        (start,), client_id="client-2", duration_minutes=120))
    chat.text("what about that one?")
    assert results[0]["reason"] == "no_offer_to_accept"
    assert chat.calendar() == ()


def test_a_reply_that_calls_the_request_confirmed_is_replaced_by_the_pending_notice() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    model.plan = lambda tools: (accept(tools), "You're booked! Your visit is confirmed.")[1]
    reply = chat.text("10 works")
    assert model.rejected[-1] is not None
    assert reply.committed and len(chat.calendar()) == 1
    assert reply.text.startswith("Requested Wed Sep 30 at 10:00 AM (ref ")
    assert "pending owner approval" in reply.text


def test_a_loop_failure_after_the_write_still_sends_the_pending_notice_once() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)

    def plan(tools: Tools) -> str:
        accept(tools)
        raise TimeoutError("budget")

    model.plan = plan
    reply = chat.text("10 works")
    assert reply.committed and reply.text.startswith("Requested Wed Sep 30 at 10:00 AM")
    assert len(chat.calendar()) == 1


def test_a_refused_booking_whose_loop_then_fails_does_not_try_again() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    second_client(chat)
    HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "taken", datetime(2026, 9, 30, 17, tzinfo=NOW.tzinfo),
        120), NOW)

    def plan(tools: Tools) -> str:
        accept(tools)
        raise TimeoutError("budget")

    model.plan = plan
    reply = chat.text("10 works")
    assert not reply.committed and "nothing was booked" in reply.text
    assert len(chat.calendar()) == 1


def test_a_service_question_and_a_booking_request_in_one_text() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    seen: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        seen.append(tools("get_business_info", {}))
        seen.append(accept(tools))
        return ("The Standard cleaning visit is about 2 hours. "
                + requested_text(chat))

    model.plan = plan
    reply = chat.text("How long is the visit? And yes, 10 works.")
    assert model.rejected[-1] is None and reply.committed
    assert seen[0]["service"]["duration_minutes"] == 120 and seen[1]["requested"] is True
    assert reply.text.startswith("The Standard cleaning visit is about 2 hours.")
    assert len(chat.calendar()) == 1


def test_a_repeated_receipt_replays_one_request_and_one_reply() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    model.plan = lambda tools: (accept(tools), requested_text(chat))[1]
    inbound = InboundReceipt("pilot", "SM-accept-1", "+14155550101", "+14155550000",
                             "10 works", NOW, SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    store = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(store, chat.service, "pilot", lambda: NOW)
    first = processor.process("pilot", inbound.provider_id)
    assert isinstance(first, ConversationOutcome) and first.committed
    assert processor.process("pilot", inbound.provider_id) is None
    assert len(store.committed_drafts) == 1 and len(chat.calendar()) == 1

    # A crash after the write but before the offer was cleared: the receipt replays the same
    # hold instead of creating a second one.
    start = datetime(2026, 9, 30, 17, tzinfo=NOW.tzinfo)
    chat.states.put_state(ConversationState(
        "pilot", "+14155550101", "SM-offer", PromptKind.OFFER, NOW,
        NOW + timedelta(minutes=30), (start,), client_id="client-1", duration_minutes=120))
    model.plan = lambda tools: (accept(tools), requested_text(chat))[1]
    replay = chat.service.handle(inbound)
    assert replay.committed and replay.appointment_id == first.appointment_id
    assert len(chat.calendar()) == 1


def test_the_tool_rejects_malformed_and_extra_arguments() -> None:
    chat, model = chat_with(offer_plan)
    offered(chat, model)
    results: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        results.append(tools("request_booking", {"date": TOMORROW}))
        results.append(tools("request_booking", {"date": TOMORROW, "time": "10am"}))
        results.append(tools("request_booking", {"date": TOMORROW, "time": "10:00",
                                                 "client_id": "client-2"}))
        return "I could not check that. Nothing changed."

    model.plan = plan
    chat.text("10 works")
    assert results == [{"ok": False, "error": "invalid_arguments"}] * 3
    assert chat.calendar() == ()


def test_reply_check_requires_pending_wording_and_the_reference() -> None:
    ok = {"ok": True, "requested": True, "ref": "abcd1234", "date": "2026-09-30",
          "time": "10:00 AM", "when": "Wed Sep 30 at 10:00 AM"}
    check = lambda text: check_reply(text, [ok], (), ZONE)
    assert check("Requested Wed Sep 30 at 10:00 AM (ref abcd1234). Pending owner approval.") is None
    assert check("Requested Wed Sep 30 at 10:00 AM (ref abcd1234). Pending approval, "
                 "not confirmed yet.") is None
    assert check("Your visit Wed Sep 30 at 10:00 AM (ref abcd1234) is confirmed. Pending "
                 "owner approval.") is not None
    assert check("Requested Wed Sep 30 at 10:00 AM (ref abcd1234).") is not None
    assert check("Pending owner approval for Wed Sep 30 at 10:00 AM.") is not None
    none = {"ok": True, "requested": False, "reason": "offer_expired"}
    assert check_reply("That expired. Nothing was booked.", [none], (), ZONE) is None
    assert check_reply("That expired.", [none], (), ZONE) is not None
    assert check_reply("I requested it for you. Nothing was booked.", [none], (), ZONE) is not None
