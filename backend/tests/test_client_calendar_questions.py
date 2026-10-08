"""A verified client's calendar questions are answered from their own current visits only.

Boundary: the conversation service with the in-memory calendar and a scripted model that
stands in for the model's reading of each text (range, statuses, list or count). It checks
the answer text, data isolation, and that nothing is written. Whether the real model reads
a given wording or language that way is covered by the synthetic model evaluation
(`backend/evals/scheduling_messages.py`), not here.
"""

from datetime import UTC, datetime, timedelta

import pytest
from test_conversation_flow import NOW, THURSDAY, ZONE, DraftScript, Harness, ask
from test_sms_processing import Reader

from evals.scheduling_messages import CASES
from scheduling.domain.calendar import CalendarEvent, CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.conversation import ConversationOutcome, MessageProposal
from scheduling.domain.conversation_state import PromptKind
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

FRIDAY = THURSDAY + timedelta(days=1)  # Fri Oct 2, 9:00 AM local.
NEXT_TUESDAY = THURSDAY + timedelta(days=5)  # Tue Oct 6, 9:00 AM local.
THIS_WEEK = ("2026-09-28", "2026-10-04")


def cal(first: str | None = None, last: str | None = None,
        statuses: tuple[str, ...] | None = None, view: str | None = None,
        intent: str = "calendar_question", scope: str | None = None) -> MessageProposal:
    """What the model proposes for a calendar question; with no dates the range is kept
    in a follow-up unless ``scope`` says all upcoming."""
    return MessageProposal(intent, None, None, None, False, first, last or first,
                           statuses=statuses, view=view,
                           range_scope=scope or ("dates" if first else "keep"))


def asked(chat: Harness, text: str, proposal: MessageProposal) -> ConversationOutcome:
    chat.model.replies[text] = proposal
    return chat.text(text)


def week_with_visits() -> tuple[Harness, str, str]:
    """Client-1: a confirmed Thursday visit and a pending Friday request."""
    chat = Harness()
    confirmed = chat.hold(THURSDAY, "thu", confirm=True)
    pending = chat.hold(FRIDAY, "fri")
    return chat, confirmed, pending


def unchanged(chat: Harness, before: tuple[tuple[str, str, int], ...]) -> None:
    """No calendar write, and no offer or confirmation is left to answer (#241 keeps only
    the calendar question's range and statuses)."""
    assert chat.calendar() == before
    state = chat.states.read_state("pilot", "+14155550101")
    assert state is None or (state.kind == PromptKind.CALENDAR and not state.options
                             and state.appointment_id is None)


def test_bookings_this_week_lists_confirmed_and_pending_separately() -> None:
    chat, confirmed, pending = week_with_visits()
    before = chat.calendar()
    reply = asked(chat, "Do I have bookings this week?", cal(*THIS_WEEK))
    assert not reply.committed
    assert reply.text == (
        "You have 1 confirmed visit, 1 pending request from Tue Sep 29 to Sun Oct 4:\n"
        f"- Thu Oct 1 at 9:00 AM-11:00 AM PDT, confirmed (ref {confirmed[:8]})\n"
        f"- Fri Oct 2 at 9:00 AM-11:00 AM PDT, pending owner approval, not confirmed yet "
        f"(ref {pending[:8]})")
    unchanged(chat, before)


