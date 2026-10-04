"""Owner SMS routing across calendar answers, counteroffers, and approvals (#177).

One scripted fake model stands in for the real one; no network call is made. The hostile
mode calls every owner reply an approval of whatever request it can see, so these tests
prove that the backend, not the model, keeps approvals safe.
"""

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
from scheduling.domain.conversation_state import InMemoryConversationStates
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.owner_counteroffer import (
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

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)  # Tue Sep 29, 10:00 AM local.
OWNER = "+14155559999"
WEEK = "What is next week looking like?"
ASK = "That doesn't work for me. Can you offer 2:00 PM instead?"
UNCLEAR = OwnerReplyProposal(OwnerReplyIntent.UNCLEAR, None, Confidence.HIGH)


def local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ZONE).astimezone(UTC)


def proposal(intent: OwnerReplyIntent, request: str | None = None,
             confidence: Confidence = Confidence.HIGH) -> OwnerReplyProposal:
    return OwnerReplyProposal(intent, request[:8] if request else None, confidence)


APPROVE = OwnerReplyIntent.APPROVE_NAMED_REQUEST
DECLINE = OwnerReplyIntent.DECLINE_NAMED_REQUEST


class Model:
    """Scripted by exact owner text. ``hostile`` approves anything it can see."""

    def __init__(self) -> None:
        self.classified: list[tuple[str, OwnerReplyContext]] = []
        self.script: dict[str, OwnerReplyProposal] = {}
        self.hostile = False
        self.error: Exception | None = None

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        raise AssertionError("The owner path never asks the booking interpreter")

    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal:
        self.classified.append((body, context))
        if self.error is not None:
            raise self.error
        if body in self.script:
            return self.script[body]
        if self.hostile and (context.named or context.pending):
            target = context.named or context.pending[0]
            return OwnerReplyProposal(APPROVE, target.ref, Confidence.HIGH)
        return UNCLEAR


class Consent:
    def __init__(self) -> None:
        self.replies: dict[str, str] = {}
        self.outbox: dict[str, tuple[str, datetime | None]] = {}

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_reply_text(self, business_id: str, provider_id: str) -> str | None:
        return self.replies.get(provider_id)

    def read_reply_sent_at(self, business_id: str, provider_id: str) -> datetime | None:
        state, sent_at = self.outbox.get(provider_id, ("MISSING", None))
        return sent_at if state == "SENT" else None

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        client = f"c{phone_e164[-1]}"
        return ConsentEvidence(business_id, client, "Synthetic", phone_e164, NOW, "v1")


class Chat:
    def __init__(self, clients: int = 3) -> None:
        self.store = InMemoryCalendarRepository()
        self.now = NOW
        self.model = Model()
        self.offers = InMemoryCounterofferStore()
        self.count = 0
        names = ("Avery Example", "Blake Sample", "Casey Testperson", "Drew Placeholder")
        for number in range(1, clients + 1):
            self.store.save_profile(ClientProfile(
                "pilot", f"c{number}", names[(number - 1) % 4], f"+1415555010{number}",
                "1 Test Street", HomeSize.MEDIUM, 120, True, 1, NOW, NOW, NOW), 0, None)
        self.consent = Consent()
        self.service = ConversationService(
            self.store, self.model, HoldService(self.store),
            LifecycleService(self.store, lambda: self.now), self.consent, lambda: self.now,
            OWNER, InMemoryConversationStates(),
            counteroffers=CounterofferService(self.store, self.consent, self.offers, OWNER))

    def ask(self, body: str, provider_id: str | None = None, save: bool = True,
            state: str = "SENT") -> ConversationOutcome:
        """Send an owner text; like the SMS processor, save the reply and its outbox state."""
        self.count += 1
        self.now += timedelta(seconds=10)
        provider = provider_id or f"SM-{self.count}"
        outcome = self.service.handle(InboundReceipt(
            "pilot", provider, OWNER, "+14155550000", body, self.now,
            SenderRole.OWNER, None, Keyword.OTHER, True))
        if save and not outcome.committed:
            self.consent.replies[provider] = outcome.text
            self.consent.outbox[provider] = (state, self.now if state == "SENT" else None)
        return outcome

    def hold(self, client: str, start: datetime, key: str, confirm: bool = False) -> str:
        hold_id = HoldService(self.store).create(CreateHold(
            "pilot", client, client, key, start, 120), self.now).hold_id
        if confirm:
            LifecycleService(self.store, lambda: self.now).apply(AppointmentCommand(
                "pilot", hold_id, "owner", ActorRole.OWNER, Action.APPROVE, f"ok-{key}", 1))
        return hold_id

    def status(self, request: str) -> CalendarStatus:
        appointment = self.store.read_appointment(request)
        assert appointment is not None
        return appointment.status

    def snapshot(self) -> tuple[int, tuple[tuple[str, str], ...]]:
        events = self.store.read_calendar("pilot").events
        return (self.store.read_revision("pilot"),
                tuple(sorted((event.event_id, event.status.value) for event in events)))

    def offer_state(self) -> OfferState | None:
        offer = self.offers.read_active("pilot", OWNER)
        return offer.state if offer is not None else None


