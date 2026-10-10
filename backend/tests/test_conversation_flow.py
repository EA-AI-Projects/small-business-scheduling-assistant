"""Plain-language texts write only when a reply maps to one current offer or prompt."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.client_replies import ClientReplyResult
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
        self.owner: dict[str, str] = {}
        self.classified: list[str] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        self.calls.append(body)
        self.contexts.append(context)
        return self.replies.get(body, CLARIFY)

    def run_owner_loop(self, body: str, today: date, timezone: str, history: Any,
                       tool: Any) -> str:
        self.classified.append(body)
        requests = tool("list_pending_requests", {})["requests"]
        if not requests:
            return "No request is waiting for approval right now."
        action = self.owner.get(body)
        if action is None or len(requests) != 1:
            return "Nothing changed. Which request do you mean?"
        result = tool(action, {"ref": requests[0]["ref"], "version": requests[0]["version"]})
        return (f"{'Approved' if action == 'approve_request' else 'Declined'}: "
                f"Avery Example, {requests[0]['time']} (ref {requests[0]['ref']}).") if result["ok"] else "Nothing changed."

class DraftScript(Script):
    def __init__(self) -> None:
        super().__init__()
        self.drafts: dict[str, str | Exception | Callable[[ClientReplyResult], str]] = {}
        self.draft_calls: list[tuple[str, ClientReplyResult]] = []

    def draft_client_reply(self, body: str, context: MessageContext,
                           result: ClientReplyResult) -> str:
        self.draft_calls.append((body, result))
        draft = self.drafts.get(body, result.fallback)
        if isinstance(draft, Exception):
            raise draft
        if callable(draft):
            return draft(result)
        return draft


class Consent:
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return ConsentEvidence(business_id, "client-1", "Avery Example", phone_e164, NOW, "v1")


class Harness:
    def __init__(self, model: Script | None = None) -> None:
        self.store = InMemoryCalendarRepository()
        self.store.save_profile(ClientProfile(
            "pilot", "client-1", "Avery Example", "+14155550101", "1 Test Street",
            HomeSize.MEDIUM, 120, True, 1, NOW, NOW, NOW), 0, None)
        self.model = model if model is not None else Script()
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

    chat.model.owner["Yes"] = "approve_request"
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
    chat.model.owner["Decline"] = "decline_request"
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


def invited(chat: Harness) -> None:
    class History:
        def read_conversation_history(self, receipt: InboundReceipt,
                                      now: datetime) -> tuple[HistoryMessage, ...]:
            return (HistoryMessage("invite", "assistant", now - timedelta(minutes=20),
                                   "Would you like a cleaning this week?", True),
                    HistoryMessage(receipt.provider_id, "client", now, receipt.body or ""))

    chat.now = datetime(2026, 10, 12, 17, tzinfo=UTC)
    chat.service = ConversationService(
        chat.store, chat.model, HoldService(chat.store),
        LifecycleService(chat.store, lambda: chat.now), Consent(), lambda: chat.now,
        "+14155559999", chat.states, history_reader=History())
    for model_intent in ("availability", "request_booking"):
        chat.model.replies[f"Oct 13 at 1 pm ({model_intent})"] = ask(
            model_intent, "2026-10-13", "2026-10-13", "13:00", "13:00")


@pytest.mark.parametrize("intent", ["availability", "request_booking"])
def test_exact_time_after_invitation_is_offered_and_only_yes_requests_it(intent: str) -> None:
    chat = Harness()
    invited(chat)
    offer = chat.text(f"Oct 13 at 1 pm ({intent})")
    assert not offer.committed and "is open for your 2-hour cleaning. Reply YES" in offer.text
    assert chat.calendar() == ()  # Offering writes nothing.
    booked = chat.text("YES")
    assert booked.committed and booked.appointment_id is not None
    assert "pending owner approval" in booked.text and "not confirmed" in booked.text
    assert chat.status(booked.appointment_id) == CalendarStatus.PENDING_APPROVAL


def test_a_second_exact_time_is_a_second_offer_not_a_second_request() -> None:
    chat = Harness()
    invited(chat)
    chat.text("Oct 13 at 1 pm (availability)")
    chat.text("YES")
    chat.model.replies["Oct 20 at 2 pm"] = ask(
        "request_booking", "2026-10-20", "2026-10-20", "14:00", "14:00")
    again = chat.text("Oct 20 at 2 pm")
    assert not again.committed and "Reply YES" in again.text
    assert len(chat.calendar()) == 1


def test_retried_yes_does_not_create_a_second_hold() -> None:
    chat = Harness()
    invited(chat)
    chat.text("Oct 13 at 1 pm (availability)")
    chat.text("YES")
    chat.text("YES")
    assert len(chat.calendar()) == 1


def test_conflicting_exact_time_offers_alternatives_and_creates_nothing() -> None:
    chat = Harness()
    invited(chat)
    chat.hold(datetime(2026, 10, 13, 20, tzinfo=UTC), "busy-1pm")
    reply = chat.text("Oct 13 at 1 pm (availability)")
    assert not reply.committed and "That exact time isn't open" in reply.text
    assert len(chat.calendar()) == 1


def test_unavailable_exact_time_then_date_only_gets_openings_not_what_day() -> None:
    chat = Harness()
    invited(chat)
    chat.hold(datetime(2026, 10, 13, 20, tzinfo=UTC), "busy-1pm")
    chat.model.replies["Oct 13"] = ask("availability", "2026-10-13")
    chat.text("Oct 13 at 1 pm (availability)")
    followup = chat.text("Oct 13")
    assert "Open times on Tue Oct 13" in followup.text and "What day" not in followup.text
    assert len(chat.calendar()) == 1


def test_invitation_sequence_drafts_each_reply_and_falls_back_on_a_bad_draft() -> None:
    model = DraftScript()
    chat = Harness(model)
    invited(chat)
    chat.hold(datetime(2026, 10, 13, 20, tzinfo=UTC), "busy-1pm")
    model.replies["Oct 13 at 1 pm"] = ask(
        "availability", "2026-10-13", "2026-10-13", "13:00", "13:00")
    model.replies["Oct 13"] = ask("availability", "2026-10-13")
    first = chat.text("Oct 13 at 1 pm")
    assert "What day" not in first.text
    followup = chat.text("Oct 13")
    assert [result.kind for _body, result in model.draft_calls] == ["offer_made", "offer_made"]
    assert followup.text == model.draft_calls[-1][1].fallback
    model.drafts["Oct 13"] = "Tue Oct 13 at 4:00 AM is open. Reply YES."  # Not an offered time.
    rejected = chat.text("Oct 13")
    assert rejected.text == model.draft_calls[-1][1].fallback and "4:00 AM" not in rejected.text
    assert len(chat.calendar()) == 1  # Only the preexisting conflict; nothing was booked.


@pytest.mark.parametrize("day", ["2026-10-17", "2026-11-11", "2027-03-01"])
def test_weekend_holiday_or_beyond_horizon_exact_time_creates_nothing(day: str) -> None:
    chat = Harness()
    invited(chat)
    chat.model.replies["x"] = ask("request_booking", day, day, "13:00", "13:00")
    reply = chat.text("x")
    assert not reply.committed and "Reply YES to request it" not in reply.text
    assert chat.calendar() == ()


ACCEPT = "lovely, lets lock that in"  # No keyword the deterministic matcher knows.


def offered(chat: Harness) -> None:
    invited(chat)
    chat.text("Oct 13 at 1 pm (availability)")
    chat.model.replies[ACCEPT] = ask(
        "request_booking", "2026-10-13", "2026-10-13", "13:00", "13:00")


def test_model_reads_a_free_form_acceptance_and_requests_the_offered_time() -> None:
    chat = Harness()
    offered(chat)
    calls = len(chat.model.calls)
    booked = chat.text(ACCEPT)
    assert len(chat.model.calls) == calls + 1  # The model, not the keyword matcher, decided.
    assert booked.committed and booked.appointment_id is not None
    assert "pending owner approval" in booked.text
    assert chat.status(booked.appointment_id) == CalendarStatus.PENDING_APPROVAL
    assert len(chat.calendar()) == 1


def test_client_draft_uses_validated_offer_and_pending_request() -> None:
    model = DraftScript()
    chat = Harness(model)
    invited(chat)
    model.drafts["Oct 13 at 1 pm (availability)"] = (
        "Tue Oct 13 at 1:00 PM is open. Reply YES to request it.")
    offer = chat.text("Oct 13 at 1 pm (availability)")
    assert offer.text == model.drafts["Oct 13 at 1 pm (availability)"]
    assert model.draft_calls[-1][1].kind == "offer_made"
    model.replies[ACCEPT] = ask("request_booking", "2026-10-13", "2026-10-13", "13:00", "13:00")
    model.drafts[ACCEPT] = lambda result: (
        f"Tue Oct 13 at 1:00 PM, ref {result.references[0]}, is pending owner approval.")
    booked = chat.text(ACCEPT)
    assert booked.committed and booked.client_outbox_id == f"{booked.appointment_id}#client"
    assert model.draft_calls[-1][1].status == "pending"
    assert booked.appointment_id is not None and booked.appointment_id[:8] in booked.text
    assert booked.text != model.draft_calls[-1][1].fallback


def test_hostile_or_failed_draft_keeps_safe_result_without_another_write() -> None:
    model = DraftScript()
    chat = Harness(model)
    offered(chat)
    model.drafts[ACCEPT] = "Confirmed for Tue Oct 13 at 2:00 PM."
    result = chat.text(ACCEPT)
    assert result.committed and "pending owner approval" in result.text
    assert len(chat.calendar()) == 1
    assert model.draft_calls[-1][1].references == (result.appointment_id[:8],)
    model.drafts["another day"] = TimeoutError("synthetic timeout")
    model.replies["another day"] = ask("availability", "2026-10-14")
    fallback = chat.text("another day")
    assert not fallback.committed and fallback.text == model.draft_calls[-1][1].fallback
    assert len(chat.calendar()) == 1


@pytest.mark.parametrize("draft", [
    "Confirmed for Tue Oct 13 at 1:00 PM.",
    "Tue Oct 13 at 2:00 PM is pending owner approval.",
    "Your request is pending owner approval.",
    "Your request is pending owner approval. " + "x" * 160,
])
def test_unsafe_pending_draft_falls_back_to_the_committed_result(draft: str) -> None:
    model = DraftScript()
    chat = Harness(model)
    offered(chat)
    model.drafts[ACCEPT] = draft
    booked = chat.text(ACCEPT)
    assert booked.committed and booked.appointment_id is not None
    assert booked.text == model.draft_calls[-1][1].fallback
    assert len(chat.calendar()) == 1


def test_taken_slot_draft_reports_no_booking_and_keeps_the_calendar() -> None:
    model = DraftScript()
    chat = Harness(model)
    offered(chat)
    chat.hold(datetime(2026, 10, 13, 13, tzinfo=ZONE).astimezone(UTC), "taken")
    before = chat.calendar()
    model.drafts[ACCEPT] = "Tue Oct 13 at 1:00 PM is unavailable, so nothing was booked."
    failed = chat.text(ACCEPT)
    assert not failed.committed and failed.text == model.drafts[ACCEPT]
    assert model.draft_calls[-1][1].kind == "request_failed"
    assert model.draft_calls[-1][1].reason == "slot_taken"
    assert chat.calendar() == before


def test_expired_offer_draft_has_no_write_and_reports_expiry() -> None:
    model = DraftScript()
    chat = Harness(model)
    offered(chat)
    chat.now += timedelta(minutes=31)
    model.drafts["YES"] = "That offer expired; nothing was booked. What day works instead?"
    late = chat.text("YES")
    assert not late.committed and late.text == model.drafts["YES"]
    assert model.draft_calls[-1][1].kind == "expired"
    assert chat.calendar() == ()


def test_model_drafts_client_cancel_and_move_questions_without_changing_visits() -> None:
    model = DraftScript()
    chat = Harness(model)
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    model.replies["Cancel my visit"] = ask("cancel")
    model.drafts["Cancel my visit"] = lambda result: (
        f"Cancel Thu Oct 1 at 9:00 AM, ref {result.references[0]}? Reply YES to confirm.")
    asked = chat.text("Cancel my visit")
    assert not asked.committed and asked.text == model.drafts["Cancel my visit"](
        model.draft_calls[-1][1])
    assert model.draft_calls[-1][1].kind == "cancel_question"
    assert chat.status(visit) == CalendarStatus.CONFIRMED

    model.replies["Move my visit"] = ask("reschedule")
    model.drafts["Move my visit"] = lambda result: (
        f"What day works instead of Thu Oct 1 at 9:00 AM, ref {result.references[0]}? ")
    move = chat.text("Move my visit")
    assert not move.committed and move.text == model.drafts["Move my visit"](
        model.draft_calls[-1][1])
    assert model.draft_calls[-1][1].kind == "reschedule_day_question"
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_model_drafts_unresolved_visit_and_date_questions_with_safe_fallback() -> None:
    model = DraftScript()
    chat = Harness(model)
    first = chat.hold(THURSDAY, "first", confirm=True)
    second = chat.hold(datetime(2026, 10, 2, 16, tzinfo=UTC), "second", confirm=True)
    model.replies["Cancel one"] = ask("cancel")
    model.drafts["Cancel one"] = (
        "Which visit: Thu Oct 1 at 9:00 AM or Fri Oct 2 at 9:00 AM? Reply 1 or 2.")
    choice = chat.text("Cancel one")
    assert choice.text == model.drafts["Cancel one"]
    assert model.draft_calls[-1][1].kind == "cancel_clarification"
    assert chat.status(first) == chat.status(second) == CalendarStatus.CONFIRMED

    model.replies["Book a day"] = ask("availability")
    model.drafts["Book a day"] = "What day works for you?"
    day = chat.text("Book a day")
    assert day.text == model.drafts["Book a day"]
    assert model.draft_calls[-1][1].reason == "missing_day"
    model.replies["Book far ahead"] = ask("availability", "2027-03-01")
    model.drafts["Book far ahead"] = lambda result: (
        f"I can book through {result.facts[0].date}. Which day works for you?")
    horizon = chat.text("Book far ahead")
    assert horizon.text == model.drafts["Book far ahead"](model.draft_calls[-1][1])
    assert model.draft_calls[-1][1].reason == "outside_horizon"
    model.replies["Far wrong"] = ask("availability", "2027-03-01")
    model.drafts["Far wrong"] = "I can book through Fri Oct 30. Which day works?"
    wrong_day = chat.text("Far wrong")
    assert wrong_day.text == model.draft_calls[-1][1].fallback
    assert model.draft_calls[-1][1].reason == "outside_horizon"
    model.replies["Saturday"] = ask("availability", "2026-10-10")
    model.drafts["Saturday"] = lambda result: (
        f"No openings on {result.facts[0].date}. Would another day work?")
    no_openings = chat.text("Saturday")
    assert no_openings.text == model.drafts["Saturday"](model.draft_calls[-1][1])
    assert model.draft_calls[-1][1].reason == "no_openings"
    assert chat.status(first) == chat.status(second) == CalendarStatus.CONFIRMED


def test_model_cannot_request_a_time_that_was_not_offered() -> None:
    chat = Harness()
    offered(chat)
    chat.model.replies[ACCEPT] = ask(
        "request_booking", "2026-10-13", "2026-10-13", "15:00", "15:00")
    reply = chat.text(ACCEPT)
    assert not reply.committed and chat.calendar() == ()


def test_request_booking_without_an_open_offer_only_offers_times() -> None:
    chat = Harness()
    invited(chat)
    chat.model.replies[ACCEPT] = ask(
        "request_booking", "2026-10-13", "2026-10-13", "13:00", "13:00")
    reply = chat.text(ACCEPT)
    assert not reply.committed and "Reply YES" in reply.text
    assert chat.calendar() == ()


def test_request_booking_after_the_offer_expires_writes_nothing() -> None:
    chat = Harness()
    offered(chat)
    chat.now += timedelta(minutes=31)
    calls = len(chat.model.calls)
    assert not chat.text(ACCEPT).committed
    assert len(chat.model.calls) == calls + 1
    assert chat.calendar() == ()


def test_same_receipt_replayed_through_the_model_creates_one_hold() -> None:
    chat = Harness()
    offered(chat)
    first = chat.text(ACCEPT)
    chat.count -= 1  # The same inbound SM id is delivered again.
    again = chat.text(ACCEPT)
    assert first.committed and not again.committed  # The offer was consumed; no second write.
    assert len(chat.calendar()) == 1


def test_offer_taken_before_a_free_form_acceptance_writes_nothing() -> None:
    chat = Harness()
    offered(chat)
    chat.hold(datetime(2026, 10, 13, 20, tzinfo=UTC), "taken")
    reply = chat.text(ACCEPT)
    assert not reply.committed and "no longer open" in reply.text
    assert len(chat.calendar()) == 1


def test_free_form_acceptance_of_a_move_offer_keeps_the_original_confirmed() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["Can I move Thursday to Friday morning?"] = ask(
        "reschedule", "2026-10-02", "2026-10-02", "08:00", "12:00", "2026-10-01")
    chat.text("Can I move Thursday to Friday morning?")
    chat.model.replies[ACCEPT] = ask(
        "request_booking", "2026-10-02", "2026-10-02", "08:00", "08:00")
    moved = chat.text(ACCEPT)
    assert moved.committed and moved.text.startswith("Requested a move to Fri Oct 2 at 8:00 AM")
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    assert moved.appointment_id is not None
    replacement = chat.store.read_appointment(moved.appointment_id)
    assert replacement is not None and replacement.replaces_appointment_id == visit


def test_another_client_on_the_same_phone_cannot_use_a_previous_clients_offer() -> None:
    chat = Harness()
    offered(chat)
    state = chat.states.read_state("pilot", "+14155550101")
    assert state is not None
    chat.states.put_state(ConversationState(
        state.business_id, state.sender, state.state_id, state.kind, state.created_at,
        state.expires_at, state.options, state.appointment_id, state.appointment_version,
        "client-other"))
    reply = chat.text(ACCEPT)
    assert not reply.committed and chat.calendar() == ()


# Client cancellation and rescheduling through the model (#273). These words are not
# recognized by the keyword matcher, so every case below really goes through the model.
DROP = "please go ahead and drop it"
KEEP = "actually let's hold onto it"


def cancel_asked(chat: Harness) -> str:
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["I can't make Thursday"] = ask("cancel", target_date="2026-10-01")
    chat.model.replies[DROP] = ask("confirm_cancel")
    chat.model.replies[KEEP] = ask("keep_visit")
    assert chat.text("I can't make Thursday").text.startswith("Cancel your Thu Oct 1")
    return visit


def test_model_reads_a_free_form_yes_and_cancels_the_asked_visit() -> None:
    chat = Harness()
    visit = cancel_asked(chat)
    calls = len(chat.model.calls)
    done = chat.text(DROP)
    assert len(chat.model.calls) == calls + 1  # The model, not the keyword matcher, decided.
    assert done.committed and done.text == (
        f"Cancelled your Thu Oct 1 at 9:00 AM visit (ref {visit[:8]}).")
    assert chat.status(visit) == CalendarStatus.CANCELLED


def test_valid_cancel_draft_uses_the_committed_visit_and_reference() -> None:
    model = DraftScript()
    chat = Harness(model)
    visit = cancel_asked(chat)
    model.drafts[DROP] = f"Your Thu Oct 1 at 9:00 AM visit was cancelled. Ref {visit[:8]}."
    result = chat.text(DROP)
    assert result.committed and result.text == model.drafts[DROP]
    assert result.client_outbox_id is not None
    assert chat.status(visit) == CalendarStatus.CANCELLED


def test_model_reads_a_free_form_no_and_keeps_the_visit() -> None:
    chat = Harness()
    visit = cancel_asked(chat)
    calls = len(chat.model.calls)
    kept = chat.text(KEEP)
    assert len(chat.model.calls) == calls + 1
    assert not kept.committed and "I kept your Thu Oct 1 at 9:00 AM visit" in kept.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    assert not chat.text(DROP).committed  # The question is closed.
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_wrong_reference_in_kept_visit_draft_falls_back_to_authoritative_text() -> None:
    model = DraftScript()
    chat = Harness(model)
    visit = cancel_asked(chat)
    model.drafts[KEEP] = (
        "Your Thu Oct 1 at 9:00 AM visit, ref deadbeef, was kept. "
        "Nothing was cancelled.")
    outcome = chat.text(KEEP)
    assert not outcome.committed
    assert outcome.text == "OK, I kept your Thu Oct 1 at 9:00 AM visit. Nothing was cancelled."
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_ambiguous_target_is_listed_then_model_choice_still_needs_confirmation() -> None:
    chat = Harness()
    thursday = chat.hold(THURSDAY, "one", confirm=True)
    friday = chat.hold(THURSDAY + timedelta(days=1), "two", confirm=True)
    chat.model.replies["cancel one"] = ask("cancel")
    chat.model.replies["whichever is later"] = ask("cancel", target_date="2026-10-02")
    chat.model.replies[DROP] = ask("confirm_cancel")
    assert "Which visit would you like to cancel?" in chat.text("cancel one").text
    # An acceptance with nothing selected cancels nothing.
    not_yet = chat.text(DROP)
    assert not not_yet.committed and "nothing was cancelled" in not_yet.text
    chat.text("cancel one")
    asked = chat.text("whichever is later")
    assert asked.text == "Cancel your Fri Oct 2 at 9:00 AM visit? Reply YES to confirm."
    assert chat.status(friday) == CalendarStatus.CONFIRMED
    assert chat.text(DROP).committed
    assert chat.status(friday) == CalendarStatus.CANCELLED
    assert chat.status(thursday) == CalendarStatus.CONFIRMED


def test_model_confirmation_after_expiry_cancels_nothing() -> None:
    chat = Harness()
    visit = cancel_asked(chat)
    chat.now += timedelta(minutes=31)
    calls = len(chat.model.calls)
    late = chat.text(DROP)
    assert len(chat.model.calls) == calls + 1
    assert not late.committed and "expired" in late.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_model_confirmation_refuses_a_visit_changed_since_the_question() -> None:
    chat = Harness()
    visit = cancel_asked(chat)
    LifecycleService(chat.store, lambda: NOW).apply(AppointmentCommand(
        "pilot", visit, "owner", ActorRole.OWNER, Action.EDIT, "owner-edit", 2,
        duration_minutes=90))
    result = chat.text(DROP)
    assert not result.committed and "changed since I asked" in result.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_model_confirmation_without_an_open_question_or_after_a_new_one_writes_nothing() -> None:
    chat = Harness()
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies[DROP] = ask("confirm_cancel")
    assert not chat.text(DROP).committed  # Nothing was asked.
    chat.model.replies["Anything Friday?"] = ask("availability", "2026-10-02")
    chat.model.replies["I can't make Thursday"] = ask("cancel", target_date="2026-10-01")
    chat.text("I can't make Thursday")
    chat.text("Anything Friday?")  # A new request replaces the confirmation.
    assert not chat.text(DROP).committed
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_repeated_model_confirmation_cancels_once() -> None:
    chat = Harness()
    visit = cancel_asked(chat)
    saved = chat.states.read_state("pilot", "+14155550101")
    assert saved is not None
    first = chat.text(DROP)
    chat.states.put_state(saved)  # The same stored question is open when the SMS is redelivered.
    chat.count -= 1  # The same inbound SM id is delivered again.
    again = chat.text(DROP)
    assert first.committed and not again.committed
    assert chat.status(visit) == CalendarStatus.CANCELLED
    version = chat.store.read_appointment(visit)
    assert version is not None and version.version == 3  # One cancellation, not two.


@pytest.mark.parametrize("opener", ["move", "list"])
def test_model_confirm_cancel_with_another_prompt_open_writes_nothing(opener: str) -> None:
    chat = Harness()
    if opener == "move":
        visit = move_offered(chat)
    else:
        visit = chat.hold(THURSDAY, "visit", confirm=True)
        chat.hold(THURSDAY + timedelta(days=1), "second", confirm=True)
        chat.model.replies["cancel one"] = ask("cancel")
        chat.text("cancel one")
    chat.model.replies[DROP] = ask("confirm_cancel")
    before = chat.calendar()
    reply = chat.text(DROP)
    assert not reply.committed and "nothing was cancelled" in reply.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    assert chat.calendar() == before


def test_keep_visit_while_a_move_offer_is_open_says_nothing_changed() -> None:
    chat = Harness()
    visit = move_offered(chat)
    chat.model.replies["never mind, I'll keep Thursday"] = ask("keep_visit")
    reply = chat.text("never mind, I'll keep Thursday")
    assert reply.text == "OK, nothing changed. Your visit stays booked."
    assert not reply.committed and chat.status(visit) == CalendarStatus.CONFIRMED
    assert len(chat.calendar()) == 1


def test_model_confirmation_cannot_use_another_clients_question_on_the_same_phone() -> None:
    chat = Harness()
    visit = cancel_asked(chat)
    state = chat.states.read_state("pilot", "+14155550101")
    assert state is not None
    chat.states.put_state(ConversationState(
        state.business_id, state.sender, state.state_id, state.kind, state.created_at,
        state.expires_at, state.options, state.appointment_id, state.appointment_version,
        "client-other"))
    assert not chat.text(DROP).committed
    assert chat.status(visit) == CalendarStatus.CONFIRMED


def test_model_cannot_cancel_another_clients_visit_by_name() -> None:
    chat = Harness()
    other = HoldService(chat.store).create(CreateHold(
        "pilot", "client-2", "client-2", "other", THURSDAY, 120), NOW).hold_id
    chat.model.replies["cancel theirs"] = MessageProposal(
        "cancel", other[:8], None, None, False, target_date="2026-10-01")
    chat.model.replies[DROP] = ask("confirm_cancel")
    reply = chat.text("cancel theirs")
    assert "don't see" in reply.text
    assert not chat.text(DROP).committed
    assert chat.status(other) == CalendarStatus.PENDING_APPROVAL


MOVE_ACCEPT = "wonderful, put me down for it"


def move_offered(chat: Harness) -> str:
    visit = chat.hold(THURSDAY, "visit", confirm=True)
    chat.model.replies["Can I move Thursday to Friday morning?"] = ask(
        "reschedule", "2026-10-02", "2026-10-02", "08:00", "12:00", "2026-10-01")
    chat.model.replies[MOVE_ACCEPT] = ask(
        "request_booking", "2026-10-02", "2026-10-02", "08:00", "08:00")
    chat.text("Can I move Thursday to Friday morning?")
    return visit


def test_free_form_move_acceptance_binds_the_original_and_is_idempotent() -> None:
    chat = Harness()
    visit = move_offered(chat)
    offer = chat.states.read_state("pilot", "+14155550101")
    assert offer is not None
    calls = len(chat.model.calls)
    moved = chat.text(MOVE_ACCEPT)
    assert len(chat.model.calls) == calls + 1
    assert moved.committed and "remains confirmed" in moved.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    saved = chat.states.read_state("pilot", "+14155550101")
    assert saved is None
    chat.count -= 1  # Redelivery of the same inbound SM id with the offer still stored.
    chat.states.put_state(offer)
    again = chat.text(MOVE_ACCEPT)
    assert again.committed and again.appointment_id == moved.appointment_id  # A replay.
    assert len(chat.calendar()) == 2  # The original and one replacement.


def test_drafted_move_request_keeps_original_confirmation_separate() -> None:
    model = DraftScript()
    chat = Harness(model)
    original = move_offered(chat)
    model.drafts[MOVE_ACCEPT] = lambda result: (
        f"Fri Oct 2 at 8:00 AM, ref {result.facts[0].reference}, is pending owner approval; "
        f"Thu Oct 1 at 9:00 AM, ref {result.facts[1].reference}, remains confirmed.")
    moved = chat.text(MOVE_ACCEPT)
    assert moved.committed and moved.text != model.draft_calls[-1][1].fallback
    assert model.draft_calls[-1][1].kind == "move_requested"
    assert moved.appointment_id is not None and moved.appointment_id[:8] in moved.text
    assert original[:8] in moved.text
    assert chat.status(original) == CalendarStatus.CONFIRMED


def test_free_form_move_acceptance_after_expiry_or_conflict_changes_nothing() -> None:
    chat = Harness()
    visit = move_offered(chat)
    chat.hold(datetime(2026, 10, 2, 8, tzinfo=ZONE).astimezone(UTC), "taken")
    conflict = chat.text(MOVE_ACCEPT)
    assert not conflict.committed and "no longer open" in conflict.text
    assert chat.status(visit) == CalendarStatus.CONFIRMED
    other = Harness()
    move_offered(other)
    other.now += timedelta(minutes=31)
    assert not other.text(MOVE_ACCEPT).committed
    assert len(other.calendar()) == 1


def test_free_form_move_acceptance_is_refused_when_the_original_was_cancelled() -> None:
    chat = Harness()
    visit = move_offered(chat)
    LifecycleService(chat.store, lambda: NOW).apply(AppointmentCommand(
        "pilot", visit, "owner", ActorRole.OWNER, Action.CANCEL, "owner-cancel", 2))
    moved = chat.text(MOVE_ACCEPT)
    assert not moved.committed and "has changed" in moved.text
    assert chat.calendar() == ()


def test_opted_out_client_reaches_neither_the_interpreter_nor_the_drafter() -> None:
    model = DraftScript()
    chat = Harness(model)
    model.replies["Oct 13 at 1 pm"] = ask("availability", "2026-10-13", "2026-10-13", "13:00", "13:00")

    class OptedOut(Consent):
        def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
            return True

    chat.service = ConversationService(
        chat.store, model, HoldService(chat.store),
        LifecycleService(chat.store, lambda: chat.now), OptedOut(), lambda: chat.now,
        "+14155559999", chat.states)
    reply = chat.text("Oct 13 at 1 pm")
    assert "opted out" in reply.text and not reply.committed
    assert model.calls == [] and model.draft_calls == []
    assert chat.calendar() == ()


def test_overlong_model_draft_falls_back_to_the_one_segment_safe_reply() -> None:
    model = DraftScript()
    chat = Harness(model)
    invited(chat)
    body = "Oct 13 at 1 pm (availability)"
    model.drafts[body] = "Tue Oct 13 at 1:00 PM is open. Reply YES to request it. " + "Thanks! " * 30
    offer = chat.text(body)
    assert offer.text == model.draft_calls[-1][1].fallback
    assert len(chat.calendar()) == 0
