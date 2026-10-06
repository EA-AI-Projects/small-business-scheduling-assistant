"""A verified client's calendar questions are answered from their own current visits only.

Boundary: the conversation service with the in-memory calendar and a scripted model. It
checks routing, the answer text, data isolation, and that nothing is written. It does not
cover the real model's wording or DynamoDB reads.
"""

from datetime import UTC, datetime, timedelta

from test_conversation_flow import NOW, THURSDAY, Harness, ask

from scheduling.domain.calendar import CalendarEvent, CalendarStatus
from scheduling.domain.client_calendar_questions import parse
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

FRIDAY = THURSDAY + timedelta(days=1)  # Fri Oct 2, 9:00 AM local.
NEXT_TUESDAY = THURSDAY + timedelta(days=5)  # Tue Oct 6, 9:00 AM local.


def week_with_visits() -> tuple[Harness, str, str]:
    """Client-1: a confirmed Thursday visit and a pending Friday request."""
    chat = Harness()
    confirmed = chat.hold(THURSDAY, "thu", confirm=True)
    pending = chat.hold(FRIDAY, "fri")
    return chat, confirmed, pending


def unchanged(chat: Harness, before: tuple[tuple[str, str, int], ...]) -> None:
    assert chat.calendar() == before
    assert chat.states.read_state("pilot", "+14155550101") is None


def test_do_i_have_bookings_this_week_lists_confirmed_and_pending_separately() -> None:
    chat, confirmed, pending = week_with_visits()
    before = chat.calendar()
    reply = chat.text("Do I have bookings this week?")
    assert not reply.committed
    assert reply.text == (
        "You have 1 confirmed visit, 1 pending request from Tue Sep 29 to Sun Oct 4:\n"
        f"- Thu Oct 1 at 9:00 AM-11:00 AM PDT, confirmed (ref {confirmed[:8]})\n"
        f"- Fri Oct 2 at 9:00 AM-11:00 AM PDT, pending owner approval, not confirmed yet "
        f"(ref {pending[:8]})")
    assert chat.model.calls == []  # Recognized without the model.
    unchanged(chat, before)


def test_when_are_the_cleaners_coming_lists_every_upcoming_visit() -> None:
    chat, confirmed, _ = week_with_visits()
    later = chat.hold(NEXT_TUESDAY, "tue", confirm=True)
    reply = chat.text("When are the cleaners coming?")
    assert reply.text.startswith("You have 2 confirmed visits, 1 pending request coming up:")
    assert f"Thu Oct 1 at 9:00 AM-11:00 AM PDT, confirmed (ref {confirmed[:8]})" in reply.text
    assert f"Tue Oct 6 at 9:00 AM-11:00 AM PDT, confirmed (ref {later[:8]})" in reply.text
    assert chat.model.calls == []


def test_ambiguous_booking_asks_whether_to_check_or_request() -> None:
    chat, _, _ = week_with_visits()
    before = chat.calendar()
    reply = chat.text("Booking for Friday?")
    assert reply.text == (
        "Do you want to check the visits you already have for Fri Oct 2, or request a new "
        "cleaning? Ask \"Do I have a visit Fri Oct 2?\" or \"What times are open Fri Oct 2?\"")
    assert chat.model.calls == []
    unchanged(chat, before)
    # The model can raise the same question for wording the parser does not know.
    chat.model.replies["About that booking thing"] = ask("clarify_booking")
    assert chat.text("About that booking thing").text.startswith(
        "Do you want to check the visits you already have, or request a new cleaning?")
    unchanged(chat, before)


def test_no_matching_visits_says_so_and_how_to_ask_for_one() -> None:
    chat, _, _ = week_with_visits()
    reply = chat.text("Do I have a cleaning tomorrow?")
    assert reply.text == ("You have no confirmed visits or pending requests on Wed Sep 30. "
                          "To ask for a cleaning, tell me what day works.")
    empty = Harness()
    assert empty.text("When is my next cleaning?").text == (
        "You have no confirmed visits or pending requests coming up. To ask for a cleaning, "
        "tell me what day works.")


def test_status_questions_never_hide_or_promote_a_pending_request() -> None:
    chat, _, pending = week_with_visits()
    count = chat.text("How many confirmed visits this week?")
    assert count.text == ("You have 1 confirmed visit from Tue Sep 29 to Sun Oct 4.\n"
                          "You also have 1 pending request from Tue Sep 29 to Sun Oct 4.")
    friday = chat.text("Is my Friday cleaning confirmed?")
    assert friday.text == (
        "You have no confirmed visits on Fri Oct 2.\n"
        "You also have 1 pending request on Fri Oct 2.\n"
        f"- Fri Oct 2 at 9:00 AM-11:00 AM PDT, pending owner approval, not confirmed yet "
        f"(ref {pending[:8]})")


def test_answers_show_only_this_clients_visits() -> None:
    chat, _, _ = week_with_visits()
    chat.store.save_profile(ClientProfile(
        "pilot", "client-2", "Jordan Sample", "+14155550102", "2 Test Street",
        HomeSize.MEDIUM, 120, True, 1, NOW, NOW, NOW), 0, None)
    other = HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "other", THURSDAY + timedelta(hours=3), 120),
        NOW).hold_id
    reply = chat.text("What's on my schedule this week?")
    assert "1 confirmed visit, 1 pending request" in reply.text
    assert other[:8] not in reply.text
    assert "Jordan" not in reply.text and "12:00 PM" not in reply.text


