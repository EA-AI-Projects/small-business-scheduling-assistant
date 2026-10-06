"""Follow-ups to a client's calendar question reread the calendar and never write (#241).

Boundary: the conversation service with the in-memory calendar, conversation memory, and a
scripted model, plus the DynamoDB state adapter against a fake client. It does not cover the
real model's reading of follow-ups.
"""

from datetime import timedelta
from typing import Any

from test_client_calendar_questions import FRIDAY, NEXT_TUESDAY, week_with_visits
from test_conversation_flow import CLARIFY, NOW, Harness, ask
from test_counteroffer_acceptance import World, world  # noqa: F401 - pytest fixture

from scheduling.adapters.conversation_state_dynamodb import DynamoConversationStates
from scheduling.domain.conversation_state import ConversationState, PromptKind
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService

PHONE = "+14155550101"


def approve(chat: Harness, appointment_id: str) -> None:
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", appointment_id, "owner", ActorRole.OWNER, Action.APPROVE,
        f"ok-{appointment_id}", 1))


def test_three_message_sequence_carries_range_and_status_forward() -> None:
    chat, confirmed, pending = week_with_visits()
    later = chat.hold(NEXT_TUESDAY, "tue", confirm=True)
    first = chat.text("Do I have bookings this week?")
    assert first.text.startswith("You have 1 confirmed visit, 1 pending request from Tue Sep 29")
    only = chat.text("Just confirmed ones")
    assert only.text == (
        "You have 1 confirmed visit from Tue Sep 29 to Sun Oct 4:\n"
        f"- Thu Oct 1 at 9:00 AM-11:00 AM PDT, confirmed (ref {confirmed[:8]})\n"
        "You also have 1 pending request from Tue Sep 29 to Sun Oct 4.\n"
        f"- Fri Oct 2 at 9:00 AM-11:00 AM PDT, pending owner approval, not confirmed yet "
        f"(ref {pending[:8]})")
    following = chat.text("What about next week?")  # Keeps "confirmed only".
    assert following.text == (
        "You have 1 confirmed visit from Mon Oct 5 to Sun Oct 11:\n"
        f"- Tue Oct 6 at 9:00 AM-11:00 AM PDT, confirmed (ref {later[:8]})")
    assert chat.text("And Friday?").text.startswith("You have no confirmed visits on Fri Oct 2.")
    assert chat.text("What about my visits Friday?").text.startswith(
        "You have no confirmed visits on Fri Oct 2.")
    summary = chat.text("Summarize my itinerary")  # Every status, for the range just shown.
    assert summary.text.startswith("You have 1 pending request on Fri Oct 2:")
    assert pending[:8] in summary.text
    assert chat.model.calls == []
    assert len(chat.calendar()) == 3


def test_summarize_after_an_upcoming_question_covers_every_upcoming_visit() -> None:
    chat, _, _ = week_with_visits()
    chat.hold(NEXT_TUESDAY, "tue", confirm=True)
    chat.text("When are the cleaners coming?")
    assert chat.text("Summarize my itinerary").text.startswith(
        "You have 2 confirmed visits, 1 pending request coming up:")


def test_a_follow_up_rereads_the_calendar_after_a_change() -> None:
    chat, _, pending = week_with_visits()
    chat.text("Do I have bookings this week?")
    approve(chat, pending)
    reply = chat.text("Just confirmed ones")
    assert reply.text.startswith("You have 2 confirmed visits from Tue Sep 29 to Sun Oct 4:")
    assert "pending" not in reply.text


def test_a_bare_yes_or_number_after_a_calendar_answer_changes_nothing() -> None:
    chat, _, _ = week_with_visits()
    before = chat.calendar()
    chat.text("Do I have bookings this week?")
    for reply in ("yes", "2", "the first one", "no"):
        outcome = chat.text(reply)
        assert outcome.text.startswith("I only listed your visits, so nothing was booked or "
                                       "cancelled."), reply
        assert not outcome.committed
    assert chat.calendar() == before
    assert chat.model.calls == []
    # The calendar conversation is still open for a real follow-up.
    assert chat.text("How many?").text.startswith("You have 1 confirmed visit, 1 pending")


def test_a_question_during_an_open_offer_closes_it_and_a_number_then_books_nothing() -> None:
    chat, _, _ = week_with_visits()
    chat.model.replies["Anything Wednesday?"] = ask("availability", "2026-09-30")
    chat.text("Anything Wednesday?")
    before = chat.calendar()
    answer = chat.text("Do I have bookings this week?")
    assert "I closed my earlier question" in answer.text
    assert chat.text("2").text.startswith("I only listed your visits")
    assert chat.calendar() == before


def test_a_new_request_replaces_the_calendar_conversation() -> None:
    chat, _, _ = week_with_visits()
    chat.text("Do I have bookings this week?")
    chat.model.replies["Can I get a cleaning Wednesday?"] = ask("availability", "2026-09-30")
    assert "Reply with the number" in chat.text("Can I get a cleaning Wednesday?").text
    state = chat.states.read_state("pilot", PHONE)
    assert state is not None and state.kind == PromptKind.OFFER
    assert chat.text("1").committed  # The offer, not the calendar answer, is what is open.