def test_drafted_calendar_result_keeps_each_visit_status_with_its_reference() -> None:
    model = DraftScript()
    chat = Harness(model)
    confirmed = chat.hold(THURSDAY, "thu", confirm=True)
    pending = chat.hold(FRIDAY, "fri")
    message = "Show my visits this week"
    model.replies[message] = cal(*THIS_WEEK)
    model.drafts[message] = (
        f"Thu Oct 1 at 9:00 AM confirmed ref {confirmed[:8]}; "
        f"Fri Oct 2 at 9:00 AM pending owner approval ref {pending[:8]}.")
    reply = chat.text(message)
    assert reply.text == model.drafts[message]
    result = model.draft_calls[-1][1]
    assert result.kind == "calendar_list" and result.status == "mixed"
    assert tuple(fact.status for fact in result.facts) == ("confirmed", "pending owner approval")
    assert chat.status(confirmed) == CalendarStatus.CONFIRMED
    assert chat.status(pending) == CalendarStatus.PENDING_APPROVAL

    only = "Show confirmed visits tomorrow"
    model.replies[only] = cal("2026-10-01", statuses=("confirmed",))
    model.drafts[only] = f"Thu Oct 1 at 9:00 AM confirmed ref {confirmed[:8]}."
    confirmed_reply = chat.text(only)
    assert confirmed_reply.text == model.drafts[only]
    assert model.draft_calls[-1][1].status == "confirmed"

    filtered = "Show confirmed visits this week"
    model.replies[filtered] = cal(*THIS_WEEK, statuses=("confirmed",))
    model.drafts[filtered] = (
        f"Thu Oct 1 at 9:00 AM confirmed ref {confirmed[:8]}; "
        + f"Fri Oct 2 at 9:00 AM pending owner approval ref {pending[:8]}.")
    filtered_reply = chat.text(filtered)
    assert filtered_reply.text == model.drafts[filtered]
    assert model.draft_calls[-1][1].status == "mixed"
    assert "You also have 1 pending request" in model.draft_calls[-1][1].fallback


def test_a_question_in_spanish_gets_the_same_grounded_answer() -> None:
    chat, confirmed, _ = week_with_visits()
    reply = asked(chat, "¿Tengo citas confirmadas esta semana?",
                  cal(*THIS_WEEK, statuses=("confirmed",)))
    assert reply.text.startswith("You have 1 confirmed visit from Tue Sep 29 to Sun Oct 4:")
    assert confirmed[:8] in reply.text
    assert "You also have 1 pending request" in reply.text


def test_no_range_lists_every_upcoming_visit() -> None:
    chat, confirmed, _ = week_with_visits()
    later = chat.hold(NEXT_TUESDAY, "tue", confirm=True)
    reply = asked(chat, "When are the cleaners coming?", cal())
    assert reply.text.startswith("You have 2 confirmed visits, 1 pending request coming up:")
    assert confirmed[:8] in reply.text and later[:8] in reply.text


def test_long_single_day_calendar_answer_lists_every_visit_within_sms_limit() -> None:
    chat = Harness()
    starts = [THURSDAY - timedelta(hours=1) + timedelta(hours=i)
              for i in range(8)]
    for index, start in enumerate(starts):
        HoldService(chat.store).create(CreateHold(
            "pilot", "client-1", "client-1", f"same-day-{index}", start, 30), NOW)

    chat.model.replies["Tomorrow?"] = ask("availability", "2026-09-30")
    chat.text("Tomorrow?")  # The calendar answer must also close this open offer.
    before = chat.calendar()
    answer = asked(chat, "What visits do I have Thursday?", cal("2026-10-01"))
    assert len(answer.text) <= 500
    assert "pending = awaiting owner approval, not confirmed" in answer.text
    for start in starts:
        assert start.astimezone(ZONE).strftime("%Y-%m-%d %H:%M") in answer.text
    assert "nothing was booked or cancelled" in answer.text
    unchanged(chat, before)