def test_owner_blocks_are_not_shown_to_a_client() -> None:
    chat = Harness()
    block_start = THURSDAY + timedelta(hours=4)
    chat.store.replace_for_test("pilot", 0, (CalendarEvent(
        "block-1", block_start, block_start + timedelta(hours=2), CalendarStatus.UNAVAILABLE),))
    reply = chat.text("Do I have anything Thursday?")
    assert reply.text == ("You have no confirmed visits or pending requests on Thu Oct 1. "
                          "To ask for a cleaning, tell me what day works.")


def test_each_answer_reads_the_calendar_as_it_is_now() -> None:
    chat, confirmed, pending = week_with_visits()
    assert "1 pending request" in chat.text("Do I have bookings this week?").text
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", pending, "owner", ActorRole.OWNER, Action.APPROVE, "ok-later", 1))
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", confirmed, "client-1", ActorRole.CLIENT, Action.CANCEL, "gone", 2))
    again = chat.text("Do I have bookings this week?")
    assert again.text == (
        "You have 1 confirmed visit from Tue Sep 29 to Sun Oct 4:\n"
        f"- Fri Oct 2 at 9:00 AM-11:00 AM PDT, confirmed (ref {pending[:8]})")


def test_model_proposed_question_uses_only_its_range_and_writes_nothing() -> None:
    chat, confirmed, _ = week_with_visits()
    before = chat.calendar()
    chat.model.replies["Anything on the books for me Thursday?"] = ask(
        "calendar_question", "2026-10-01", "2026-10-01")
    reply = chat.text("Anything on the books for me Thursday?")
    assert reply.text == ("You have 1 confirmed visit on Thu Oct 1:\n"
                          f"- Thu Oct 1 at 9:00 AM-11:00 AM PDT, confirmed (ref {confirmed[:8]})")
    unchanged(chat, before)


def test_a_question_closes_an_open_offer_so_a_later_pick_books_nothing() -> None:
    chat, _, _ = week_with_visits()
    chat.model.replies["Anything Wednesday?"] = ask("availability", "2026-09-30")
    assert "Reply with the number" in chat.text("Anything Wednesday?").text
    before = chat.calendar()
    reply = chat.text("When is my next visit?")
    assert reply.text.endswith("I closed my earlier question, so nothing was booked or "
                               "cancelled. Ask again when you're ready.")
    assert not chat.text("1").committed
    assert chat.calendar() == before


def test_open_times_and_change_requests_keep_their_existing_routes() -> None:
    today = NOW.date()
    for text in ("What times are open Friday?", "Do you have availability tomorrow?",
                 "Can I move my Thursday visit?", "I need to cancel my cleaning",
                 "Anything tomorrow?", "When can the cleaners come Friday?",
                 # A question followed by a change is a change.
                 "When is the cleaning tomorrow? We have to call it off",
                 "Is my visit Friday? Let's drop it", "When is my visit? Remove it please",
                 "What time is my cleaning? Make it 10 am",
                 "When are you coming Friday? Please push it to Monday",
                 # Open times in other words keep the availability offer.
                 "Which day works best for a cleaning next week?",
                 "When is the earliest cleaning next week?",
                 "What's the soonest visit you have Friday?",
                 # Not about the schedule.
                 "How many hours is a cleaning?", "How many cleaners are coming?",
                 "What does a cleaning cost?",
                 # A plain booking request, not a question.
                 "Cleaning Friday", "Visit tomorrow", "A cleaning next week"):
        assert parse(text, today) is None, text


def test_unverified_sender_gets_no_calendar_answer() -> None:
    chat, _, _ = week_with_visits()
    reply = chat.service.handle(InboundReceipt(
        "pilot", "SM-x", "+14155550199", "+14155550000", "Do I have bookings this week?",
        NOW, SenderRole.CLIENT, "client-1", Keyword.OTHER, True))
    assert reply.text == "This sender needs a verified client profile and consent."


def test_dates_are_bounded_to_upcoming_visits_and_one_range() -> None:
    chat, _, _ = week_with_visits()
    assert chat.text("Do I have anything yesterday?").text == (
        "That date has passed. I can check visits that haven't happened yet: ask about "
        "today or a later day.")
    assert chat.text("Do I have anything Friday or next week?").text.startswith(
        "I can check one day or one week at a time.")
    for proposed in (ask("calendar_question", "2026-10-09", "2026-10-02"),
                     ask("calendar_question", "2026-10-01", "2026-12-31")):
        chat.model.replies["Anything on the books for me?"] = proposed
        assert chat.text("Anything on the books for me?").text.startswith(
            "Which day or week do you mean?")


def test_model_path_question_also_closes_an_open_offer() -> None:
    chat, _, _ = week_with_visits()
    chat.model.replies["Anything Wednesday?"] = ask("availability", "2026-09-30")
    chat.text("Anything Wednesday?")
    chat.model.replies["Any idea where I'm at with you all?"] = ask("calendar_question")
    reply = chat.text("Any idea where I'm at with you all?")
    assert reply.text.startswith("You have 1 confirmed visit, 1 pending request coming up:")
    assert reply.text.endswith("nothing was booked or cancelled. Ask again when you're ready.")
    assert chat.states.read_state("pilot", "+14155550101") is None


def test_times_carry_standard_time_after_the_clock_change() -> None:
    chat = Harness()
    chat.now = datetime(2026, 10, 30, 17, tzinfo=UTC)  # Fri Oct 30, 10:00 AM PDT.
    visit = chat.hold(datetime(2026, 11, 3, 17, tzinfo=UTC), "nov", confirm=True)
    assert chat.text("When is my next cleaning?").text == (
        "You have 1 confirmed visit coming up:\n"
        f"- Tue Nov 3 at 9:00 AM-11:00 AM PST, confirmed (ref {visit[:8]})")
