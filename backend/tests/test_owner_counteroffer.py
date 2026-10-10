"""Owner counteroffers: prepare, confirm, and send only what the owner reviewed."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.owner_counteroffer import (
    COUNTEROFFER_TEMPLATE,
    CounterofferService,
    InMemoryCounterofferStore,
    OfferState,
)
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

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
    """Scripted owner loop for counteroffer workflows; no live model is called."""

    def __init__(self) -> None:
        self.draft_id: str | None = None
        self.calls: list[str] = []
        self.client_text = EXPECTED_TEXT
        self.version_override: int | None = None
        self.ref_override: str | None = None
        self.date = "2026-10-01"
        self.time = "14:00"

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        return MessageProposal("clarify", None, None, None, True)

    def run_owner_loop(self, body: str, today: date, timezone: str, history: Any,
                       tool: Any) -> str:
        self.calls.append(body)
        listed = tool("list_pending_requests", {})
        pending = listed["requests"]
        open_offer = listed.get("open_offer")
        if body in ("YES", "Yes", "yes"):
            draft_id = open_offer["draft_id"] if open_offer else self.draft_id
            if draft_id is None:
                return "There is no offer to send."
            result = tool("send_counteroffer", {"draft_id": draft_id})
            return ("Queued the offer to Avery Sample; the request is not approved."
                    if result.get("ok") else f"I did not send it: {result['error']}.")
        if body in ("NO", "No", "no", "Cancel offer"):
            draft_id = open_offer["draft_id"] if open_offer else self.draft_id
            if draft_id is None:
                return "There is no offer to cancel."
            result = tool("cancel_counteroffer", {"draft_id": draft_id})
            return "I cancelled the offer. Nothing was sent." if result.get("ok") else (
                f"Nothing was sent: {result['error']}.")
        if body == ASK or body == "Revise to 3pm" or body == "Draft offer":
            if not pending:
                return "There is no pending request."
            target = pending[0]
            clock = "15:00" if body == "Revise to 3pm" else self.time
            text = self.client_text if body != "Revise to 3pm" else (
                "Could Thu Oct 1 at 3:00 PM work instead? Reply YES to request that time.")
            result = tool("draft_counteroffer", {
                "ref": self.ref_override or target["ref"],
                "version": self.version_override if self.version_override is not None
                else target["version"],
                "date": self.date, "time": clock, "client_text": text})
            if not result.get("ok"):
                return f"I did not prepare an offer: {result['error']}. Nothing was sent."
            self.draft_id = result["draft_id"]
            return (f"Offer for {result['client']} at {result['time']}. "
                    f"Text I would send: \"{result['client_text']}\". "
                    "Reply YES to send exactly this, or NO to cancel it.")
        return "Which request do you mean?"


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
    model: NeverModel
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
            "pilot", client, client, key, start, 120), self.clock[0]).hold_id

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


def make_world() -> World:
    repository = Repository()
    repository.save_profile(profile("client-1", "Avery Sample", CLIENT_PHONE), 0, None)
    repository.save_profile(profile("client-2", "Blake Example", "+15005550007"), 0, None)
    store, consent, clock = InMemoryCounterofferStore(), Consent(), [NOW]
    counteroffers = CounterofferService(repository, consent, store, OWNER)
    model = NeverModel()
    service = ConversationService(
        repository, model, HoldService(repository),
        LifecycleService(repository, lambda: clock[0]), consent, lambda: clock[0], OWNER,
        counteroffers=counteroffers)
    messages = Messages()
    sender = TwilioSmsSender(messages, repository, consent,  # type: ignore[arg-type]
                             "pilot", "+14155550000", OWNER, clock=lambda: clock[0],
                             counteroffers=store)
    return World(repository, store, consent, service, model, messages, sender, clock)


@pytest.fixture
def world() -> World:
    return make_world()



def test_draft_then_yes_sends_exact_model_text_once(world: World) -> None:
    request = world.pending()
    world.model.client_text = "Could Thursday at two work? Reply YES to request it."
    prompt = world.say(ASK)
    assert world.model.client_text in prompt.text and not prompt.committed
    offer = world.store.read_active("pilot", OWNER)
    assert offer is not None and offer.state == OfferState.PROPOSED
    assert not world.store.outbox and not world.messages.calls

    sent = world.say("YES")
    assert "Queued the offer" in sent.text and not sent.committed
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
    assert len(world.store.outbox) == 1
    world.deliver()
    assert [(call["to"], call["body"]) for call in world.messages.calls] == [
        (CLIENT_PHONE, world.model.client_text)]
    confirmed = world.store.read_confirmed_for_client("pilot", "client-1")
    assert confirmed is not None and confirmed.proposed_start == THURSDAY_2PM


def test_cancel_discards_draft_without_sending(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    answer = world.say("NO")
    assert "cancelled" in answer.text and not answer.committed
    assert state_of(world) == OfferState.DISCARDED
    assert not world.store.outbox and not world.messages.calls
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)


def test_revised_instruction_replaces_draft_and_only_revised_text_is_sent(world: World) -> None:
    world.pending()
    world.say(ASK)
    old = world.store.read_active("pilot", OWNER)
    assert old is not None
    revised = world.say("Revise to 3pm")
    current = world.store.read_active("pilot", OWNER)
    assert current is not None and current.offer_id != old.offer_id
    assert "3:00 PM" in revised.text and not world.store.outbox
    world.say("YES")
    world.deliver()
    assert len(world.messages.calls) == 1
    assert world.messages.calls[0]["body"] == current.text
    assert "3:00 PM" in current.text


def test_stale_version_and_unknown_reference_do_not_store_draft(world: World) -> None:
    request = world.pending()
    world.model.version_override = 99
    assert "stale_version" in world.say(ASK).text
    assert state_of(world) is None
    world.model.version_override = None
    world.model.ref_override = "deadbeef"
    assert "not_found" in world.say(ASK).text
    assert state_of(world) is None and not world.store.outbox
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)


def test_past_or_unavailable_slot_is_refused_at_draft(world: World) -> None:
    request = world.pending()
    world.model.date = "2026-09-01"
    assert "did not prepare" in world.say(ASK).text
    world.model.date = "2026-10-01"
    world.pending(THURSDAY_2PM, "client-2", "taken")
    assert "slot_unavailable" in world.say(ASK).text
    assert state_of(world) is None and not world.store.outbox
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)


def test_expired_draft_and_newer_request_block_send(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    world.clock[0] = NOW + timedelta(minutes=31)
    assert "expired" in world.say("YES").text
    assert not world.store.outbox and world.status(request)[0] == CalendarStatus.PENDING_APPROVAL

    world.clock[0] = NOW
    world.say(ASK)
    world.clock[0] = NOW + timedelta(minutes=1)
    world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "later")
    assert "newer_request" in world.say("YES").text
    assert not world.store.outbox and world.status(request)[0] == CalendarStatus.PENDING_APPROVAL


def test_lost_consent_blocks_draft_and_send(world: World) -> None:
    request = world.pending()
    world.consent.consented = False
    assert "no_consent" in world.say(ASK).text
    assert state_of(world) is None
    world.consent.consented = True
    world.say(ASK)
    world.consent.opted_out = True
    assert "did not send" in world.say("YES").text
    assert not world.store.outbox and world.status(request)[0] == CalendarStatus.PENDING_APPROVAL


def test_dispatch_rechecks_slot_and_sends_no_client_sms_after_conflict(world: World) -> None:
    from scheduling.domain.outbox import PermanentDeliveryFailure

    world.pending()
    world.say(ASK)
    world.say("YES")
    world.pending(THURSDAY_2PM, "client-2", "taken")
    with pytest.raises(PermanentDeliveryFailure):
        world.deliver()
    assert not world.messages.calls


def test_exact_approval_command_still_decides_original_request(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    answer = world.say(f"APPROVE {request[:8]}")
    assert answer.committed and world.status(request)[0] == CalendarStatus.CONFIRMED
    assert not world.store.outbox


def test_redelivered_draft_and_confirmation_replay_without_sending_twice(
        world: World) -> None:
    world.pending()
    ask = InboundReceipt("pilot", "SM-ask", OWNER, "+14155550000", ASK, NOW,
                         SenderRole.OWNER, None, Keyword.OTHER, True)
    yes = InboundReceipt("pilot", "SM-yes", OWNER, "+14155550000", "YES", NOW,
                         SenderRole.OWNER, None, Keyword.OTHER, True)
    class Reader:
        def __init__(self) -> None:
            self.receipts = {"SM-ask": ask, "SM-yes": yes}
            self.replies: dict[str, str] = {}

        def read_received(self, business_id: str, provider_id: str) -> InboundReceipt | None:
            return self.receipts.get(provider_id)

        def read_reply_text(self, business_id: str, provider_id: str) -> str | None:
            return self.replies.get(provider_id)

        def has_committed_command(self, receipt: InboundReceipt) -> bool:
            return False

        def has_committed_owner_reply_command(self, receipt: InboundReceipt) -> bool:
            return False

        def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
            return False

        def claim_processing(self, receipt: InboundReceipt, token: str, now: datetime,
                             lease_until: datetime) -> bool:
            return receipt.provider_id not in self.replies

        def mark_processed(self, receipt: InboundReceipt, token: str, now: datetime) -> None:
            pass

        def put_reply(self, receipt: InboundReceipt, text: str, token: str,
                      now: datetime) -> bool:
            self.replies[receipt.provider_id] = text
            return True

        def put_committed_reply(self, receipt: InboundReceipt, outbox_id: str,
                                text: str, token: str) -> bool:
            raise AssertionError("An offer prompt does not commit an appointment")

    reader = Reader()
    processor = ReceiptProcessor(reader, world.service, "pilot", lambda: NOW)
    assert processor.process("pilot", "SM-ask") is not None
    assert processor.process("pilot", "SM-ask") is None
    assert processor.process("pilot", "SM-yes") is not None
    assert processor.process("pilot", "SM-yes") is None
    assert world.model.calls == [ASK, "YES"]
    assert len(world.store.outbox) == 1
    world.deliver()
    assert len(world.messages.calls) == 1