@pytest.mark.parametrize("failure", ["timeout", "malformed"])
def test_failed_final_draft_puts_authoritative_read_answer_in_outbox(failure: str) -> None:
    body = "What times are open tomorrow?"
    baseline = Harness()
    baseline.model.replies[body] = ask("availability", "2026-09-30")
    expected = baseline.text(body).text

    model = DraftScript()
    model.replies[body] = ask("availability", "2026-09-30")
    model.drafts[body] = (TimeoutError("synthetic timeout") if failure == "timeout"
                          else ValueError("synthetic malformed draft"))
    chat = Harness(model)
    receipt = InboundReceipt("pilot", "SM-draft", "+14155550101", "+14155550000",
                             body, chat.now, SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    outbox = Reader({("pilot", "SM-draft"): receipt})
    processor = ReceiptProcessor(outbox, chat.service, "pilot", lambda: chat.now)
    assert processor.process("pilot", "SM-draft") == ConversationOutcome(expected)
    assert [result.kind for _body, result in model.draft_calls] == ["offer_made"]
    assert outbox.replies == [("SM-draft", expected)]
    assert processor.process("pilot", "SM-draft") is None
    assert outbox.replies == [("SM-draft", expected)]
    assert chat.calendar() == ()


@pytest.mark.parametrize("kind", ["availability", "calendar"])
def test_failed_current_read_sends_retry_without_inventing_calendar_facts(
        monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    chat = Harness()
    body = "What times are open tomorrow?" if kind == "availability" else "My visits?"
    chat.model.replies[body] = (ask("availability", "2026-09-30")
                                if kind == "availability" else cal())
    before = chat.calendar()
    read_calendar = chat.store.read_calendar
    calls = 0

    def fail_read(business_id: str) -> object:
        nonlocal calls
        calls += 1
        if kind == "calendar" or calls > 1:
            raise OSError("synthetic calendar read failure")
        return read_calendar(business_id)

    monkeypatch.setattr(chat.store, "read_calendar", fail_read)
    reply = chat.text(body)
    assert "try again later" in reply.text
    assert "open times" not in reply.text.lower()
    assert "confirmed visit" not in reply.text.lower()
    monkeypatch.setattr(chat.store, "read_calendar", read_calendar)
    assert chat.calendar() == before


def test_ambiguous_booking_asks_whether_to_check_or_request() -> None:
    chat, _, _ = week_with_visits()
    before = chat.calendar()
    reply = asked(chat, "Booking for Friday?", cal("2026-10-02", intent="clarify_booking"))
    assert reply.text == (
        "Do you want to check the visits you already have for Fri Oct 2, or request a new "
        "cleaning? Ask \"Do I have a visit Fri Oct 2?\" or \"What times are open Fri Oct 2?\"")
    assert asked(chat, "About that booking thing", cal(intent="clarify_booking")).text \
        .startswith("Do you want to check the visits you already have, or request a new")
    unchanged(chat, before)


def test_no_matching_visits_says_so_and_how_to_ask_for_one() -> None:
    chat, _, _ = week_with_visits()
    assert asked(chat, "Do I have a cleaning tomorrow?", cal("2026-09-30")).text == (
        "You have no confirmed visits or pending requests on Wed Sep 30. "
        "To ask for a cleaning, tell me what day works.")


def test_status_filters_never_hide_or_promote_a_pending_request() -> None:
    chat, _, pending = week_with_visits()
    count = asked(chat, "How many confirmed visits this week?",
                  cal(*THIS_WEEK, statuses=("confirmed",), view="count"))
    assert count.text == ("You have 1 confirmed visit from Tue Sep 29 to Sun Oct 4.\n"
                          "You also have 1 pending request from Tue Sep 29 to Sun Oct 4.")
    friday = asked(chat, "Is my Friday cleaning confirmed?",
                   cal("2026-10-02", statuses=("confirmed",), view="list"))
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
    reply = asked(chat, "What's on my schedule this week?", cal(*THIS_WEEK))
    assert "1 confirmed visit, 1 pending request" in reply.text
    assert other[:8] not in reply.text
    assert "Jordan" not in reply.text and "12:00 PM" not in reply.text


def test_owner_blocks_are_not_shown_to_a_client() -> None:
    chat = Harness()
    block_start = THURSDAY + timedelta(hours=4)
    chat.store.replace_for_test("pilot", 0, (CalendarEvent(
        "block-1", block_start, block_start + timedelta(hours=2), CalendarStatus.UNAVAILABLE),))
    assert asked(chat, "Do I have anything Thursday?", cal("2026-10-01")).text == (
        "You have no confirmed visits or pending requests on Thu Oct 1. "
        "To ask for a cleaning, tell me what day works.")


def test_each_answer_reads_the_calendar_as_it_is_now() -> None:
    chat, confirmed, pending = week_with_visits()
    assert "1 pending request" in asked(chat, "Do I have bookings this week?",
                                        cal(*THIS_WEEK)).text
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", pending, "owner", ActorRole.OWNER, Action.APPROVE, "ok-later", 1))
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", confirmed, "client-1", ActorRole.CLIENT, Action.CANCEL, "gone", 2))
    assert chat.text("Do I have bookings this week?").text == (
        "You have 1 confirmed visit from Tue Sep 29 to Sun Oct 4:\n"
        f"- Fri Oct 2 at 9:00 AM-11:00 AM PDT, confirmed (ref {pending[:8]})")


def test_a_question_closes_an_open_offer_so_a_later_pick_books_nothing() -> None:
    chat, _, _ = week_with_visits()
    assert "Reply with the number" in asked(chat, "Anything Wednesday?",
                                            ask("availability", "2026-09-30")).text
    before = chat.calendar()
    reply = asked(chat, "When is my next visit?", cal())
    assert reply.text.endswith("I closed my earlier question, so nothing was booked or "
                               "cancelled. Ask again when you're ready.")
    assert not chat.text("1").committed
    assert chat.calendar() == before


def test_unverified_sender_gets_no_calendar_answer() -> None:
    chat, _, _ = week_with_visits()
    chat.model.replies["Do I have bookings this week?"] = cal(*THIS_WEEK)
    reply = chat.service.handle(InboundReceipt(
        "pilot", "SM-x", "+14155550199", "+14155550000", "Do I have bookings this week?",
        NOW, SenderRole.CLIENT, "client-1", Keyword.OTHER, True))
    assert reply.text == "This sender needs a verified client profile and consent."
    assert chat.model.calls == []  # The model is not consulted for an unverified sender.


def test_unusable_or_past_ranges_ask_for_a_day() -> None:
    chat, _, _ = week_with_visits()
    assert asked(chat, "Anything yesterday?", cal("2026-09-28")).text == (
        "That date has passed. I can check visits that haven't happened yet: ask about "
        "today or a later day.")
    for proposed in (cal("2026-10-09", "2026-10-02"), cal("2026-10-01", "2026-12-31")):
        assert asked(chat, f"Anything {proposed.date_from}?", proposed).text.startswith(
            "Which day or week do you mean?")


def test_times_carry_standard_time_after_the_clock_change() -> None:
    chat = Harness()
    chat.now = datetime(2026, 10, 30, 17, tzinfo=UTC)  # Fri Oct 30, 10:00 AM PDT.
    visit = chat.hold(datetime(2026, 11, 3, 17, tzinfo=UTC), "nov", confirm=True)
    assert asked(chat, "When is my next cleaning?", cal()).text == (
        "You have 1 confirmed visit coming up:\n"
        f"- Tue Nov 3 at 9:00 AM-11:00 AM PST, confirmed (ref {visit[:8]})")


def _raw(intent: str, first: str | None = None, last: str | None = None,
         statuses: list[str] | None = None, view: str | None = None,
         scope: str | None = None) -> dict[str, object]:
    return {"intent": intent, "request_reference": None, "date_text": None,
            "date_from": first, "date_to": last, "time_from": None, "time_to": None,
            "target_date": None, "owner_decision": None, "needs_clarification": False,
            "question": None, "statuses": statuses, "view": view, "range_scope": scope}


def test_calendar_eval_cases_accept_the_intended_reading_only() -> None:
    """Guards the eval expectations themselves; the live run checks the real model."""
    cases = {case.name: case for case in CASES}
    good = {
        "screenshot-1-bookings-this-week": _raw("calendar_question", *THIS_WEEK),
        "screenshot-2-confirmed-already": _raw("calendar_question", statuses=["confirmed"]),
        "screenshot-3-summary": _raw("calendar_question", view="list"),
        "count-confirmed-next-week": _raw("calendar_question", "2026-10-05", "2026-10-11",
                                          ["confirmed"], "count"),
        "spanish-question": _raw("calendar_question", *THIS_WEEK),
        "spanish-follow-up": _raw("calendar_question", "2026-10-05", "2026-10-11"),
        "follow-up-only-confirmed": _raw("calendar_question", statuses=["confirmed"]),
        "open-times": _raw("availability", "2026-10-02", "2026-10-02"),
        "request-during-calendar-talk": _raw("availability", "2026-10-02", "2026-10-02"),
        "ambiguous-booking": _raw("clarify_booking", "2026-10-02", "2026-10-02"),
        "all-upcoming-after-a-week": _raw("calendar_question", scope="all_upcoming"),
    }
    for name, proposal in good.items():
        assert cases[name].expect(proposal), name
        wrong = (_raw("availability", "2026-10-02", "2026-10-02")
                 if proposal["intent"] != "availability"
                 else _raw("calendar_question", "2026-10-02", "2026-10-02"))
        assert not cases[name].expect(wrong), name