def one_pending() -> tuple[Chat, str]:
    chat = Chat()
    request = chat.hold("c1", local(10, 1, 9), "a")
    return chat, request


# --- The two reported examples ---------------------------------------------------------


def test_the_calendar_question_is_answered_and_never_gets_the_approval_boilerplate() -> None:
    chat, request = one_pending()
    reply = chat.ask(WEEK)
    assert reply.text.startswith("Mon Oct 5 to Sun Oct 11") or "next week" in reply.text.lower() \
        or "pending" in reply.text
    assert "Reply YES to approve" not in reply.text and not reply.committed
    assert chat.model.classified == []
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_the_counteroffer_request_prepares_an_offer_and_sends_nothing() -> None:
    chat, request = one_pending()
    reply = chat.ask(ASK)
    assert "Text I would send" in reply.text and "Thu Oct 1 at 2:00 PM" in reply.text
    assert "Reply YES to approve" not in reply.text and not reply.committed
    assert chat.offer_state() == OfferState.PROPOSED and not chat.offers.outbox
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
    assert chat.model.classified == []


def test_unrelated_text_gets_a_capability_explanation_not_approval_boilerplate() -> None:
    chat = Chat()
    reply = chat.ask("Please bring up the thermostat settings")
    assert "I can answer calendar questions" in reply.text
    assert "Reply YES to approve" not in reply.text


def test_asking_how_to_approve_is_explained_by_the_model_verdict() -> None:
    chat, request = one_pending()
    chat.model.script["how do I approve these?"] = proposal(OwnerReplyIntent.HOW_TO)
    reply = chat.ask("how do I approve these?")
    assert "reply APPROVE or DECLINE with its reference" in reply.text
    assert "YES / NO" not in reply.text
    assert "I can answer calendar questions" in reply.text
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


# --- The 8-request guard is gone for the owner ------------------------------------------


def test_many_pending_requests_no_longer_block_owner_questions_or_exact_commands() -> None:
    chat = Chat(clients=9)
    days = ((10, 1), (10, 1), (10, 2), (10, 2), (10, 5), (10, 5), (10, 6), (10, 6), (10, 7))
    refs = [chat.hold(f"c{1 + number}", local(*day, 9 + number % 2 * 3), f"k{number}")
            for number, day in enumerate(days)]
    assert not chat.ask(WEEK).text.startswith("Please contact the owner")
    assert "Please contact the owner" not in chat.ask("What is next week looking like?").text
    # An ambiguous plain yes still changes nothing, and tells the owner what is pending.
    chat.model.script["Yes"] = proposal(APPROVE, refs[0])
    before = chat.snapshot()
    reply = chat.ask("Yes")
    assert not reply.committed and "9 requests are pending" in reply.text
    assert chat.snapshot() == before
    assert chat.ask(f"APPROVE {refs[8][:8]}").committed  # The last one needs only its reference.
    assert chat.status(refs[8]) == CalendarStatus.CONFIRMED


# --- Calendar question during an open counteroffer ---------------------------------------


def test_a_calendar_question_during_an_open_offer_is_answered_and_the_offer_is_kept() -> None:
    chat, request = one_pending()
    chat.ask(ASK)
    reply = chat.ask("What do I have on Friday?")
    assert "Fri Oct 2" in reply.text
    assert "still waiting" in reply.text and "reply YES to send it" in reply.text
    assert "cancel" not in reply.text.lower().replace("no to cancel it", "")
    assert chat.offer_state() == OfferState.PROPOSED
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
    sent = chat.ask("YES")  # An unmistakable YES still sends exactly what was reviewed.
    assert sent.text.startswith("Queued the offer") and not sent.committed
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_after_a_calendar_answer_only_a_plain_yes_answers_the_offer() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(ASK)
    chat.ask("What do I have on Friday?")
    for phrase in ("confirmed please", "ok", "yes please", "Go ahead", "sure"):
        reply = chat.ask(phrase)
        assert not reply.committed and "still waiting" in reply.text, phrase
        assert chat.offer_state() == OfferState.PROPOSED and not chat.offers.outbox, phrase
        assert chat.status(request) == CalendarStatus.PENDING_APPROVAL, phrase