def test_model_follow_up_sees_the_open_range_and_keeps_the_statuses() -> None:
    chat, _, _ = week_with_visits()
    later = chat.hold(NEXT_TUESDAY, "tue", confirm=True)
    chat.text("How many confirmed visits this week?")
    chat.model.replies["And the week after?"] = ask(
        "calendar_question", "2026-10-05", "2026-10-11")
    reply = chat.text("And the week after?")
    assert chat.model.contexts[-1].calendar_range == "2026-09-28 to 2026-10-04"
    assert reply.text == "You have 1 confirmed visit from Mon Oct 5 to Sun Oct 11."
    assert later[:8] not in reply.text  # Still a count, as asked first.
    chat.model.replies["And the week after that"] = ask(
        "calendar_question", "2026-10-12", "2026-10-18")
    assert chat.text("And the week after that").text == (
        "You have 0 confirmed visits from Mon Oct 12 to Sun Oct 18.")
    chat.model.replies["Hmm, which ones were those again"] = ask("calendar_question")
    again = chat.text("Hmm, which ones were those again")
    assert again.text.startswith("You have no confirmed visits from Mon Oct 12 to Sun Oct 18.")


def test_unclear_reply_in_a_calendar_conversation_asks_one_question() -> None:
    chat, _, _ = week_with_visits()
    chat.text("Do I have bookings this week?")
    chat.model.replies["hmm ok whatever"] = CLARIFY
    assert chat.text("hmm ok whatever").text == (
        "I wasn't sure what you meant. Should I check another day or status, or do you want "
        "to request or change a visit?")
    assert chat.text("Just pending").text.startswith("You have 1 pending request")


def test_an_unclear_range_asks_and_keeps_the_conversation_open() -> None:
    chat, _, _ = week_with_visits()
    chat.text("Do I have bookings this week?")
    assert chat.text("What about Friday or next week?").text.startswith(
        "I can check one day or one week at a time.")
    assert chat.text("Just pending").text.startswith(
        "You have 1 pending request from Tue Sep 29 to Sun Oct 4:")


def test_follow_ups_end_after_thirty_minutes() -> None:
    chat, _, _ = week_with_visits()
    chat.text("Do I have bookings this week?")
    chat.now = NOW + timedelta(minutes=30)
    chat.text("Just confirmed ones")
    assert chat.model.calls == ["Just confirmed ones"]  # No longer a follow-up.


def test_calendar_answer_reminds_of_an_open_counteroffer_and_yes_still_accepts_it(
        world: World) -> None:  # noqa: F811 - pytest fixture
    world.offered()
    answer = world.client("Do I have bookings this week?")
    assert answer.text.endswith(
        "The owner's offer of Thu Oct 1 at 2:00 PM is still open: reply YES to request it, "
        "or NO to turn it down.")
    follow = world.client("Just pending")
    assert follow.text.endswith("reply YES to request it, or NO to turn it down.")
    assert world.client("YES").committed


def test_inside_a_calendar_conversation_only_plain_yes_or_no_answers_a_counteroffer(
        world: World) -> None:  # noqa: F811 - pytest fixture
    world.offered()
    before = dict(world.repository._appointments)
    world.client("Do I have bookings this week?")
    for reply in ("Just the confirmed one", "the first one", "1", "confirmed"):
        outcome = world.client(reply)
        assert not outcome.committed, reply
    assert world.repository._appointments == before
    assert world.client("NO").text.startswith("OK, I won't request that time.")


def test_booking_requests_are_not_read_as_follow_ups() -> None:
    chat, _, _ = week_with_visits()
    chat.text("Do I have bookings this week?")
    for text in ("Cleaning Friday 10 please", "Friday 10", "Cleaning next week please",
                 "Friday please", "Friday"):
        chat.model.replies[text] = ask("availability", "2026-10-02")
        assert "Reply with the number" in chat.text(text).text, text
        chat.text("Do I have bookings this week?")  # Reopen the calendar conversation.
    for text in ("Booking for Friday", "Appointment on Friday", "Cleaning Friday instead",
                 "Also cleaning Friday", "Cleaning Friday too", "What about cleaning Friday"):
        assert chat.text(text).text.startswith(
            "Do you want to check the visits you already have for Fri Oct 2"), text


def test_a_cancel_clarification_keeps_its_specific_question() -> None:
    chat, _, _ = week_with_visits()
    chat.text("Do I have bookings this week?")
    assert chat.text("Cancel my visit").text == "Which visit would you like to cancel? Tell me its day."


class FakeDynamo:
    def __init__(self) -> None:
        self.item: dict[str, Any] | None = None

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return {"Item": self.item} if self.item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("states are written in a transaction")

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self.item = None
        return {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.item = kwargs["TransactItems"][0]["Put"]["Item"]
        return {}


def test_calendar_memory_round_trips_through_dynamodb_without_visit_details() -> None:
    fake = FakeDynamo()
    states = DynamoConversationStates(fake, "table")
    state = ConversationState(
        "pilot", PHONE, "SM-1", PromptKind.CALENDAR, NOW, NOW + timedelta(minutes=30),
        client_id="client-1", calendar_first=FRIDAY.date(), calendar_last=FRIDAY.date(),
        calendar_statuses=("CONFIRMED",), calendar_view="count")
    states.put_state(state)
    assert fake.item is not None
    assert not {"options", "appointment_id", "appointment_version"} & set(fake.item)
    assert states.read_state("pilot", PHONE) == state
    upcoming = ConversationState(
        "pilot", PHONE, "SM-2", PromptKind.CALENDAR, NOW, NOW + timedelta(minutes=30),
        client_id="client-1", calendar_statuses=("CONFIRMED", "PENDING_APPROVAL"),
        calendar_view="list")
    states.put_state(upcoming)
    assert states.read_state("pilot", PHONE) == upcoming
