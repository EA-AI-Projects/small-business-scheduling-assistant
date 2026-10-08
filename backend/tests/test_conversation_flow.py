"""Plain-language texts write only when a reply maps to one current offer or prompt."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.conversation import (
    ConversationOutcome,
    ConversationService,
    MessageContext,
    MessageProposal,
)
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.conversation_state import (
    ConversationState,
    InMemoryConversationStates,
    PromptKind,
)
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyContext,
    OwnerReplyIntent,
    OwnerReplyProposal,
)
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)  # Tue Sep 29, 10:00 AM local.
TOMORROW = "2026-09-30"
THURSDAY = datetime(2026, 10, 1, 16, tzinfo=UTC)  # Thu Oct 1, 9:00 AM local.
CLARIFY = MessageProposal("clarify", None, None, None, True)


def ask(intent: str, date_from: str | None = None, date_to: str | None = None,
        time_from: str | None = None, time_to: str | None = None,
        target_date: str | None = None) -> MessageProposal:
    return MessageProposal(intent, None, None, None, False, date_from, date_to,
                           time_from, time_to, target_date)


class Script:
    """A fake model keyed by exact inbound text; unknown text asks for clarification."""

    def __init__(self) -> None:
        self.replies: dict[str, MessageProposal] = {}
        self.calls: list[str] = []
        self.contexts: list[MessageContext] = []
        self.owner: dict[str, OwnerReplyIntent] = {}  # Owner reply text to what the model says.
        self.classified: list[str] = []
        self.reads: list[tuple[str, str]] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        self.calls.append(body)
        self.contexts.append(context)
        return self.replies.get(body, CLARIFY)

    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal:
        self.classified.append(body)
        intent = self.owner.get(body, OwnerReplyIntent.UNCLEAR)
        reference = context.pending[0].ref if len(context.pending) == 1 else None
        return OwnerReplyProposal(intent, reference, Confidence.HIGH)

    def draft_read_reply(self, body: str, context: MessageContext,
                         tool_name: str, tool_result: str) -> str:
        self.reads.append((tool_name, tool_result))
        return tool_result


class Consent:
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return ConsentEvidence(business_id, "client-1", "Avery Example", phone_e164, NOW, "v1")


class Harness:
    def __init__(self) -> None:
        self.store = InMemoryCalendarRepository()
        self.store.save_profile(ClientProfile(
            "pilot", "client-1", "Avery Example", "+14155550101", "1 Test Street",
            HomeSize.MEDIUM, 120, True, 1, NOW, NOW, NOW), 0, None)
        self.model = Script()
        self.states = InMemoryConversationStates()
        self.now = NOW
        self.service = ConversationService(
            self.store, self.model, HoldService(self.store),
            LifecycleService(self.store, lambda: self.now), Consent(), lambda: self.now,
            "+14155559999", self.states)
        self.count = 0

    def text(self, body: str, role: SenderRole = SenderRole.CLIENT) -> ConversationOutcome:
        self.count += 1
        owner = role == SenderRole.OWNER
        return self.service.handle(InboundReceipt(
            "pilot", f"SM-{self.count}", "+14155559999" if owner else "+14155550101",
            "+14155550000", body, self.now, role, None if owner else "client-1",
            Keyword.OTHER, True))

    def hold(self, start: datetime, key: str, confirm: bool = False) -> str:
        hold_id = HoldService(self.store).create(CreateHold(
            "pilot", "client-1", "client-1", key, start, 120), self.now).hold_id
        if confirm:
            LifecycleService(self.store, lambda: self.now).apply(AppointmentCommand(
                "pilot", hold_id, "owner", ActorRole.OWNER, Action.APPROVE, f"ok-{key}", 1))
        return hold_id

    def calendar(self) -> tuple[tuple[str, str, int], ...]:
        appointments = (self.store.read_appointment(event.event_id)
                        for event in self.store.read_calendar("pilot").events)
        return tuple(sorted((item.appointment_id, item.status.value, item.version)
                            for item in appointments if item is not None))

    def status(self, appointment_id: str) -> CalendarStatus:
        appointment = self.store.read_appointment(appointment_id)
        assert appointment is not None
        return appointment.status


def test_plain_language_booking_then_owner_yes_confirms_it() -> None:
    chat = Harness()
    chat.model.replies["Hi. Do you have availability for tomorrow?"] = ask("availability", TOMORROW)
    offer = chat.text("Hi. Do you have availability for tomorrow?")
    assert not offer.committed
    assert offer.text.startswith("Open times on Wed Sep 30: 1) ")
    assert "10:00 AM" in offer.text and "30 minutes" in offer.text
    assert "owner approves" in offer.text
    assert len(offer.text) <= 500
    assert chat.calendar() == ()

    booked = chat.text("10 works")
    assert booked.committed
    assert booked.text.startswith("Requested Wed Sep 30 at 10:00 AM (ref ")
    assert "pending owner approval" in booked.text
    assert booked.appointment_id is not None
    assert chat.status(booked.appointment_id) == CalendarStatus.PENDING_APPROVAL
    assert chat.model.calls == ["Hi. Do you have availability for tomorrow?"]

    chat.model.owner["Yes"] = OwnerReplyIntent.APPROVE_NAMED_REQUEST
    approved = chat.text("Yes", SenderRole.OWNER)
    assert approved.committed
    assert approved.text == (f"Approved: Avery Example, Wed Sep 30 at 10:00 AM "
                             f"(ref {booked.appointment_id[:8]}).")
    assert chat.status(booked.appointment_id) == CalendarStatus.CONFIRMED
    assert len(chat.model.calls) == 1  # The owner's yes is read by the owner classifier only.
    assert chat.model.classified == ["Yes"]


def test_the_same_reply_cannot_book_twice_from_one_offer() -> None:
    chat = Harness()
    chat.model.replies["Anything tomorrow?"] = ask("availability", TOMORROW)
    chat.text("Anything tomorrow?")
    assert chat.text("the 10 o'clock one").committed
    again = chat.text("the 10 o'clock one")
    assert not again.committed
    assert len(chat.calendar()) == 1


def test_specific_open_time_is_a_single_option_confirmed_by_yes() -> None:
    chat = Harness()
    chat.model.replies["Can you do tomorrow at 1pm?"] = ask(
        "request_booking", TOMORROW, TOMORROW, "13:00", "13:00")
    offer = chat.text("Can you do tomorrow at 1pm?")
    assert offer.text.startswith("Wed Sep 30 at 1:00 PM is open for your 2-hour cleaning. "
                                 "Reply YES to request it.")
    assert chat.calendar() == ()
    booked = chat.text("Yes please")
    assert booked.committed
    assert "Wed Sep 30 at 1:00 PM" in booked.text


def test_closed_specific_time_offers_nearby_times_instead() -> None:
    chat = Harness()
    chat.hold(datetime(2026, 9, 30, 20, tzinfo=UTC), "busy")  # 1:00 PM local.
    chat.model.replies["Tomorrow at 1?"] = ask("availability", TOMORROW, TOMORROW,
                                               "13:00", "13:00")
    offer = chat.text("Tomorrow at 1?")
    assert offer.text.startswith("That exact time isn't open. Open times on Wed Sep 30: 1) ")
    assert "1:00 PM" not in offer.text


def test_expired_offer_asks_again_and_writes_nothing() -> None:
    chat = Harness()
    chat.model.replies["Tomorrow?"] = ask("availability", TOMORROW)
    chat.text("Tomorrow?")
    chat.now = NOW + timedelta(minutes=30)
    late = chat.text("10")
    assert not late.committed
    assert "expired after 30 minutes" in late.text
    assert chat.calendar() == ()
    assert chat.states.read_state("pilot", "+14155550101") is None


@pytest.mark.parametrize("reply", ["not 10", "no, 10 doesn't work", "10 or 12",
                                   "can I do 10?", "10 next week instead", "4:45"])
def test_negated_unmatched_or_ambiguous_replies_write_nothing(reply: str) -> None:
    chat = Harness()
    chat.model.replies["Tomorrow?"] = ask("availability", TOMORROW)
    chat.text("Tomorrow?")
    result = chat.text(reply)
    assert not result.committed
    assert chat.calendar() == ()


def test_time_offered_on_two_days_needs_the_option_number() -> None:
    chat = Harness()
    chat.model.replies["Wednesday or Thursday?"] = ask("availability", TOMORROW, "2026-10-01")
    offer = chat.text("Wednesday or Thursday?")
    assert "Wed Sep 30 at 8:00 AM" in offer.text and "Thu Oct 1 at 8:00 AM" in offer.text
    unclear = chat.text("8am")
    assert not unclear.committed
    assert "'option 1' to 'option 5'" in unclear.text
    booked = chat.text("Thursday at 8am")
    assert booked.committed
    assert booked.text.startswith("Requested Thu Oct 1 at 8:00 AM")


def test_bare_indices_select_remembered_booking_options_before_clock_hours() -> None:
    offered = options((30, 8, 0), (30, 10, 0), (30, 13, 0))
    for reply, expected in (("1", "8:00 AM"), ("2", "10:00 AM"),
                            ("3", "1:00 PM"), ("option 1", "8:00 AM"),
                            ("1 PM", "1:00 PM"), ("10 AM", "10:00 AM")):
        chat = Harness()
        chat.states.put_state(ConversationState(
            "pilot", "+14155550101", "offer-1", PromptKind.OFFER, NOW,
            NOW + timedelta(minutes=30), offered, client_id="client-1"))
        booked = chat.text(reply)
        assert booked.committed, reply
        assert booked.text.startswith(f"Requested Wed Sep 30 at {expected}"), reply
        assert "pending owner approval" in booked.text
        assert booked.appointment_id is not None
        assert chat.status(booked.appointment_id) == CalendarStatus.PENDING_APPROVAL


def test_bare_number_offer_keeps_rejection_and_availability_checks() -> None:
    offered = options((30, 8, 0), (30, 10, 0), (30, 13, 0))
    for reply in ("4", "not 1", "8 or 1", "9 AM"):
        chat = Harness()
        chat.states.put_state(ConversationState(
            "pilot", "+14155550101", "offer-1", PromptKind.OFFER, NOW,
            NOW + timedelta(minutes=30), offered, client_id="client-1"))
        result = chat.text(reply)
        assert not result.committed, reply
        assert chat.calendar() == ()

    chat = Harness()
    chat.states.put_state(ConversationState(
        "pilot", "+14155550101", "offer-1", PromptKind.OFFER, NOW,
        NOW + timedelta(minutes=30), offered, client_id="client-1"))
    chat.hold(offered[0], "new-conflict")
    result = chat.text("1")
    assert not result.committed
    assert "no longer open" in result.text


def test_new_request_replaces_the_previous_offer() -> None:
    chat = Harness()
    chat.model.replies["Tomorrow?"] = ask("availability", TOMORROW)
    chat.model.replies["Actually, what about Friday?"] = ask("availability", "2026-10-02")
    chat.text("Tomorrow?")
    chat.text("Actually, what about Friday?")
    booked = chat.text("option 1")
    assert booked.committed
    assert "Fri Oct 2" in booked.text


def test_relative_dates_are_checked_against_horizon_holidays_and_the_past() -> None:
    chat = Harness()
    chat.model.replies.update({
        "In three weeks?": ask("availability", "2026-10-20"),
        "Yesterday?": ask("availability", "2026-09-28"),
        "Columbus Day?": ask("availability", "2026-10-12"),
        "Saturday?": ask("availability", "2026-10-03"),
        "February 30?": ask("availability", "2026-02-30"),
    })
    assert "through Tue Oct 13" in chat.text("In three weeks?").text
    assert "has passed" in chat.text("Yesterday?").text
    assert "any openings on Mon Oct 12" in chat.text("Columbus Day?").text
    assert "any openings on Sat Oct 3" in chat.text("Saturday?").text
    assert "which day you meant" in chat.text("February 30?").text
    assert chat.states.read_state("pilot", "+14155550101") is None
    assert chat.calendar() == ()


def test_date_only_followup_uses_invitation_context_and_current_availability() -> None:
    chat = Harness()
    chat.now = datetime(2026, 10, 12, 17, tzinfo=UTC)
    chat.hold(datetime(2026, 10, 13, 20, tzinfo=UTC), "busy-1pm")
    chat.model.replies["Oct 13 at 1 pm"] = ask(
        "availability", "2026-10-13", "2026-10-13", "13:00", "13:00")
    chat.model.replies["Oct 13"] = ask("availability", "2026-10-13")

    class History:
        def read_conversation_history(self, receipt: InboundReceipt,
                                      now: datetime) -> tuple[HistoryMessage, ...]:
            return (
                HistoryMessage("invite", "assistant", now - timedelta(minutes=20),
                               "Would you like a cleaning this week?", True),
                HistoryMessage("first", "client", now - timedelta(minutes=15),
                               "Oct 13 at 1 pm"),
                HistoryMessage("answer", "assistant", now - timedelta(minutes=14),
                               "That exact time isn't open. Here are other times."),
                HistoryMessage(receipt.provider_id, "client", now, receipt.body or ""),
            )

    chat.service = ConversationService(
        chat.store, chat.model, HoldService(chat.store),
        LifecycleService(chat.store, lambda: chat.now), Consent(), lambda: chat.now,
        "+14155559999", chat.states, history_reader=History())
    first = chat.text("Oct 13 at 1 pm")
    assert "That exact time isn't open" in first.text
    followup = chat.text("Oct 13")
    assert "Open times on Tue Oct 13" in followup.text
    assert "1:00 PM" not in followup.text
    assert chat.model.contexts[-1].history[0].invitation
    assert chat.model.contexts[-1].prompt_kind == "offer"
    assert [name for name, _result in chat.model.reads] == [
        "list_available_slots", "list_available_slots"]
    assert len(chat.calendar()) == 1  # The preexisting conflict is unchanged.


def test_week_long_range_spreads_five_options_across_days() -> None:
    chat = Harness()
    chat.model.replies["This week?"] = ask("availability", "2026-09-29", "2026-10-04")
    offer = chat.text("This week?")
    state = chat.states.read_state("pilot", "+14155550101")
    assert state is not None and state.kind == PromptKind.OFFER
    assert len(state.options) == 5
    assert len({option.astimezone(ZONE).date() for option in state.options}) == 4
    assert len(offer.text) <= 500


def test_cant_make_thursday_asks_before_cancelling() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I can't make Thursday"] = ask("cancel", target_date="2026-10-01")
    prompt = chat.text("I can't make Thursday")
    assert prompt.text == "Cancel your Thu Oct 1 at 9:00 AM visit? Reply YES to confirm."
    assert not prompt.committed
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    done = chat.text("Yes, cancel it")
    assert done.committed
    assert done.text == f"Cancelled your Thu Oct 1 at 9:00 AM visit (ref {visit[:8]})."
    assert chat.status(visit) == CalendarStatus.CANCELLED


def test_no_keeps_the_visit_and_expired_confirmation_cancels_nothing() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["Cancel Thursday"] = ask("cancel", target_date="2026-10-01")
    chat.text("Cancel Thursday")
    assert "I kept your Thu Oct 1 at 9:00 AM visit" in chat.text("No").text
    assert not chat.text("yes").committed  # The prompt is gone.
    chat.text("Cancel Thursday")
    chat.now = NOW + timedelta(minutes=31)
    late = chat.text("yes")
    assert not late.committed and "expired" in late.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_cancel_confirmation_refuses_a_visit_changed_since_the_prompt() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["Cancel my visit"] = ask("cancel")
    chat.text("Cancel my visit")
    LifecycleService(chat.store, lambda: NOW).apply(AppointmentCommand(
        "pilot", visit, "owner", ActorRole.OWNER, Action.EDIT, "owner-edit", 2,
        duration_minutes=90))
    result = chat.text("yes")
    assert not result.committed
    assert "changed since I asked" in result.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_cancel_with_several_visits_lists_them_and_still_asks_yes() -> None:
    chat = Harness()
    thursday = chat.hold(THURSDAY, "one", confirm=True)
    friday = chat.hold(THURSDAY + timedelta(days=1), "two", confirm=True)
    chat.model.replies["Cancel my appointment"] = ask("cancel")
    chat.model.replies["Cancel Monday"] = ask("cancel", target_date="2026-10-05")
    before = chat.calendar()
    which = chat.text("Cancel my appointment")
    assert which.text == ("Which visit would you like to cancel? 1) Thu Oct 1 at 9:00 AM, "
                          "2) Fri Oct 2 at 9:00 AM. Reply with the number.")
    assert not chat.text("yes").committed  # "Yes" does not pick one of two.
    assert chat.text("2").text == "Cancel your Fri Oct 2 at 9:00 AM visit? Reply YES to confirm."
    assert chat.calendar() == before
    assert chat.text("yes").committed
    assert chat.status(friday) == CalendarStatus.CANCELLED
    assert chat.status(thursday) == CalendarStatus.CONFIRMED
    assert "don't see a visit on Mon Oct 5" in chat.text("Cancel Monday").text


def test_two_visits_on_one_day_can_be_told_apart_by_time_for_a_move() -> None:
    chat = Harness()
    chat.hold(THURSDAY, "early", confirm=True)
    late = chat.hold(THURSDAY + timedelta(hours=4), "late", confirm=True)  # 1:00 PM.
    chat.model.replies["I need to move my Thursday visit"] = ask(
        "reschedule", target_date="2026-10-01")
    which = chat.text("I need to move my Thursday visit")
    assert "1) Thu Oct 1 at 9:00 AM, 2) Thu Oct 1 at 1:00 PM" in which.text
    question = chat.text("the 1pm one")
    assert question.text.startswith("What day would you like instead of your Thu Oct 1 at 1:00 PM")
    state = chat.states.read_state("pilot", "+14155550101")
    assert state is not None and state.appointment_id == late


def test_reschedule_offers_replacements_and_keeps_the_original_confirmed() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["Can I move Thursday to Friday morning?"] = ask(
        "reschedule", "2026-10-02", "2026-10-02", "08:00", "12:00", "2026-10-01")
    offer = chat.text("Can I move Thursday to Friday morning?")
    assert offer.text.startswith("To move your Thu Oct 1 at 9:00 AM visit: Open times on Fri Oct 2")
    assert "stays booked" in offer.text
    assert not offer.committed
    moved = chat.text("1")
    assert moved.committed
    assert moved.text.startswith("Requested a move to Fri Oct 2 at 8:00 AM")
    assert "Thu Oct 1 at 9:00 AM visit" in moved.text and "remains confirmed" in moved.text
    assert moved.appointment_id is not None
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    replacement = chat.store.read_appointment(moved.appointment_id)
    assert replacement is not None and replacement.replaces_appointment_id == visit


def test_bare_number_selects_remembered_reschedule_option() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    original = chat.store.read_appointment(visit)
    assert original is not None
    chat.states.put_state(ConversationState(
        "pilot", "+14155550101", "move-offer", PromptKind.OFFER, NOW,
        NOW + timedelta(minutes=30),
        (datetime(2026, 10, 2, 8, tzinfo=ZONE).astimezone(UTC),
         datetime(2026, 10, 2, 10, tzinfo=ZONE).astimezone(UTC),
         datetime(2026, 10, 2, 13, tzinfo=ZONE).astimezone(UTC)),
        visit, original.version, "client-1"))
    moved = chat.text("1")
    assert moved.committed and moved.appointment_id is not None
    assert moved.text.startswith("Requested a move to Fri Oct 2 at 8:00 AM")
    assert "pending owner approval" in moved.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    assert chat.status(moved.appointment_id) == CalendarStatus.PENDING_APPROVAL
    replacement = chat.store.read_appointment(moved.appointment_id)
    assert replacement is not None and replacement.replaces_appointment_id == visit


def test_reschedule_without_a_day_asks_and_carries_the_visit_into_the_answer() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I need to reschedule"] = ask("reschedule")
    chat.model.replies["Friday"] = ask("availability", "2026-10-02")
    question = chat.text("I need to reschedule")
    assert question.text.startswith("What day would you like instead of your Thu Oct 1 at 9:00 AM")
    offer = chat.text("Friday")
    assert offer.text.startswith("To move your Thu Oct 1 at 9:00 AM visit:")
    moved = chat.text("8am")
    assert moved.committed and moved.appointment_id is not None
    replacement = chat.store.read_appointment(moved.appointment_id)
    assert replacement is not None and replacement.replaces_appointment_id == visit


def test_replacement_offer_is_refused_when_the_original_changed() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["Move Thursday to Friday"] = ask(
        "reschedule", "2026-10-02", target_date="2026-10-01")
    chat.text("Move Thursday to Friday")
    LifecycleService(chat.store, lambda: NOW).apply(AppointmentCommand(
        "pilot", visit, "owner", ActorRole.OWNER, Action.CANCEL, "owner-cancel", 2))
    result = chat.text("option 1")
    assert not result.committed
    assert "has changed" in result.text
    assert chat.calendar() == ()  # The cancelled original is off the calendar; no new hold.


def test_owner_plain_decline_and_yes_without_pending_requests() -> None:
    chat = Harness()
    assert "No request is waiting" in chat.text("yes", SenderRole.OWNER).text
    request = chat.hold(THURSDAY, "request")
    chat.model.owner["Decline"] = OwnerReplyIntent.DECLINE_NAMED_REQUEST
    declined = chat.text("Decline", SenderRole.OWNER)
    assert declined.committed and declined.text.startswith("Declined: Avery Example, Thu Oct 1")
    assert chat.status(request) == CalendarStatus.DECLINED


def test_owner_negated_or_qualified_yes_goes_to_the_model_and_writes_nothing() -> None:
    chat = Harness()
    request = chat.hold(THURSDAY, "request")
    chat.model.replies["Yes but not yet"] = MessageProposal(
        "owner_decision", None, None, "approve", False)
    for body in ("Yes but not yet", "Don't approve", "yes?", "no"):
        result = chat.text(body, SenderRole.OWNER)
        assert not result.committed, body
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_model_cannot_select_an_offer_or_confirm_a_cancellation() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    unsafe = [
        MessageProposal("request_booking", visit[:8], "2026-09-30 10:00", "approve", False,
                        TOMORROW, TOMORROW, "10:00", "10:00", "2026-10-01"),
        MessageProposal("cancel", visit[:8], None, None, False, target_date="2026-10-01"),
        MessageProposal("reschedule", visit[:8], "2026-10-02 09:00", None, False,
                        "2026-10-02", "2026-10-02", "09:00", "09:00", "2026-10-01"),
        MessageProposal("owner_decision", visit[:8], None, "decline", False),
    ]
    before = chat.calendar()
    for number, proposal in enumerate(unsafe):
        body = f"Whatever you think is best {number}"
        chat.model.replies[body] = proposal
        assert not chat.text(body).committed
    assert chat.calendar() == before


def test_owner_has_no_offer_memory() -> None:
    chat = Harness()
    chat.model.replies["Tomorrow?"] = ask("availability", TOMORROW)
    chat.text("Tomorrow?")
    result = chat.text("1", SenderRole.OWNER)
    assert not result.committed
    assert chat.calendar() == ()


def options(*local: tuple[int, int, int]) -> tuple[datetime, ...]:
    return tuple(datetime(2026, 9, day, hour, minute, tzinfo=ZONE).astimezone(UTC)
                 for day, hour, minute in local)