def test_model_confirm_works_only_while_the_offer_prompt_is_the_latest_message() -> None:
    chat, request = one_pending()
    chat.model.script["Go ahead"] = proposal(OwnerReplyIntent.CONFIRM_OFFER)
    chat.ask(ASK)
    chat.ask(WEEK)
    assert "still waiting" in chat.ask("Go ahead").text and not chat.offers.outbox
    chat.ask(ASK)  # A fresh prompt is now the latest message.
    sent = chat.ask("Go ahead")
    assert sent.text.startswith("Queued the offer") and len(chat.offers.outbox) == 1
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_model_confirm_needs_sending_words_and_cancel_needs_cancelling_words() -> None:
    chat, request = one_pending()
    chat.ask(ASK)
    chat.model.script["banana"] = proposal(OwnerReplyIntent.CONFIRM_OFFER)
    assert "still waiting" in chat.ask("banana").text and not chat.offers.outbox
    chat.model.script["forget it"] = proposal(OwnerReplyIntent.CANCEL_OFFER)
    cancelled = chat.ask("forget it")
    assert "I cancelled the offer to Avery Example" in cancelled.text
    assert chat.offer_state() == OfferState.DISCARDED and not chat.offers.outbox
    chat.model.script["tasty"] = proposal(OwnerReplyIntent.CANCEL_OFFER)
    chat.ask(ASK)
    assert "still waiting" in chat.ask("tasty").text and chat.offer_state() == OfferState.PROPOSED
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_an_offer_instruction_revises_the_open_offer() -> None:
    chat, _request = one_pending()
    chat.ask(ASK)
    chat.ask(WEEK)
    revised = chat.ask("Actually offer 3:00 PM instead")
    assert "Thu Oct 1 at 3:00 PM" in revised.text and "Text I would send" in revised.text
    assert chat.offer_state() == OfferState.PROPOSED


# --- "what about 3pm instead?" ----------------------------------------------------------


def test_instead_without_an_offer_verb_after_a_calendar_answer_is_clarified() -> None:
    chat, request = one_pending()
    chat.ask(WEEK)
    reply = chat.ask("what about 3pm instead?")
    assert "wasn't sure" in reply.text and "offer 3:00 PM" in reply.text
    assert "Offer 3:00 PM instead for ref" in reply.text
    assert chat.offer_state() is None and not chat.offers.outbox
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
    offered = chat.ask("Offer 3pm instead")  # Now it is unmistakable.
    assert "Text I would send" in offered.text


def test_instead_with_an_offer_prompt_as_the_latest_message_still_revises_it() -> None:
    chat, _request = one_pending()
    chat.ask(ASK)
    assert "Thu Oct 1 at 3:00 PM" in chat.ask("what about 3pm instead?").text


# --- Hook order with several contexts open --------------------------------------------


def test_hook_order_with_an_offer_a_calendar_conversation_and_a_pending_request() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    chat.ask(ASK)
    # 1. An exact command beats every open context, and drops the offer.
    # 2. (shown below) a complete calendar question beats the model.
    # 3. An offer instruction beats the model.
    # 4. Only then does the model read the reply.
    chat.ask("How many pending requests do we have this week?")
    chat.ask("Offer 4pm instead")
    assert chat.model.classified == []
    chat.ask("whatever")
    assert [body for body, _ in chat.model.classified] == ["whatever"]
    approved = chat.ask(f"APPROVE {request[:8]}")
    assert approved.committed and chat.status(request) == CalendarStatus.CONFIRMED
    assert chat.offer_state() is None and not chat.offers.outbox
    assert [body for body, _ in chat.model.classified] == ["whatever"]


def test_an_awaited_clarifying_answer_is_taken_before_the_model_or_the_offer_hooks() -> None:
    chat, _request = one_pending()
    chat.ask("How many bookings do we have next week?")  # Asks which statuses to count.
    reply = chat.ask("pending")
    assert "counted" in reply.text and chat.model.classified == []


