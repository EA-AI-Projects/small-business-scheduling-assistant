"""Owner counteroffers: prepare, confirm, and send only what the owner reviewed."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.conversation import (
    ConversationOutcome,
    ConversationService,
    MessageContext,
    MessageProposal,
)
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.outbox import PermanentDeliveryFailure
from scheduling.domain.owner_counteroffer import (
    COUNTEROFFER_FAILED_TEMPLATE,
    COUNTEROFFER_TEMPLATE,
    CounterofferService,
    InMemoryCounterofferStore,
    OfferState,
)
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyContext,
    OwnerReplyIntent,
    OwnerReplyProposal,
)
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)  # Tue Sep 29, 10:00 AM Pacific
THURSDAY_9AM = datetime(2026, 10, 1, 16, tzinfo=UTC)
THURSDAY_2PM = datetime(2026, 10, 1, 21, tzinfo=UTC)
OWNER = "+15005550009"
CLIENT_PHONE = "+15005550006"
ASK = "That doesn't work for me. Can you offer 2:00 PM instead?"
EXPECTED_TEXT = (
    "We can't do your cleaning request for Thu Oct 1 at 9:00 AM. Could Thu Oct 1 at 2:00 PM "
    "work instead? Reply YES to request that time. The owner still has to approve it, so it "
    "is not confirmed yet.")


class NeverModel:
    """A hostile model: it calls every owner reply a decision about the one pending request.

    Backend validation, not the model, must keep an open offer from being approved.
    """

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        return MessageProposal("clarify", None, None, None, True)

    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal:
        if len(context.pending) != 1:
            return OwnerReplyProposal(OwnerReplyIntent.UNCLEAR, None, Confidence.HIGH)
        return OwnerReplyProposal(OwnerReplyIntent.APPROVE_NAMED_REQUEST,
                                  context.pending[0].ref, Confidence.HIGH)


class Consent:
    def __init__(self) -> None:
        self.opted_out = False
        self.consented = True

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return self.opted_out and phone_e164 != OWNER

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        if not self.consented:
            return None
        client = "client-1" if phone_e164 == CLIENT_PHONE else "client-2"
        return ConsentEvidence(business_id, client, "Synthetic Client", phone_e164, NOW, "v1")

    def record_outbound(self, *args: object) -> None:
        pass


class Messages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return type("Sent", (), {"sid": f"SM-{len(self.calls)}"})()


class Repository(InMemoryCalendarRepository):
    def acquire_client_send(self, business_id: str, client_id: str) -> str:
        return "synthetic-token"

    def release_client_send(self, business_id: str, client_id: str, token: str) -> None:
        pass


@dataclass
class World:
    repository: InMemoryCalendarRepository
    store: InMemoryCounterofferStore
    consent: Consent
    service: ConversationService
    messages: Messages
    sender: TwilioSmsSender
    clock: list[datetime]
    count: int = 0

    def say(self, body: str, provider_id: str | None = None) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "pilot", provider_id or f"SM-{self.count}", OWNER, "+14155550000", body, self.clock[0],
            SenderRole.OWNER, None, Keyword.OTHER, True))

    def pending(self, start: datetime = THURSDAY_9AM, client: str = "client-1",
                key: str = "seed") -> str:
        return HoldService(self.repository).create(CreateHold(
            "pilot", client, client, key, start, 120), NOW).hold_id

    def status(self, appointment_id: str) -> tuple[CalendarStatus, int]:
        appointment = self.repository.read_appointment(appointment_id)
        assert appointment is not None
        return appointment.status, appointment.version

    def deliver(self, template: str = COUNTEROFFER_TEMPLATE) -> str:
        (record,) = (r for r in self.store.outbox.values() if r.template == template)
        return self.sender.deliver(record)


def state_of(world: World) -> OfferState | None:
    offer = world.store.read_active("pilot", OWNER)
    return offer.state if offer is not None else None


def profile(client_id: str, name: str, phone: str) -> ClientProfile:
    return ClientProfile("pilot", client_id, name, phone, "123 Test Street", HomeSize.MEDIUM,
                         120, True, 1, NOW, NOW, NOW)


@pytest.fixture
def world() -> World:
    repository = Repository()
    repository.save_profile(profile("client-1", "Avery Sample", CLIENT_PHONE), 0, None)
    repository.save_profile(profile("client-2", "Blake Example", "+15005550007"), 0, None)
    store, consent, clock = InMemoryCounterofferStore(), Consent(), [NOW]
    counteroffers = CounterofferService(repository, consent, store, OWNER)
    service = ConversationService(
        repository, NeverModel(), HoldService(repository),
        LifecycleService(repository, lambda: clock[0]), consent, lambda: clock[0], OWNER,
        counteroffers=counteroffers)
    messages = Messages()
    sender = TwilioSmsSender(messages, repository, consent,  # type: ignore[arg-type]
                             "pilot", "+14155550000", OWNER, clock=lambda: clock[0],
                             counteroffers=store)
    return World(repository, store, consent, service, messages, sender, clock)


def test_owner_reviews_exact_offer_then_yes_sends_only_that_text(world: World) -> None:
    request = world.pending()
    prompt = world.say(ASK).text
    assert "Avery Sample" in prompt and "Thu Oct 1 at 2:00 PM" in prompt
    assert f'"{EXPECTED_TEXT}"' in prompt
    assert not world.store.outbox and not world.messages.calls

    sent = world.say("Yes")
    assert "Queued the offer to Avery Sample" in sent.text and "not approved" in sent.text
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    (record,) = world.store.outbox.values()
    assert record.template == COUNTEROFFER_TEMPLATE and record.recipient == "client"
    world.deliver()
    assert [(call["to"], call["body"]) for call in world.messages.calls] == [
        (CLIENT_PHONE, EXPECTED_TEXT)]
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None and offer.state == OfferState.CONFIRMED
    assert offer.request_id == request and offer.proposed_start == THURSDAY_2PM


def test_yes_that_confirms_an_offer_never_approves_the_request(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    world.say("YES")
    again = world.say("YES")  # A second plain YES is absorbed, not read as approval.
    assert "already queued" in again.text
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    assert len(world.store.outbox) == 1
    # Another reply while the sent offer is still absorbed is not an approval either.
    assert "Nothing was approved" in world.say("Looks good").text
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    # Once the notice window has passed, YES has its normal meaning again.
    world.clock[0] = NOW + timedelta(hours=2)
    assert world.say("YES").committed
    assert world.status(request)[0] == CalendarStatus.CONFIRMED


def test_ambiguous_request_asks_which_and_stores_nothing(world: World) -> None:
    first = world.pending(key="a")
    second = world.pending(THURSDAY_9AM + timedelta(hours=4), "client-2", "b")
    reply = world.say(ASK).text
    assert first[:8] in reply and second[:8] in reply and "Nothing was sent" in reply
    assert world.store.read_active("pilot", OWNER) is None
    named = world.say(f"Offer 12:00 PM instead for {second[:8]}").text
    assert "Blake Example" in named and "Thu Oct 1 at 12:00 PM" in named
    world.say("no")
    by_name = world.say("Offer Avery 2pm on 2026-10-02").text
    assert "Avery Sample" in by_name and "Fri Oct 2 at 2:00 PM" in by_name


def test_date_selects_among_pending_requests(world: World) -> None:
    world.pending(key="a")
    friday = world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "b")
    reply = world.say("Offer 2pm instead on Friday").text
    assert "Blake Example" in reply and "Fri Oct 2 at 2:00 PM" in reply
    assert friday[:8] in world.store.read_active("pilot", OWNER).request_id  # type: ignore[union-attr]


def test_decline_or_revision_or_other_message_sends_nothing(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    assert "nothing was sent" in world.say("No").text.lower()
    assert state_of(world) == OfferState.DISCARDED
    world.say(ASK)
    revised = world.say("Actually offer 3:00 PM instead").text
    assert "3:00 PM" in revised and "2:00 PM" not in revised.split("Text I would send")[0]
    world.say("DECLINE")  # Bare decline cancels the offer; it does not decline the request.
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    assert not world.say("YES").committed  # Absorbed: the owner just turned the offer down.
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    assert world.say(f"APPROVE {request[:8]}").committed  # The exact command still works.
    assert not world.store.outbox and not world.messages.calls


def test_unrelated_message_keeps_the_offer_and_says_so(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    reply = world.say("What is the weather?").text
    assert "still waiting" in reply and "cancelled" not in reply
    assert state_of(world) == OfferState.PROPOSED
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    assert world.say("YES").text.startswith("Queued the offer")  # Still sends what was reviewed.


def test_cancelled_or_expired_offer_is_not_followed_by_approval_on_a_bare_ok(
        world: World) -> None:
    request = world.pending()
    held = (CalendarStatus.PENDING_APPROVAL, 1)
    # "No" then "ok" / "ok thanks" / "yes".
    world.say(ASK)
    world.say("No")
    for reply in ("ok", "ok thanks", "yes"):
        assert "Nothing was approved" in world.say(reply).text
        assert world.status(request) == held
    # A reply that is neither a plain YES/NO nor a command leaves the offer waiting.
    for phrase in ("yes send the offer", "send it now", "approve it now"):
        world.say(ASK)
        assert "reply YES to send it or NO to cancel it" in world.say(phrase).text
        assert state_of(world) == OfferState.PROPOSED
    world.say("no")
    assert "Nothing was approved" in world.say("yes").text
    assert world.status(request) == held
    # Expired, "yes", then "ok".
    world.say(ASK)
    world.clock[0] = NOW + timedelta(minutes=31)
    assert "expired" in world.say("yes").text
    assert "Nothing was approved" in world.say("ok").text
    assert world.status(request) == held
    assert not world.store.outbox
    # Any other message clears it, and the exact command still approves.
    assert world.say(f"APPROVE {request[:8]}").committed
    assert world.status(request)[0] == CalendarStatus.CONFIRMED


def test_repeat_yes_after_a_send_time_failure_says_it_could_not_be_sent(world: World) -> None:
    world.pending()
    world.say(ASK)
    world.say("YES")
    world.pending(THURSDAY_2PM, "client-2", "taken")
    with pytest.raises(PermanentDeliveryFailure):
        world.deliver()
    reply = world.say("YES").text
    assert "could not be sent" in reply and "already queued" not in reply


def test_changed_request_blocks_the_send(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    LifecycleService(world.repository, lambda: NOW).apply(AppointmentCommand(
        "pilot", request, OWNER, ActorRole.OWNER, Action.DECLINE, "dashboard-1", 1))
    reply = world.say("YES").text
    assert "I did not send it" in reply and "no longer pending" in reply
    assert not world.store.outbox


def test_occupied_slot_is_refused_when_preparing(world: World) -> None:
    request = world.pending()
    world.pending(THURSDAY_2PM, "client-2", "other")
    refused = world.say(f"Offer 2:00 PM instead for {request[:8]}").text
    assert "not open" in refused and "another time" in refused
    assert world.store.read_active("pilot", OWNER) is None


def test_slot_taken_after_preparing_is_refused_when_confirming(world: World) -> None:
    world.pending()
    assert "2:00 PM" in world.say(ASK).text
    world.pending(THURSDAY_9AM + timedelta(hours=4), "client-2", "late")  # 1 PM, overlaps.
    blocked = world.say("YES").text
    assert "I did not send it" in blocked and "not open" in blocked
    assert not world.store.outbox and state_of(world) == OfferState.DISCARDED


def test_expired_confirmation_sends_nothing_and_is_not_approval(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    world.clock[0] = NOW + timedelta(minutes=31)
    reply = world.say("YES").text
    assert "expired" in reply and "not approved" in reply
    assert not world.store.outbox
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)


def test_duplicate_delivery_and_second_confirmation_send_once(world: World) -> None:
    world.pending()
    world.say(ASK, "SM-ask")
    assert world.say(ASK, "SM-ask").text.startswith("Offer for")  # Redelivered ask.
    first = world.say("YES", "SM-yes").text
    assert world.say("YES", "SM-yes").text == first  # Redelivered confirmation.
    assert world.say("YES", "SM-yes-2").text.startswith("That offer was already queued")
    assert len(world.store.outbox) == 1
    world.deliver()
    assert len(world.messages.calls) == 1


def test_stale_version_cannot_confirm(world: World) -> None:
    world.pending()
    world.say(ASK)
    stale = world.store.read_active("pilot", OWNER)
    assert stale is not None
    (_, ) = [world.store.confirm(stale, "SM-a", NOW, _outbox(world, stale))]
    assert world.store.confirm(stale, "SM-b", NOW, _outbox(world, stale)) is None
    assert len(world.store.outbox) == 1


def _outbox(world: World, offer: Any) -> Any:
    from scheduling.domain.owner_counteroffer import counteroffer_outbox
    return counteroffer_outbox(replace(offer, version=offer.version + 1), NOW)


def test_consent_and_eligibility_are_checked_at_prepare_and_confirm(world: World) -> None:
    world.pending()
    world.consent.consented = False
    assert "not consented" in world.say(ASK).text
    world.consent.consented = True
    world.say(ASK)
    world.consent.opted_out = True
    assert "I did not send it" in world.say("YES").text
    assert not world.store.outbox


def test_dispatch_rechecks_state_consent_and_slot(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    world.say("YES")
    world.consent.opted_out = True
    with pytest.raises(PermanentDeliveryFailure):
        world.deliver()
    world.consent.opted_out = False
    world.pending(THURSDAY_2PM, "client-2", "taken")
    with pytest.raises(PermanentDeliveryFailure) as slot:
        world.deliver()
    assert slot.value.code == "OFFER_SLOT_UNAVAILABLE"
    assert not world.messages.calls
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)


def test_send_time_failure_queues_one_owner_text_asking_for_another_time(world: World) -> None:
    world.pending()
    world.say(ASK)
    world.say("YES")
    world.pending(THURSDAY_2PM, "client-2", "taken")
    for _ in range(2):  # A retried delivery must not queue a second owner text.
        with pytest.raises(PermanentDeliveryFailure):
            world.deliver()
    failed = [r for r in world.store.outbox.values()
              if r.template == COUNTEROFFER_FAILED_TEMPLATE]
    assert len(failed) == 1 and failed[0].recipient == "owner"
    assert failed[0].outbox_id.startswith("counteroffer-failed#")
    world.deliver(COUNTEROFFER_FAILED_TEMPLATE)
    (call,) = world.messages.calls
    assert call["to"] == OWNER
    assert "couldn't send the offer to Avery Sample" in call["body"]
    assert "not open" in call["body"] and "another time" in call["body"]


@pytest.mark.parametrize("phrase", [
    "Yes, approve it", "Approved", "Approve", "Go ahead", "Send it", "OK approve", "decline it",
    "Yes please approve",
])
def test_approval_like_replies_never_fall_through_to_approving(
        world: World, phrase: str) -> None:
    request = world.pending()
    world.say(ASK)
    reply = world.say(phrase).text
    assert "reply YES to send it or NO to cancel it" in reply
    assert f"APPROVE {request[:8]}" in reply
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    assert not world.store.outbox
    # Nothing changed: the offer is still waiting, and YES now sends it, not approves.
    assert "Queued the offer" in world.say("YES").text
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)


@pytest.mark.parametrize("phrase", ["Yes, approve it", "Approved", "Go ahead", "Send it"])
def test_approval_like_reply_after_expiry_does_not_approve(
        world: World, phrase: str) -> None:
    request = world.pending()
    world.say(ASK)
    world.clock[0] = NOW + timedelta(minutes=31)
    reply = world.say(phrase).text
    assert "expired" in reply and f"APPROVE {request[:8]}" in reply
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    assert not world.store.outbox


def test_exact_approve_command_still_works_and_cancelling_is_announced(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    assert world.say(f"APPROVE {request[:8]}").committed
    assert world.status(request)[0] == CalendarStatus.CONFIRMED
    assert not world.store.outbox

    other = world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "again")
    world.say(f"Offer 2pm instead for {other[:8]}")
    assert "Blake Example" in world.say("no").text and state_of(world) == OfferState.DISCARDED


def test_repeat_yes_is_absorbed_only_while_the_request_is_still_pending(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    world.say("YES")
    LifecycleService(world.repository, lambda: NOW).apply(AppointmentCommand(
        "pilot", request, OWNER, ActorRole.OWNER, Action.DECLINE, "dashboard-2", 1))
    world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "next")
    reply = world.say("YES")
    assert "already queued" not in reply.text and reply.committed  # Normal approval.


def test_redelivered_ask_after_the_offer_finished_shows_no_prompt(world: World) -> None:
    world.pending()
    world.say(ASK, "SM-ask")
    world.say("YES", "SM-yes")
    reply = world.say(ASK, "SM-ask").text
    assert "Text I would send" not in reply and "already handled" in reply
    offer = world.store.read_active("pilot", OWNER)
    assert offer is not None and not world.store.put_draft(replace(
        offer, state=OfferState.PROPOSED, confirmed_by=None, confirmed_at=None))


def test_confirmed_offer_expiry_is_the_shared_client_validity(world: World) -> None:
    from scheduling.domain.owner_counteroffer import CLIENT_OFFER_VALIDITY
    world.pending()
    world.clock[0] = NOW + timedelta(minutes=10)
    world.say(ASK)
    world.clock[0] = NOW + timedelta(minutes=20)
    world.say("YES")
    offer = world.store.read_confirmed_for_client("pilot", "client-1")
    assert offer is not None
    assert offer.expires_at == NOW + timedelta(minutes=20) + CLIENT_OFFER_VALIDITY


def test_offer_may_overlap_the_requests_own_slot(world: World) -> None:
    request = world.pending()  # 9 AM to 11 AM; 10 AM overlaps only itself.
    reply = world.say(f"Offer 10:00 AM instead for {request[:8]}").text
    assert "Thu Oct 1 at 10:00 AM" in reply and "Text I would send" in reply
    assert world.say("YES").text.startswith("Queued")


def test_dispatch_refuses_an_offer_that_was_never_confirmed(world: World) -> None:
    from scheduling.domain.owner_counteroffer import counteroffer_outbox
    world.pending()
    world.say(ASK)
    draft = world.store.read_active("pilot", OWNER)
    assert draft is not None
    with pytest.raises(PermanentDeliveryFailure) as refused:
        world.sender.deliver(counteroffer_outbox(draft, NOW))
    assert refused.value.code == "OFFER_UNAVAILABLE"
    assert not world.messages.calls


def test_unrelated_owner_messages_are_not_handled(world: World) -> None:
    world.pending()
    for body in ("YES", "approve", "What clients do I have at 2 pm tomorrow?", "offer"):
        assert world.service._counteroffers.handle(  # type: ignore[union-attr]
            InboundReceipt("pilot", "SM-x", OWNER, "+14155550000", body, NOW, SenderRole.OWNER,
                           None, Keyword.OTHER, True), NOW, ()) is None


def test_time_without_am_pm_or_with_two_times_asks(world: World) -> None:
    world.pending()
    assert "AM or PM" in world.say("Offer 2 instead").text
    assert "more than one time" in world.say("Offer 2pm or 3pm instead").text
    assert world.store.read_active("pilot", OWNER) is None


def test_yes_after_a_successful_offer_expires_still_approves_nothing(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    world.say("YES")
    world.clock[0] = NOW + timedelta(minutes=31)  # Past the client-offer validity.
    for reply in ("yes", "ok"):
        assert "was not approved" in world.say(reply).text
        assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    world.clock[0] = NOW + timedelta(hours=2)  # Past the one-hour notice window.
    assert world.say("yes").committed
