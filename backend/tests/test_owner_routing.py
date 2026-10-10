"""Owner SMS routing across calendar answers, offers, and the #298 decision tools.

The in-memory store and scripted model make these local workflow checks.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.conversation import ConversationService, MessageContext, MessageProposal
from scheduling.domain.conversation_state import InMemoryConversationStates
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.owner_counteroffer import (
    CounterofferService,
    InMemoryCounterofferStore,
    OfferState,
)
from scheduling.domain.owner_transitional import (
    OwnerTransitionContext,
    OwnerTransitionIntent,
    OwnerTransitionProposal,
)
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)
OWNER = "+14155559999"
ASK = "That doesn't work for me. Can you offer 2:00 PM instead?"


class Model:
    def __init__(self) -> None:
        self.loop_calls: list[str] = []
        self.transition_calls: list[str] = []
        self.transitions: dict[str, OwnerTransitionProposal] = {}

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        raise AssertionError("Owner path must not use the client proposer")

    def classify_owner_transition(self, body: str,
                                  context: OwnerTransitionContext) -> OwnerTransitionProposal:
        self.transition_calls.append(body)
        return self.transitions.get(body, OwnerTransitionProposal(OwnerTransitionIntent.UNCLEAR))

    def run_owner_loop(self, body: str, today: date, timezone: str, history: Any,
                       tool: Any) -> str:
        self.loop_calls.append(body)
        if body.startswith("What "):
            first, last = (("2026-10-05", "2026-10-11")
                           if "next week" in body else
                           ("2026-10-02", "2026-10-02") if "Friday" in body else
                           ("2026-10-01", "2026-10-01"))
            result = tool("get_calendar", {"from": first, "to": last,
                                           "statuses": [], "offset": 0})
            text = f"Calendar {first} to {last}: {result['total']} items."
            if result.get("open_offer"):
                text += f" {result['open_offer']}"
            return text
        if body == "Approve Avery, please":
            target = tool("list_pending_requests", {})["requests"][0]
            result = tool("approve_request", {"ref": target["ref"],
                                              "version": target["version"]})
            return "Approved Avery's request." if result["ok"] else "Nothing changed."
        return "Which request do you mean?"


class Consent:
    def __init__(self) -> None:
        self.replies: dict[str, str] = {}
        self.sent: dict[str, datetime] = {}

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_reply_text(self, business_id: str, provider_id: str) -> str | None:
        return self.replies.get(provider_id)

    def read_reply_sent_at(self, business_id: str, provider_id: str) -> datetime | None:
        return self.sent.get(provider_id)

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return ConsentEvidence(business_id, "c1", "Synthetic", phone_e164, NOW, "v1")


class Chat:
    def __init__(self) -> None:
        self.store = InMemoryCalendarRepository()
        self.model = Model()
        self.consent = Consent()
        self.offers = InMemoryCounterofferStore()
        self.now = NOW
        self.count = 0
        for number, name in ((1, "Avery Example"), (2, "Blake Sample")):
            self.store.save_profile(ClientProfile(
                "pilot", f"c{number}", name, f"+1415555010{number}",
                "1 Test Street", HomeSize.MEDIUM, 120, True, 1, NOW, NOW, NOW), 0, None)
        self.service = ConversationService(
            self.store, self.model, HoldService(self.store),
            LifecycleService(self.store, lambda: self.now), self.consent, lambda: self.now,
            OWNER, InMemoryConversationStates(),
            counteroffers=CounterofferService(self.store, self.consent, self.offers, OWNER))

    def hold(self, client: str = "c1", day: int = 1, key: str = "a") -> str:
        start = datetime(2026, 10, day, 9, tzinfo=ZONE).astimezone(UTC)
        return HoldService(self.store).create(CreateHold(
            "pilot", client, client, key, start, 120), self.now).hold_id

    def ask(self, body: str):  # type: ignore[no-untyped-def]
        self.count += 1
        self.now += timedelta(seconds=10)
        provider = f"SM-{self.count}"
        result = self.service.handle(InboundReceipt(
            "pilot", provider, OWNER, "+14155550000", body, self.now,
            SenderRole.OWNER, None, Keyword.OTHER, True))
        if not result.committed:
            self.consent.replies[provider] = result.text
            self.consent.sent[provider] = self.now
        return result

    def status(self, request: str) -> CalendarStatus:
        found = self.store.read_appointment(request)
        assert found is not None
        return found.status

    def offer_state(self) -> OfferState | None:
        offer = self.offers.read_active("pilot", OWNER)
        return offer.state if offer is not None else None


def test_calendar_question_uses_read_only_tool_loop() -> None:
    chat = Chat()
    request = chat.hold()
    answer = chat.ask("What is next week looking like?")
    assert "2026-10-05 to 2026-10-11" in answer.text
    assert chat.model.loop_calls == ["What is next week looking like?"]
    assert not answer.committed
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_counteroffer_instruction_prepares_without_sending_or_model_call() -> None:
    chat = Chat()
    request = chat.hold()
    answer = chat.ask(ASK)
    assert "Text I would send" in answer.text and "2:00 PM" in answer.text
    assert chat.offer_state() == OfferState.PROPOSED
    assert not chat.offers.outbox and chat.model.loop_calls == []
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_plain_yes_sends_reviewed_offer_but_does_not_approve_request() -> None:
    chat = Chat()
    request = chat.hold()
    chat.ask(ASK)
    answer = chat.ask("YES")
    assert answer.text.startswith("Queued the offer") and not answer.committed
    assert len(chat.offers.outbox) == 1 and chat.model.loop_calls == []
    assert chat.status(request) == CalendarStatus.PENDING_APPROVAL


def test_calendar_answer_during_offer_keeps_offer_waiting() -> None:
    chat = Chat()
    chat.hold()
    chat.ask(ASK)
    chat.model.transitions["What do I have on Friday?"] = OwnerTransitionProposal(
        OwnerTransitionIntent.CALENDAR_QUESTION)
    answer = chat.ask("What do I have on Friday?")
    assert "2026-10-02" in answer.text and "still waits for YES or NO" in answer.text
    assert chat.offer_state() == OfferState.PROPOSED
    assert not chat.offers.outbox and chat.model.loop_calls == ["What do I have on Friday?"]


def test_exact_command_bypasses_model_even_after_calendar_and_offer() -> None:
    chat = Chat()
    request = chat.hold()
    chat.ask("What do I have on Thursday?")
    chat.ask(ASK)
    answer = chat.ask(f"APPROVE {request[:8]}")
    assert answer.committed and chat.model.loop_calls == ["What do I have on Thursday?"]
    assert chat.status(request) == CalendarStatus.CONFIRMED
    assert not chat.offers.outbox


def test_owner_decision_uses_tool_loop_and_model_final_text() -> None:
    chat = Chat()
    request = chat.hold()
    answer = chat.ask("Approve Avery, please")
    assert answer.committed and answer.text == "Approved Avery's request."
    assert chat.model.loop_calls == ["Approve Avery, please"]
    assert chat.status(request) == CalendarStatus.CONFIRMED


def test_ambiguous_owner_reply_remains_read_only() -> None:
    chat = Chat()
    first, second = chat.hold(), chat.hold("c2", 2, "b")
    answer = chat.ask("Sure")
    assert answer.text == "Which request do you mean?" and not answer.committed
    assert all(chat.status(ref) == CalendarStatus.PENDING_APPROVAL
               for ref in (first, second))