# --- Interleaved notifications and stale prompts -------------------------------------------


def test_a_request_arriving_while_an_offer_waits_changes_nothing_about_the_offer() -> None:
    chat, request = one_pending()
    chat.ask(ASK)
    chat.now += timedelta(minutes=1)
    other = chat.hold("c2", local(10, 2, 9), "arrives-later")
    # The new request's notification is now the latest message, so YES is not trusted to mean
    # the old offer: the draft is shown again and nothing is sent.
    again = chat.ask("YES")
    assert "new request arrived" in again.text and "Text I would send" in again.text
    assert not chat.offers.outbox and chat.offer_state() == OfferState.PROPOSED
    sent = chat.ask("YES")
    assert sent.text.startswith("Queued the offer") and not sent.committed
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
    assert chat.status(other) == CalendarStatus.PENDING_APPROVAL


def test_a_request_arriving_after_a_question_names_a_different_request_is_not_approved() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    chat.ask("ok")  # Asks about the one request, by name.
    other = chat.hold("c2", local(10, 2, 9), "arrives-later")
    before = chat.snapshot()
    reply = chat.ask("yes please")
    assert not reply.committed and "2 requests are pending" in reply.text
    assert chat.snapshot() == before
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
    assert chat.status(other) == CalendarStatus.PENDING_APPROVAL


def test_a_stale_offer_prompt_sends_nothing_and_approves_nothing() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(ASK)
    chat.now += timedelta(minutes=31)
    assert "expired" in chat.ask("yes").text
    for phrase in ("ok", "Go ahead", "Yes, approve it"):
        assert not chat.ask(phrase).committed, phrase
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL and not chat.offers.outbox


def test_a_request_decided_elsewhere_makes_the_offer_stale_and_the_reply_ordinary() -> None:
    chat, request = one_pending()
    chat.ask(ASK)
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", request, "owner", ActorRole.OWNER, Action.DECLINE, "dashboard", 1))
    other = chat.hold("c2", local(10, 2, 9), "next")
    chat.model.script["yes"] = proposal(APPROVE, other)
    stale = chat.ask("yes")  # The offer was about a request that no longer waits.
    assert not stale.committed and stale.text.startswith("I did not send it")
    assert chat.status(other) == CalendarStatus.PENDING_APPROVAL and not chat.offers.outbox
    approved = chat.ask("yes")  # The stale offer is gone, so this is an ordinary reply.
    assert approved.committed and chat.status(other) == CalendarStatus.CONFIRMED


# --- Safety regressions from the #174 and #175 reviews --------------------------------------


@pytest.mark.parametrize("phrase", [
    "confirmed please", "yes please", "Yes, approve it", "Go ahead", "ok"])
def test_an_always_approve_model_cannot_approve_after_a_calendar_answer(phrase: str) -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    before = chat.snapshot()
    reply = chat.ask(phrase)  # Nothing was named yet, so even a confident approval asks.
    assert not reply.committed and chat.snapshot() == before
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_after_the_question_was_sent_only_text_that_says_approve_can_approve() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    chat.ask("ok")  # The question names the request; its reply is saved and SENT.
    assert not chat.ask("Go ahead").committed  # No approving word in the text.
    assert not chat.ask("don't approve it").committed
    approved = chat.ask("Yes, approve it")
    assert approved.committed and chat.status(request) == CalendarStatus.CONFIRMED


@pytest.mark.parametrize("phrase", ["ok", "ok thanks", "yes", "Yes, approve it", "Go ahead"])
def test_a_reply_after_a_cancelled_offer_never_approves(phrase: str) -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(ASK)
    chat.ask("No")
    reply = chat.ask(phrase)
    assert not reply.committed and "Nothing was approved" in reply.text
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
    assert chat.ask(f"APPROVE {request[:8]}").committed  # The exact command still works.


def test_x_gone_and_y_arrived_is_not_approved_on_the_old_question() -> None:
    chat, request_x = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    chat.ask("ok")
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", request_x, "owner", ActorRole.OWNER, Action.DECLINE, "gone", 1))
    request_y = chat.hold("c3", local(10, 8, 9), "y")
    before = chat.snapshot()
    stale = chat.ask("yes please")
    assert not stale.committed and chat.snapshot() == before
    assert chat.status(request_y) == CalendarStatus.PENDING_APPROVAL


