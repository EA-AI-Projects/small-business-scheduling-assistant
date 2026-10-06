"""A client accepts an owner counteroffer: one linked pending request, never a confirmation."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest
from test_owner_counteroffer import (
    ASK,
    CLIENT_PHONE,
    NOW,
    OWNER,
    THURSDAY_2PM,
    THURSDAY_9AM,
    Consent,
    Messages,
    NeverModel,
    Repository,
    profile,
)

from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.conversation import (
    ConversationOutcome,
    ConversationService,
    MessageContext,
    MessageProposal,
)
from scheduling.domain.holds import CreateHold, HoldService, OutboxIntent
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    LifecycleService,
)
from scheduling.domain.outbox import DeliveryState, OutboxRecord
from scheduling.domain.owner_counteroffer import (
    CounterofferAcceptance,
    CounterofferService,
    InMemoryCounterofferStore,
    OfferState,
)
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole


@dataclass
class World:
    repository: Repository
    store: InMemoryCounterofferStore
    consent: Consent
    service: ConversationService
    sender: TwilioSmsSender
    messages: Messages
    clock: list[datetime]
    count: int = 0

    def owner(self, body: str) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "pilot", f"SM-o{self.count}", OWNER, "+14155550000", body, self.clock[0],
            SenderRole.OWNER, None, Keyword.OTHER, True))

    def client(self, body: str, provider_id: str | None = None) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "pilot", provider_id or f"SM-c{self.count}", CLIENT_PHONE, "+14155550000", body,
            self.clock[0], SenderRole.CLIENT, "client-1", Keyword.OTHER, True))

    def offered(self) -> str:
        """A pending Thursday 9 AM request with a confirmed 2 PM counteroffer; its ID."""
        request = HoldService(self.repository).create(CreateHold(
            "pilot", "client-1", "client-1", "seed", THURSDAY_9AM, 120), NOW).hold_id
        self.owner(ASK)
        assert "Queued the offer" in self.owner("YES").text
        return request

    def appointment(self, appointment_id: str):  # type: ignore[no-untyped-def]
        found = self.repository.read_appointment(appointment_id)
        assert found is not None
        return found

    def replacement_of(self, request: str):  # type: ignore[no-untyped-def]
        (found,) = (a for a in self.repository._appointments.values()
                    if a.replaces_appointment_id == request)
        return found

    def lifecycle(self) -> LifecycleService:
        return LifecycleService(self.repository, lambda: self.clock[0])

    def owner_command(self, appointment_id: str, action: Action, key: str):  # type: ignore[no-untyped-def]
        return self.lifecycle().apply(AppointmentCommand(
            "pilot", appointment_id, OWNER, ActorRole.OWNER, action, key,
            self.appointment(appointment_id).version))

    def outbox(self, template: str) -> list[OutboxIntent]:
        return [intent for intent in self.repository.list_outbox_intents()
                if intent.template == template]


@pytest.fixture
def world() -> World:
    repository = Repository()
    repository.save_profile(profile("client-1", "Avery Sample", CLIENT_PHONE), 0, None)
    store, consent, clock = InMemoryCounterofferStore(), Consent(), [NOW]
    holds = HoldService(repository)
    service = ConversationService(
        repository, NeverModel(), holds,
        LifecycleService(repository, lambda: clock[0]), consent, lambda: clock[0], OWNER,
        counteroffers=CounterofferService(repository, consent, store, OWNER),
        counteroffer_acceptance=CounterofferAcceptance(repository, consent, store, holds))
    messages = Messages()
    sender = TwilioSmsSender(messages, repository, consent,  # type: ignore[arg-type]
                             "pilot", "+14155550000", OWNER, clock=lambda: clock[0],
                             counteroffers=store)
    return World(repository, store, consent, service, sender, messages, clock)


def render(world: World, intent: OutboxIntent) -> str:
    """Deliver one committed notification through the real sender; return the text sent."""
    world.sender.deliver(OutboxRecord(
        "pilot", intent.outbox_id, intent.hold_id, intent.recipient, intent.template,
        world.appointment(intent.hold_id).version, DeliveryState.PENDING, NOW, NOW, NOW))
    return str(world.messages.calls[-1]["body"])


def test_acceptance_creates_one_linked_pending_request_and_confirms_nothing(world: World) -> None:
    request = world.offered()
    world.clock[0] = NOW + timedelta(minutes=5)
    reply = world.client("Yes")
    assert reply.committed and "pending owner approval" in reply.text
    assert "Thu Oct 1 at 2:00 PM" in reply.text and "Thu Oct 1 at 9:00 AM" in reply.text
    assert "is confirmed" not in reply.text.lower()
    new = world.replacement_of(request)
    assert (new.status, new.start_at, new.client_id) == (
        CalendarStatus.PENDING_APPROVAL, THURSDAY_2PM, "client-1")
    original = world.appointment(request)
    assert (original.status, original.version) == (CalendarStatus.PENDING_APPROVAL, 1)
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.ACCEPTED
    assert offer.accepted_request_id == new.appointment_id
    # The owner is told it is an accepted counteroffer replacing the original, with both times.
    (intent,) = world.outbox("counteroffer-request")
    text = render(world, intent)
    assert "accepted your counteroffer" in text and "Thu Oct 1 at 2:00 PM" in text
    assert "Thu Oct 1 at 9:00 AM" in text and "replaces" in text and "confirmed" not in text


def test_owner_cannot_approve_the_original_while_the_replacement_waits(world: World) -> None:
    request = world.offered()
    world.client("YES")
    assert "replacement" in world.owner(f"APPROVE {request[:8]}").text
    assert world.appointment(request).status == CalendarStatus.PENDING_APPROVAL


def test_approval_resolves_both_requests_in_one_transaction(world: World) -> None:
    request = world.offered()
    world.client("YES")
    new = world.replacement_of(request)
    reply = world.owner(f"APPROVE {new.appointment_id[:8]}")
    assert "Both requests are resolved" in reply.text
    assert "Thu Oct 1 at 2:00 PM" in reply.text and "Thu Oct 1 at 9:00 AM" in reply.text
    assert world.appointment(new.appointment_id).status == CalendarStatus.CONFIRMED
    assert world.appointment(request).status == CalendarStatus.DECLINED
    events = {e.event_id for e in world.repository.read_calendar("pilot").events}
    assert events == {new.appointment_id}
    owner_note = next(i for i in world.outbox("counteroffer-approved")
                      if i.recipient == "owner")
    assert "Both requests are resolved" in render(world, owner_note)
    client_note = next(i for i in world.outbox("counteroffer-approved")
                       if i.recipient == "client")
    assert "confirmed: Thu Oct 1 at 2:00 PM" in render(world, client_note)


def test_decline_of_the_new_request_keeps_the_original_pending(world: World) -> None:
    request = world.offered()
    world.client("YES")
    new = world.replacement_of(request)
    assert "declined" in world.owner(f"DECLINE {new.appointment_id[:8]}").text
    assert world.appointment(new.appointment_id).status == CalendarStatus.DECLINED
    assert world.appointment(request).status == CalendarStatus.PENDING_APPROVAL
    client_note = next(i for i in world.outbox("counteroffer-declined")
                       if i.recipient == "client")
    assert "still pending owner approval" in render(world, client_note)
    # With the replacement gone the original can be approved on its own again.
    assert world.owner(f"APPROVE {request[:8]}").committed


def test_new_request_expiring_leaves_the_original_pending(world: World) -> None:
    request = world.offered()
    world.client("YES")
    new = world.replacement_of(request)
    # The new hold lasts 24 hours from acceptance, so it outlives the original.
    world.clock[0] = NOW + timedelta(hours=24, minutes=1)
    assert world.appointment(request).hold_expires_at < world.clock[0]
    # Make the original valid for longer than the replacement, then expire only the new one.
    world.repository._appointments[request] = replace(
        world.appointment(request), hold_expires_at=world.clock[0] + timedelta(hours=5))
    world.lifecycle().apply(AppointmentCommand(
        "pilot", new.appointment_id, "system", ActorRole.SYSTEM, Action.EXPIRE, "expire-1",
        new.version))
    assert world.appointment(new.appointment_id).status == CalendarStatus.EXPIRED
    assert world.appointment(request).status == CalendarStatus.PENDING_APPROVAL


def test_original_expiring_first_leaves_the_new_request_approvable_alone(world: World) -> None:
    request = world.offered()
    world.clock[0] = NOW + timedelta(minutes=10)
    world.client("YES")
    new = world.replacement_of(request)
    world.clock[0] = NOW + timedelta(hours=24, minutes=1)  # Original lapsed; new still open.
    world.lifecycle().apply(AppointmentCommand(
        "pilot", request, "system", ActorRole.SYSTEM, Action.EXPIRE, "expire-orig",
        world.appointment(request).version))
    assert world.appointment(request).status == CalendarStatus.EXPIRED
    assert world.appointment(new.appointment_id).status == CalendarStatus.PENDING_APPROVAL
    reply = world.owner(f"APPROVE {new.appointment_id[:8]}")
    assert reply.committed and "Both requests" not in reply.text
    assert world.appointment(new.appointment_id).status == CalendarStatus.CONFIRMED


def test_expired_offer_creates_nothing_and_asks_for_current_times(world: World) -> None:
    request = world.offered()
    world.clock[0] = NOW + timedelta(minutes=31)
    reply = world.client("YES")
    assert "expired after 30 minutes" in reply.text and "current open times" in reply.text
    assert not reply.committed
    assert all(a.replaces_appointment_id is None
               for a in world.repository._appointments.values())
    assert world.appointment(request).status == CalendarStatus.PENDING_APPROVAL
    assert not any(a.replaces_appointment_id for a in
                   world.repository._appointments.values())


def test_offer_is_valid_just_inside_thirty_minutes(world: World) -> None:
    world.offered()
    world.clock[0] = NOW + timedelta(minutes=29, seconds=59)
    assert world.client("YES").committed


def test_slot_taken_between_offer_and_acceptance_creates_nothing(world: World) -> None:
    request = world.offered()
    HoldService(world.repository).create(CreateHold(
        "pilot", "client-2", "client-2", "other", THURSDAY_2PM, 120), NOW)
    reply = world.client("YES")
    assert not reply.committed and "nothing was requested" in reply.text
    assert not any(a.replaces_appointment_id for a in
                   world.repository._appointments.values())
    assert world.appointment(request).status == CalendarStatus.PENDING_APPROVAL


def test_changed_original_or_lost_consent_creates_nothing(world: World) -> None:
    request = world.offered()
    world.owner_command(request, Action.DECLINE, "dash-1")
    assert not world.client("YES").committed
    assert not any(a.replaces_appointment_id for a in
                   world.repository._appointments.values())


def test_consent_withdrawn_before_acceptance_creates_nothing(world: World) -> None:
    world.offered()
    world.consent.consented = False
    assert not world.client("YES").committed
    assert not any(a.replaces_appointment_id for a in
                   world.repository._appointments.values())


@pytest.mark.parametrize("body", [
    "No", "no thanks", "yes or no?", "not sure", "3 pm", "Yes, or maybe Friday"])
def test_negated_ambiguous_and_other_time_replies_create_nothing(
        world: World, body: str) -> None:
    world.offered()  # A fresh offer for every reply variant.
    assert not world.client(body).committed
    assert not any(a.replaces_appointment_id for a in
                   world.repository._appointments.values())


def test_declining_the_offer_closes_it(world: World) -> None:
    world.offered()
    assert "Nothing was requested" in world.client("No thanks").text
    assert not world.client("YES").committed
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.SUPERSEDED


def test_ambiguous_reply_keeps_the_offer_open(world: World) -> None:
    world.offered()
    assert not world.client("maybe").committed
    assert world.client("YES").committed


def test_a_calendar_question_keeps_the_offer_open(world: World) -> None:
    model = ScriptedModel()
    model.proposal = MessageProposal("calendar_question", None, None, None, False,
                                     "2026-09-28", "2026-10-04")
    world.service._interpreter = model
    world.offered()
    answer = world.client("Do I have bookings this week?")
    assert "pending owner approval, not confirmed yet" in answer.text
    assert not answer.committed
    assert world.client("YES").committed  # The counteroffer is still there to accept.


def test_a_new_client_request_replaces_the_offer(world: World) -> None:
    world.offered()
    world.client("BOOK 2026-10-02 10:00")
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.SUPERSEDED
    assert not any(a.replaces_appointment_id for a in
                   world.repository._appointments.values())
    # A later YES finds no offer, so it creates no replacement request.
    world.client("YES")
    assert not any(a.replaces_appointment_id for a in
                   world.repository._appointments.values())


def test_duplicate_delivery_and_second_yes_create_exactly_one_request(world: World) -> None:
    request = world.offered()
    first = world.client("YES", "SM-dup")
    again = world.client("YES", "SM-dup")  # Same provider message delivered twice.
    other = world.client("YES")  # A second, different YES.
    assert first.committed and not other.committed and "nothing more was requested" in other.text
    assert again.committed or "already" in again.text
    created = [a for a in world.repository._appointments.values()
               if a.replaces_appointment_id == request]
    assert len(created) == 1
    assert len(world.outbox("counteroffer-request")) == 1


class ScriptedModel:
    """Stands in for the language model: returns the proposal the test sets."""

    def __init__(self) -> None:
        self.proposal = MessageProposal("clarify", None, None, None, True)

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        return self.proposal


def _accepted_and_approved(world: World) -> ScriptedModel:
    model = ScriptedModel()
    world.service._interpreter = model
    request = world.offered()
    world.clock[0] = NOW + timedelta(minutes=5)
    assert world.client("YES").committed
    new = world.replacement_of(request)
    assert world.owner(f"APPROVE {new.appointment_id[:8]}").committed
    world.clock[0] = NOW + timedelta(hours=3)
    return model


def test_a_yes_after_the_replacement_is_approved_answers_a_normal_offer(world: World) -> None:
    model = _accepted_and_approved(world)
    model.proposal = MessageProposal(
        "request_booking", None, None, None, False, "2026-10-02", "2026-10-02",
        "10:00", "10:00")
    offer = world.client("Could I get Friday at 10?")
    assert "Fri Oct 2 at 10:00 AM" in offer.text and not offer.committed
    reply = world.client("yes")
    assert reply.committed and "Requested Fri Oct 2 at 10:00 AM" in reply.text
    assert "already asked" not in reply.text
    friday = datetime(2026, 10, 2, 17, tzinfo=THURSDAY_2PM.tzinfo)
    assert any(a.start_at == friday and a.status == CalendarStatus.PENDING_APPROVAL
               for a in world.repository._appointments.values())


def test_a_yes_after_the_replacement_is_approved_confirms_a_cancellation(world: World) -> None:
    model = _accepted_and_approved(world)
    model.proposal = MessageProposal("cancel", None, None, None, False, target_date="2026-10-01")
    asked = world.client("Please cancel my Thursday visit")
    assert "Cancel your Thu Oct 1 at 2:00 PM visit?" in asked.text
    done = world.client("yes")
    assert done.committed and "Cancelled" in done.text
    assert "already asked" not in done.text


def test_a_repeat_yes_is_answered_only_while_the_new_request_is_pending(world: World) -> None:
    world.offered()
    world.clock[0] = NOW + timedelta(minutes=5)
    assert world.client("YES").committed
    assert "nothing more was requested" in world.client("YES").text


def test_a_new_request_ends_an_accepted_offer(world: World) -> None:
    world.offered()
    world.clock[0] = NOW + timedelta(minutes=5)
    world.client("YES")
    world.client("BOOK 2026-10-02 10:00")
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.SUPERSEDED


def test_a_lost_accepted_write_is_repaired_when_the_yes_is_redelivered(world: World) -> None:
    request = world.offered()
    real_accept = world.store.accept
    world.store.accept = lambda offer, request_id: None  # type: ignore[method-assign]
    assert world.client("YES", "SM-crash").committed  # The process died before ACCEPTED.
    world.store.accept = real_accept  # type: ignore[method-assign]
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.CONFIRMED
    again = world.client("YES", "SM-crash")
    assert again.committed and "Requested Thu Oct 1 at 2:00 PM" in again.text
    assert "nothing was requested" not in again.text
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.ACCEPTED
    assert len([a for a in world.repository._appointments.values()
                if a.replaces_appointment_id == request]) == 1


def _crashed_after_hold(world: World) -> str:
    """The request exists but the offer is still CONFIRMED (the ACCEPTED write was lost)."""
    request = world.offered()
    world.clock[0] = NOW + timedelta(minutes=5)
    real_accept = world.store.accept
    world.store.accept = lambda offer, request_id: None  # type: ignore[method-assign]
    assert world.client("YES", "SM-crash").committed
    world.store.accept = real_accept  # type: ignore[method-assign]
    return world.replacement_of(request).appointment_id


def _says_no_false_status(text: str) -> None:
    assert "pending owner approval" not in text and "Requested" not in text
    assert "nothing was requested" not in text.lower()


@pytest.mark.parametrize("decision", [Action.APPROVE, Action.DECLINE])
def test_redelivered_yes_after_the_owner_decided_gives_no_false_status(
        world: World, decision: Action) -> None:
    new = _crashed_after_hold(world)
    world.owner_command(new, decision, "dash-decide")
    reply = world.client("YES", "SM-crash")
    _says_no_false_status(reply.text)
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.ACCEPTED


def test_no_while_the_lost_write_request_is_pending_says_so_truthfully(world: World) -> None:
    new = _crashed_after_hold(world)
    reply = world.client("No")
    assert "pending owner approval" in reply.text and "Nothing was requested" not in reply.text
    assert world.appointment(new).status == CalendarStatus.PENDING_APPROVAL
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.ACCEPTED


def test_redelivery_after_the_offer_window_gives_no_success_reply(world: World) -> None:
    _crashed_after_hold(world)
    world.clock[0] = NOW + timedelta(minutes=40)
    _says_no_false_status(world.client("YES", "SM-crash").text)


def test_original_expiry_text_still_goes_out_and_mentions_the_waiting_request(
        world: World) -> None:
    request = world.offered()
    world.clock[0] = NOW + timedelta(minutes=10)
    world.client("YES")
    world.clock[0] = NOW + timedelta(hours=24, minutes=1)
    world.lifecycle().apply(AppointmentCommand(
        "pilot", request, "system", ActorRole.SYSTEM, Action.EXPIRE, "expire-orig",
        world.appointment(request).version))
    (intent,) = world.outbox("expire-replacement-waiting")
    text = render(world, intent)
    assert text.startswith("Cleaning visit expired: Thu Oct 1 at 9:00 AM.")
    assert "request for Thu Oct 1 at 2:00 PM" in text and "still pending owner approval" in text
    assert "confirmed" not in text


def test_only_that_clients_offer_can_be_accepted(world: World) -> None:
    world.repository.save_profile(profile("client-2", "Blake Example", "+15005550007"), 0, None)
    world.offered()
    world.count += 1
    other = world.service.handle(InboundReceipt(
        "pilot", "SM-x", "+15005550007", "+14155550000", "YES", NOW, SenderRole.CLIENT,
        "client-2", Keyword.OTHER, True))
    assert not other.committed
    assert not any(a.replaces_appointment_id for a in
                   world.repository._appointments.values())
