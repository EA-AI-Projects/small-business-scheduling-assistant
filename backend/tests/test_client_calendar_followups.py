"""Follow-ups to a client's calendar question reread the calendar and never write (#241).

Boundary: the conversation service with the in-memory calendar, conversation memory, and a
scripted model standing in for the model's reading of each follow-up, plus the DynamoDB state
adapter against a fake client. The real model's reading is covered by the synthetic model
evaluation, not here.
"""

from datetime import timedelta
from typing import Any

from test_client_calendar_questions import (
    FRIDAY,
    NEXT_TUESDAY,
    THIS_WEEK,
    asked,
    cal,
    week_with_visits,
)
from test_conversation_flow import CLARIFY, NOW, Harness, Script, ask
from test_counteroffer_acceptance import World, world  # noqa: F401 - pytest fixture

from scheduling.adapters.conversation_state_dynamodb import DynamoConversationStates
from scheduling.domain.conversation_state import ConversationState, PromptKind
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService

PHONE = "+14155550101"
NEXT_WEEK = ("2026-10-05", "2026-10-11")


def approve(chat: Harness, appointment_id: str) -> None:
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", appointment_id, "owner", ActorRole.OWNER, Action.APPROVE,
        f"ok-{appointment_id}", 1))


def test_the_reported_three_message_sequence_is_answered_from_the_calendar() -> None:
    """The texts from the #241 screenshot. The model reads each one; a follow-up leaves
    unchanged fields null, so the backend keeps the conversation's range."""
    chat, confirmed, pending = week_with_visits()
    later = chat.hold(NEXT_TUESDAY, "tue", confirm=True)
    before = chat.calendar()
    week = asked(chat, "do I have any bookings for this week?", cal(*THIS_WEEK))
    assert week.text.startswith("You have 1 confirmed visit, 1 pending request from Tue Sep 29 "
                                "to Sun Oct 4:")
    coming = asked(chat, "Do I have any confirmed bookings already?  I'm trying to find out "
                         "when the cleaners are coming", cal(statuses=("confirmed",)))
    # No range of its own, so it stays on this week, as the model was told it was shown.
    assert chat.model.contexts[-1].calendar_answer == (
        "2026-09-28 to 2026-10-04; statuses: confirmed, pending; view: list")
    assert coming.text == (
        "You have 1 confirmed visit from Tue Sep 29 to Sun Oct 4:\n"
        f"- Thu Oct 1 at 9:00 AM-11:00 AM PDT, confirmed (ref {confirmed[:8]})\n"
        "You also have 1 pending request from Tue Sep 29 to Sun Oct 4.\n"
        f"- Fri Oct 2 at 9:00 AM-11:00 AM PDT, pending owner approval, not confirmed yet "
        f"(ref {pending[:8]})")
    summary = asked(chat, "I want a summary of my itinerary",
                    cal(statuses=("confirmed", "pending"), view="list"))
    assert summary.text.startswith("You have 1 confirmed visit, 1 pending request from Tue "
                                   "Sep 29 to Sun Oct 4:")
    assert later[:8] not in summary.text
    assert chat.calendar() == before


def test_follow_ups_keep_what_they_do_not_change() -> None:
    chat, confirmed, _ = week_with_visits()
    later = chat.hold(NEXT_TUESDAY, "tue", confirm=True)
    asked(chat, "Do I have bookings this week?", cal(*THIS_WEEK))
    only = asked(chat, "Just confirmed ones", cal(statuses=("confirmed",)))
    assert only.text.startswith("You have 1 confirmed visit from Tue Sep 29 to Sun Oct 4:")
    assert confirmed[:8] in only.text
    following = asked(chat, "¿Y la próxima semana?", cal(*NEXT_WEEK))  # Keeps "confirmed".
    assert following.text == (
        "You have 1 confirmed visit from Mon Oct 5 to Sun Oct 11:\n"
        f"- Tue Oct 6 at 9:00 AM-11:00 AM PDT, confirmed (ref {later[:8]})")
    count = asked(chat, "How many?", cal(view="count"))
    assert count.text == "You have 1 confirmed visit from Mon Oct 5 to Sun Oct 11."
    assert chat.model.contexts[-1].calendar_answer == (
        "2026-10-05 to 2026-10-11; statuses: confirmed; view: list")


def test_a_follow_up_rereads_the_calendar_after_a_change() -> None:
    chat, _, pending = week_with_visits()
    asked(chat, "Do I have bookings this week?", cal(*THIS_WEEK))
    approve(chat, pending)
    reply = asked(chat, "Just confirmed ones", cal(statuses=("confirmed",)))
    assert reply.text.startswith("You have 2 confirmed visits from Tue Sep 29 to Sun Oct 4:")
    assert "pending" not in reply.text