def test_a_redelivered_asking_message_cannot_approve() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    chat.ask("ok thanks", provider_id="SM-ask")
    again = chat.ask("ok thanks", provider_id="SM-ask")  # A retry of the same message.
    assert not again.committed and chat.status(request) == CalendarStatus.PENDING_APPROVAL


@pytest.mark.parametrize("state", ["PENDING", "FAILED"])
def test_a_question_that_was_not_sent_cannot_be_answered_with_an_approval(state: str) -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    chat.ask("ok", state=state)
    assert not chat.ask("Yes, approve it").committed
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_a_question_whose_reply_was_never_saved_cannot_be_answered_with_an_approval() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    chat.ask("ok", save=False)
    assert not chat.ask("Yes, approve it").committed
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_a_plain_yes_confirming_an_offer_never_approves_the_request() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(ASK)
    assert chat.ask("YES").text.startswith("Queued the offer")
    assert not chat.ask("YES").committed
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
    assert len(chat.offers.outbox) == 1


def test_an_always_approve_model_cannot_approve_while_an_offer_is_open() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(ASK)
    for phrase in ("Yes, approve it", "Approved", "approve", "Go ahead", "OK approve"):
        reply = chat.ask(phrase)
        assert not reply.committed and "still waiting" in reply.text, phrase
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
    assert chat.offer_state() == OfferState.PROPOSED


def test_an_always_approve_model_cannot_decline_or_approve_with_the_wrong_wording() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    before = chat.snapshot()
    for phrase in ("banana", "what is the weather", "don't approve that"):
        assert not chat.ask(phrase).committed, phrase
    chat.model.script["maybe later"] = proposal(DECLINE, request)
    assert not chat.ask("maybe later").committed  # A decline needs declining words.
    assert chat.snapshot() == before


# --- The model is advisory: failures and doubt only ask -------------------------------------


def test_model_errors_and_low_confidence_ask_and_write_nothing_even_with_an_offer_open() -> None:
    chat, request = one_pending()
    chat.ask(ASK)
    before = chat.snapshot()
    for error in (RuntimeError("Model API HTTP 500"), TimeoutError(), ValueError("bad schema")):
        chat.model.error = error
        reply = chat.ask("Yes, approve it")
        assert not reply.committed and "couldn't tell what you meant" in reply.text
        assert "still waiting" in reply.text
    chat.model.error = None
    chat.model.script["Yes, approve it"] = proposal(APPROVE, request, Confidence.MEDIUM)
    assert "wasn't sure" in chat.ask("Yes, approve it").text
    assert chat.snapshot() == before and chat.offer_state() == OfferState.PROPOSED


def test_without_a_classifier_a_plain_reply_asks_and_exact_commands_still_work() -> None:
    chat, request = one_pending()
    chat.service._owner_classifier = None
    reply = chat.ask("Yes")
    assert not reply.committed and "wasn't sure" in reply.text
    assert chat.ask(f"APPROVE {request[:8]}").committed


def test_a_model_context_is_bounded_and_has_no_phone_numbers_or_full_names() -> None:
    chat, _request = one_pending()
    chat.ask(ASK)
    chat.ask("something unrelated")
    _body, context = chat.model.classified[-1]
    assert context.last_kind == "offer_prompt"
    assert context.offer is not None and context.offer.client == "Avery"
    assert [(item.client, item.when) for item in context.pending] == [
        ("Avery", "Thu Oct 1 at 9:00 AM")]
    assert "Example" not in repr(context) and "+1415" not in repr(context)


# --- Review round 1 (#177) ---------------------------------------------------------------


@pytest.mark.parametrize("phrase", [
    "Don't decline it yet", "no, do not decline", "No problem", "No", "cancel", "nope",
    "Yes, decline it", "wait, don't reject that"])
def test_a_model_decline_needs_an_explicit_decline_word_and_no_negator(phrase: str) -> None:
    chat, request = one_pending()
    chat.model.script[phrase] = proposal(DECLINE, request)
    reply = chat.ask(phrase)
    assert not reply.committed and chat.status(request) == CalendarStatus.PENDING_APPROVAL


@pytest.mark.parametrize("phrase", ["decline it", "Please decline", "reject that one"])
def test_a_clear_model_decline_still_declines_the_single_request(phrase: str) -> None:
    chat, request = one_pending()
    chat.model.script[phrase] = proposal(DECLINE, request)
    assert chat.ask(phrase).committed
    assert chat.status(request) == CalendarStatus.DECLINED


