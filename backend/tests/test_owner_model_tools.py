"""Owner conversation through the model's typed tools (#274).

Each text here is one the keyword matcher and counteroffer parser do not special-case, so the
model is really consulted (asserted through ``Model.calls``). The model only proposes; every
write is decided by stored state: exact current reference and version, owner confirmation
before an offer is sent, offer expiry, and the plain-reply safeguards.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pytest
from test_owner_counteroffer import (
    CLIENT_PHONE,
    NOW,
    OWNER,
    THURSDAY_2PM,
    THURSDAY_9AM,
    Consent,
    Messages,
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
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.owner_counteroffer import (
    CounterofferAcceptance,
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
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

THURSDAY = date(2026, 10, 1)
FRIDAY = date(2026, 10, 2)
ASK_FRIDAY_2PM = "Would Blake be free Friday around 2pm rather than the morning?"
ASK_THURSDAY_2PM = "Would Avery be free at 2pm Thursday rather than the morning?"
PENDING = CalendarStatus.PENDING_APPROVAL


class Model:
    """A scripted model: exact owner text to a tool call; unknown text makes it fail."""

    def __init__(self) -> None:
        self.script: dict[str, OwnerReplyProposal] = {}
        self.calls: list[str] = []
        self.contexts: list[OwnerReplyContext] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        return MessageProposal("clarify", None, None, None, True)

    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal:
        self.calls.append(body)
        self.contexts.append(context)
        if body not in self.script:
            raise RuntimeError("model unavailable")
        return self.script[body]


def call(intent: OwnerReplyIntent, ref: str | None = None, version: int | None = None,
         day: date | None = None, clock: time | None = None,
         confidence: Confidence = Confidence.HIGH) -> OwnerReplyProposal:
    return OwnerReplyProposal(intent, ref, confidence, None, None, None, version, day, clock)


@dataclass
class World:
    repository: Repository
    store: InMemoryCounterofferStore
    model: Model
    service: ConversationService
    sender: TwilioSmsSender
    clock: list[datetime]
    count: int = 0

    def owner(self, body: str, provider_id: str | None = None) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "pilot", provider_id or f"SM-o{self.count}", OWNER, "+14155550000", body,
            self.clock[0], SenderRole.OWNER, None, Keyword.OTHER, True))

    def client(self, body: str) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "pilot", f"SM-c{self.count}", CLIENT_PHONE, "+14155550000", body, self.clock[0],
            SenderRole.CLIENT, "client-1", Keyword.OTHER, True))

    def pending(self, start: datetime = THURSDAY_9AM, client: str = "client-1",
                key: str = "seed") -> str:
        return HoldService(self.repository).create(CreateHold(
            "pilot", client, client, key, start, 120), NOW).hold_id

    def state(self, appointment_id: str) -> tuple[CalendarStatus, int]:
        found = self.repository.read_appointment(appointment_id)
        assert found is not None
        return found.status, found.version

    def version(self, appointment_id: str) -> int:
        return self.state(appointment_id)[1]

    def offer_state(self) -> OfferState | None:
        offer = self.store.read_active("pilot", OWNER)
        return offer.state if offer is not None else None

    def offer_for(self, request: str, text: str, day: date = THURSDAY,
                  clock: time = time(14, 0)) -> None:
        """Script the model to prepare an offer for ``request`` at its current version."""
        self.model.script[text] = call(
            OwnerReplyIntent.PREPARE_COUNTEROFFER, request[:8], self.version(request), day, clock)


@pytest.fixture
def world() -> World:
    repository = Repository()
    repository.save_profile(profile("client-1", "Avery Sample", CLIENT_PHONE), 0, None)
    repository.save_profile(profile("client-2", "Blake Example", "+15005550007"), 0, None)
    store, consent, clock, model = InMemoryCounterofferStore(), Consent(), [NOW], Model()
    holds = HoldService(repository)
    service = ConversationService(
        repository, model, holds, LifecycleService(repository, lambda: clock[0]), consent,
        lambda: clock[0], OWNER,
        counteroffers=CounterofferService(repository, consent, store, OWNER),
        counteroffer_acceptance=CounterofferAcceptance(repository, consent, store, holds))
    sender = TwilioSmsSender(Messages(), repository, consent,  # type: ignore[arg-type]
                             "pilot", "+14155550000", OWNER, clock=lambda: clock[0],
                             counteroffers=store)
    return World(repository, store, model, service, sender, clock)


def two_requests(world: World) -> tuple[str, str]:
    first = world.pending(key="a")
    second = world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "b")
    return first, second


def test_model_prepares_an_offer_for_the_named_request_among_several(world: World) -> None:
    first, second = two_requests(world)
    world.offer_for(second, ASK_FRIDAY_2PM, FRIDAY)
    reply = world.owner(ASK_FRIDAY_2PM)
    assert world.model.calls == [ASK_FRIDAY_2PM]
    # The model was shown both requests with their versions; it chose one.
    assert {ref.ref for ref in world.model.contexts[0].pending} == {first[:8], second[:8]}
    assert "Offer for Blake Example, Fri Oct 2 at 2:00 PM" in reply.text
    assert "Reply YES to send exactly this" in reply.text and not reply.committed
    # Preparing is not sending: nothing queued, both requests still pending.
    assert not world.store.outbox and world.offer_state() == OfferState.PROPOSED
    assert world.state(first) == world.state(second) == (PENDING, 1)
    assert "Queued the offer to Blake Example" in world.owner("YES").text
    assert world.state(second) == (PENDING, 1)


def test_model_cannot_name_a_stale_or_invented_request(world: World) -> None:
    first, second = two_requests(world)
    world.model.script["stale one"] = call(
        OwnerReplyIntent.PREPARE_COUNTEROFFER, second[:8], 7, FRIDAY, time(14, 0))
    stale = world.owner("stale one").text
    assert "That request changed" in stale and "Nothing was sent" in stale
    world.model.script["invented one"] = call(
        OwnerReplyIntent.PREPARE_COUNTEROFFER, "deadbeef", 1, FRIDAY, time(14, 0))
    assert "wasn't sure which request" in world.owner("invented one").text
    world.model.script["no version"] = call(
        OwnerReplyIntent.PREPARE_COUNTEROFFER, first[:8], None, FRIDAY, time(14, 0))
    assert "That request changed" in world.owner("no version").text
    world.model.script["no time"] = call(
        OwnerReplyIntent.PREPARE_COUNTEROFFER, first[:8], 1, FRIDAY, None)
    assert "wasn't sure which request or time" in world.owner("no time").text
    assert world.store.read_active("pilot", OWNER) is None and not world.store.outbox
    assert world.state(first) == world.state(second) == (PENDING, 1)


def test_low_confidence_or_model_failure_changes_nothing_and_says_so(world: World) -> None:
    request = world.pending()
    world.model.script["maybe later"] = call(
        OwnerReplyIntent.PREPARE_COUNTEROFFER, request[:8], 1, FRIDAY, time(14, 0),
        Confidence.LOW)
    low = world.owner("maybe later").text
    assert "wasn't sure" in low and world.store.read_active("pilot", OWNER) is None
    failed = world.owner("this text the model cannot read").text  # The fake model raises.
    assert "couldn't tell what you meant" in failed
    assert world.model.calls == ["maybe later", "this text the model cannot read"]
    assert world.state(request) == (PENDING, 1)


def test_model_proposed_offer_for_a_past_day_is_refused(world: World) -> None:
    request = world.pending()
    world.offer_for(request, "past", date(2026, 9, 1))
    reply = world.owner("past").text
    assert "I did not prepare an offer" in reply and "Nothing was sent" in reply
    assert world.store.read_active("pilot", OWNER) is None


def test_duplicate_delivery_replays_the_prompt_without_asking_the_model_again(
        world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    first = world.owner(ASK_THURSDAY_2PM, "SM-same")
    again = world.owner(ASK_THURSDAY_2PM, "SM-same")
    assert again.text == first.text and world.model.calls == [ASK_THURSDAY_2PM]
    assert world.owner("YES").text.startswith("Queued the offer")
    # A redelivery of the original instruction after the send sends nothing more.
    assert "already handled" in world.owner(ASK_THURSDAY_2PM, "SM-same").text
    assert len(world.store.outbox) == 1


def test_calendar_question_amid_an_offer_keeps_the_offer_waiting(world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.owner(ASK_THURSDAY_2PM)
    world.model.script["Is anything else on my plate that day?"] = call(
        OwnerReplyIntent.CALENDAR_QUESTION)
    reply = world.owner("Is anything else on my plate that day?")
    assert "Is anything else on my plate that day?" in world.model.calls
    assert "still waiting" in reply.text and "reply YES to send it" in reply.text
    assert world.offer_state() == OfferState.PROPOSED and not world.store.outbox
    assert world.state(request) == (PENDING, 1)
    assert world.owner("YES").text.startswith("Queued the offer")


def test_model_offer_right_after_a_calendar_answer_needs_an_offer_verb(world: World) -> None:
    request = world.pending()
    assert "Thu Oct 1" in world.owner("What does Thursday look like?").text
    world.offer_for(request, "How about 2pm for Avery then")
    reply = world.owner("How about 2pm for Avery then")
    assert world.model.calls == ["How about 2pm for Avery then"]
    assert "asking about the calendar" in reply.text and "Nothing was sent" in reply.text
    assert world.store.read_active("pilot", OWNER) is None


def test_a_revised_model_offer_replaces_the_open_draft(world: World) -> None:
    request = world.pending()
    revision = "Make that 3pm Thursday rather than the morning"
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.offer_for(request, revision, clock=time(15, 0))
    world.owner(ASK_THURSDAY_2PM)
    assert "Thu Oct 1 at 3:00 PM" in world.owner(revision).text
    sent = world.owner("YES").text
    assert "3:00 PM" in sent and len(world.store.outbox) == 1


def test_model_named_decision_among_several_only_asks_and_never_writes(world: World) -> None:
    first, second = two_requests(world)
    text = "Blake's one is fine by me, let's lock it in"
    world.model.script[text] = call(
        OwnerReplyIntent.APPROVE_NAMED_REQUEST, second[:8], world.version(second))
    reply = world.owner(text)
    assert world.model.calls == [text] and not reply.committed
    assert f"Reply APPROVE {second[:8]}" in reply.text and "Nothing has changed" in reply.text
    assert world.state(first) == world.state(second) == (PENDING, 1)
    # A bare YES is still not an approval while two requests wait.
    world.model.script["yes"] = call(
        OwnerReplyIntent.APPROVE_NAMED_REQUEST, second[:8], world.version(second))
    assert not world.owner("yes").committed
    assert world.state(second) == (PENDING, 1)
    # Only the exact command decides, and only that request.
    assert world.owner(f"APPROVE {second[:8]}").committed
    assert world.state(second)[0] == CalendarStatus.CONFIRMED
    assert world.state(first) == (PENDING, 1)


def test_decision_with_an_old_version_or_unknown_reference_is_refused(world: World) -> None:
    first, second = two_requests(world)
    world.model.script["go with Blake's"] = call(
        OwnerReplyIntent.DECLINE_NAMED_REQUEST, second[:8], 5)
    assert "changed" in world.owner("go with Blake's").text
    world.model.script["the other one"] = call(
        OwnerReplyIntent.DECLINE_NAMED_REQUEST, "ffffffff", 1)
    assert "wasn't sure which request" in world.owner("the other one").text
    assert world.state(first) == world.state(second) == (PENDING, 1)


def test_hedged_decision_for_the_single_request_still_only_asks(world: World) -> None:
    request = world.pending()
    text = "I suppose that one can go ahead and get the green light"
    world.model.script[text] = call(
        OwnerReplyIntent.APPROVE_NAMED_REQUEST, request[:8], world.version(request))
    reply = world.owner(text)
    assert world.model.calls == [text] and not reply.committed
    assert "Do you want to approve" in reply.text and "Nothing has changed" in reply.text
    assert world.state(request) == (PENDING, 1)
    world.model.script["fine, that works for me"] = call(
        OwnerReplyIntent.APPROVE_NAMED_REQUEST, request[:8], 9)
    assert "changed" in world.owner("fine, that works for me").text
    assert world.state(request) == (PENDING, 1)


def test_a_model_decision_never_approves_while_an_offer_is_open(world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.owner(ASK_THURSDAY_2PM)
    text = "sounds like a plan, approve the original then"
    world.model.script[text] = call(OwnerReplyIntent.APPROVE_NAMED_REQUEST, request[:8], 1)
    reply = world.owner(text)
    assert text in world.model.calls and not reply.committed
    assert "doesn't approve or decline" in reply.text
    assert world.state(request) == (PENDING, 1) and not world.store.outbox


def test_model_confirm_offer_needs_a_plain_yes_to_send(world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.owner(ASK_THURSDAY_2PM)
    text = "that wording looks great to me, send whatever you drafted"
    world.model.script[text] = call(OwnerReplyIntent.CONFIRM_OFFER)
    reply = world.owner(text)
    assert text in world.model.calls and "Reply YES to send exactly that offer" in reply.text
    assert not world.store.outbox and world.offer_state() == OfferState.PROPOSED


def test_expired_model_prepared_offer_is_not_sent_by_a_late_yes(world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.owner(ASK_THURSDAY_2PM)
    world.clock[0] = NOW + timedelta(minutes=31)
    late = world.owner("YES").text
    assert "expired" in late and "not approved" in late
    assert not world.store.outbox and world.state(request) == (PENDING, 1)


def test_request_decided_after_the_model_prepared_the_offer_blocks_the_send(
        world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.owner(ASK_THURSDAY_2PM)
    assert world.owner(f"DECLINE {request[:8]}").committed
    assert "Queued" not in world.owner("YES").text and not world.store.outbox


def test_show_requests_reads_scoped_details_and_changes_nothing(world: World) -> None:
    first, second = two_requests(world)
    world.model.script["fill me in on what Blake asked for"] = call(
        OwnerReplyIntent.SHOW_REQUESTS, second[:8])
    one = world.owner("fill me in on what Blake asked for")
    assert "Pending owner approval, not confirmed" in one.text
    assert "Blake Example" in one.text and "Avery" not in one.text
    assert f"APPROVE {second[:8]}" in one.text and not one.committed
    world.model.script["what is waiting on me"] = call(OwnerReplyIntent.SHOW_REQUESTS)
    every = world.owner("what is waiting on me").text
    assert "2 requests are pending" in every and first[:8] in every and second[:8] in every
    assert world.state(first) == world.state(second) == (PENDING, 1)


def test_accepted_counteroffer_returns_for_owner_approval(world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.owner(ASK_THURSDAY_2PM)
    assert world.owner("YES").text.startswith("Queued the offer")
    world.clock[0] = NOW + timedelta(minutes=5)
    accepted = world.client("Yes")
    assert accepted.committed and "pending owner approval" in accepted.text
    replacement = next(a for a in world.repository._appointments.values()
                       if a.replaces_appointment_id == request)
    assert (replacement.status, replacement.start_at) == (PENDING, THURSDAY_2PM)
    # The client's acceptance confirmed nothing; the original stays pending until the owner acts.
    assert world.state(request) == (PENDING, 1)
    # A model-named decision on the original is refused while the replacement waits.
    text = "go ahead and take the first booking as it stands"
    world.model.script[text] = call(
        OwnerReplyIntent.APPROVE_NAMED_REQUEST, request[:8], world.version(request))
    assert not world.owner(text).committed
    assert world.state(request) == (PENDING, 1)
    done = world.owner(f"APPROVE {replacement.appointment_id[:8]}")
    assert done.committed and "Both requests are resolved" in done.text
    assert world.state(replacement.appointment_id)[0] == CalendarStatus.CONFIRMED
    assert world.state(request)[0] == CalendarStatus.DECLINED