def test_a_bare_yes_or_number_after_a_calendar_answer_changes_nothing() -> None:
    chat, _, _ = week_with_visits()
    before = chat.calendar()
    asked(chat, "Do I have bookings this week?", cal(*THIS_WEEK))
    for reply in ("yes", "2", "the first one", "no"):
        outcome = chat.text(reply)
        assert outcome.text.startswith("I only listed your visits, so nothing was booked or "
                                       "cancelled."), reply
        assert not outcome.committed
    assert chat.calendar() == before
    assert chat.model.calls == ["Do I have bookings this week?"]  # Write guard, no model.
    # The calendar conversation is still open for a real follow-up.
    assert asked(chat, "How many?", cal(view="count")).text.startswith(
        "You have 1 confirmed visit, 1 pending")


def test_a_question_during_an_open_offer_closes_it_and_a_number_then_books_nothing() -> None:
    chat, _, _ = week_with_visits()
    asked(chat, "Anything Wednesday?", ask("availability", "2026-09-30"))
    before = chat.calendar()
    answer = asked(chat, "Do I have bookings this week?", cal(*THIS_WEEK))
    assert "I closed my earlier question" in answer.text
    assert chat.text("2").text.startswith("I only listed your visits")
    assert chat.calendar() == before


def test_a_new_request_replaces_the_calendar_conversation() -> None:
    chat, _, _ = week_with_visits()
    asked(chat, "Do I have bookings this week?", cal(*THIS_WEEK))
    offer = asked(chat, "Cleaning Friday instead", ask("availability", "2026-10-02"))
    assert "Reply with the number" in offer.text
    state = chat.states.read_state("pilot", PHONE)
    assert state is not None and state.kind == PromptKind.OFFER
    assert chat.text("1").committed  # The offer, not the calendar answer, is what is open.


def test_unclear_reply_in_a_calendar_conversation_asks_one_question() -> None:
    chat, _, _ = week_with_visits()
    asked(chat, "Do I have bookings this week?", cal(*THIS_WEEK))
    assert asked(chat, "hmm ok whatever", CLARIFY).text == (
        "I wasn't sure what you meant. Should I check another day or status, or do you want "
        "to request or change a visit?")
    assert asked(chat, "Just pending", cal(statuses=("pending",))).text.startswith(
        "You have 1 pending request from Tue Sep 29 to Sun Oct 4:")


def test_follow_ups_end_after_thirty_minutes() -> None:
    chat, _, _ = week_with_visits()
    asked(chat, "Do I have bookings this week?", cal(*THIS_WEEK))
    chat.now = NOW + timedelta(minutes=30)
    asked(chat, "Just confirmed ones", cal(statuses=("confirmed",)))
    assert chat.model.contexts[-1].calendar_answer is None
    assert chat.text("yes").text != ("I only listed your visits, so nothing was booked or "
                                     "cancelled.")


def scripted(world: World) -> Script:  # noqa: F811 - pytest fixture
    model = Script()
    world.service._interpreter = model
    return model


def test_calendar_answer_reminds_of_an_open_counteroffer_and_yes_still_accepts_it(
        world: World) -> None:  # noqa: F811 - pytest fixture
    model = scripted(world)
    world.offered()
    model.replies["Do I have bookings this week?"] = cal(*THIS_WEEK)
    answer = world.client("Do I have bookings this week?")
    assert answer.text.endswith(
        "The owner's offer of Thu Oct 1 at 2:00 PM is still open: reply YES to request it, "
        "or NO to turn it down.")
    model.replies["Just pending"] = cal(statuses=("pending",))
    assert world.client("Just pending").text.endswith(
        "reply YES to request it, or NO to turn it down.")
    assert world.client("YES").committed


def test_inside_a_calendar_conversation_only_plain_yes_or_no_answers_a_counteroffer(
        world: World) -> None:  # noqa: F811 - pytest fixture
    model = scripted(world)
    world.offered()
    before = dict(world.repository._appointments)
    model.replies["Do I have bookings this week?"] = cal(*THIS_WEEK)
    model.replies["Just the confirmed one"] = cal(statuses=("confirmed",))
    world.client("Do I have bookings this week?")
    for reply in ("Just the confirmed one", "the first one", "1", "confirmed"):
        outcome = world.client(reply)
        assert not outcome.committed, reply
    assert world.repository._appointments == before
    assert world.client("NO").text.startswith("OK, I won't request that time.")


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