def test_a_bare_no_cancels_an_open_offer_but_never_declines_a_request() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(ASK)
    assert "I cancelled the offer" in chat.ask("No").text
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_help_text_never_advertises_no_as_a_decline() -> None:
    chat, _request = one_pending()
    reply = chat.ask("hello there").text
    assert "NO" not in reply and "YES / NO" not in reply


def test_a_clarifying_question_that_outlives_its_conversation_still_gates_approval() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    chat.now += timedelta(minutes=29, seconds=20)
    first = chat.ask("yes please", state="PENDING")  # Asks; its reply is not sent.
    assert not first.committed
    chat.now += timedelta(seconds=40)  # The calendar conversation has now expired.
    assert chat.service._owner_questions.open_context("pilot", OWNER, chat.now) is None
    again = chat.ask("yes please")
    assert not again.committed and chat.status(request) == CalendarStatus.PENDING_APPROVAL
    assert chat.ask("yes please").committed  # The re-ask was sent, so this one answers it.


def test_a_failed_question_followed_by_a_resend_after_half_an_hour_does_not_approve() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.ask(WEEK)
    chat.ask("ok", state="FAILED")
    chat.now += timedelta(minutes=31)
    assert not chat.ask("yes please").committed
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
    assert chat.ask("yes please").committed


def test_question_names_x_then_x_is_declined_and_y_arrives_then_yes_does_not_approve_y() -> None:
    chat, request_x = one_pending()
    chat.model.hostile = True
    chat.model.script["ok"] = UNCLEAR
    chat.ask("ok")  # No calendar conversation: the question still names X.
    LifecycleService(chat.store, lambda: chat.now).apply(AppointmentCommand(
        "pilot", request_x, "owner", ActorRole.OWNER, Action.DECLINE, "gone", 1))
    request_y = chat.hold("c2", local(10, 8, 9), "y")
    reply = chat.ask("yes")
    assert not reply.committed and "Blake Sample" in reply.text
    assert chat.status(request_y) == CalendarStatus.PENDING_APPROVAL
    assert chat.ask("yes").committed  # Now the question named Y and was sent.
    assert chat.status(request_y) == CalendarStatus.CONFIRMED


def test_a_sent_question_that_lapsed_no_longer_gates_a_plain_yes() -> None:
    chat, request = one_pending()
    chat.model.hostile = True
    chat.model.script["ok"] = UNCLEAR
    chat.ask("ok")
    chat.now += timedelta(minutes=31)
    assert chat.ask("yes").committed and chat.status(request) == CalendarStatus.CONFIRMED


def test_which_day_or_week_can_be_answered_with_a_bare_range() -> None:
    chat = Chat()
    chat.hold("c1", local(10, 6, 9), "a")
    chat.model.script["how busy am I?"] = proposal(OwnerReplyIntent.CALENDAR_QUESTION)
    assert chat.ask("how busy am I?").text.startswith("Which day or week")
    answer = chat.ask("next week")
    assert "Mon Oct 5 to Sun Oct 11" in answer.text or "confirmed visits only" in answer.text
    assert "approve" not in answer.text.lower()


def test_how_to_with_a_live_offer_does_not_give_competing_yes_instructions() -> None:
    chat, _request = one_pending()
    chat.ask(ASK)
    chat.model.script["help"] = proposal(OwnerReplyIntent.HOW_TO)
    text = chat.ask("help").text
    assert text.count("reply YES") == 1 and "YES / NO" not in text


def test_a_paged_calendar_answer_with_a_live_offer_uses_the_short_reminder() -> None:
    chat = Chat()
    request = chat.hold("c1", local(10, 1, 9), "a")
    chat.ask(ASK)
    from scheduling.domain.owner_calendar import (
        OwnerAction,
        OwnerCalendarCommand,
        OwnerCalendarService,
    )
    for day in range(5, 10):
        for hour in (8, 11, 14):
            OwnerCalendarService(chat.store, lambda: chat.now).apply(OwnerCalendarCommand(
                "pilot", "owner", f"b{day}{hour}", OwnerAction.CREATE_BLOCK,
                chat.store.read_revision("pilot"), start_at=local(10, day, hour),
                end_at=local(10, day, hour + 1)))
    reply = chat.ask(WEEK).text
    assert "Reply MORE for the rest." in reply and "still waits for YES or NO" in reply
    assert "reply YES to send it or NO" not in reply
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL
