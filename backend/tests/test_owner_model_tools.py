"""Owner transitional calendar and counteroffer intents alongside the #298 tool loop.

The model is scripted; no network or SMS provider is called.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

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
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.owner_counteroffer import (
    CounterofferAcceptance,
    CounterofferService,
    InMemoryCounterofferStore,
    OfferState,
)
from scheduling.domain.owner_transitional import (
    OwnerTransitionContext,
    OwnerTransitionIntent,
    OwnerTransitionProposal,
)
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

THURSDAY = date(2026, 10, 1)
FRIDAY = date(2026, 10, 2)
ASK_FRIDAY_2PM = "Would Blake be free Friday around 2pm rather than the morning?"
ASK_THURSDAY_2PM = "Would Avery be free at 2pm Thursday rather than the morning?"
PENDING = CalendarStatus.PENDING_APPROVAL


class Model:
    def __init__(self) -> None:
        self.transitions: dict[str, OwnerTransitionProposal] = {}
        self.transition_calls: list[str] = []
        self.contexts: list[OwnerTransitionContext] = []
        self.loop_calls: list[str] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        return MessageProposal("clarify", None, None, None, True)

    def classify_owner_transition(self, body: str,
                                  context: OwnerTransitionContext) -> OwnerTransitionProposal:
        self.transition_calls.append(body)
        self.contexts.append(context)
        if body not in self.transitions:
            raise RuntimeError("model unavailable")
        return self.transitions[body]

    def run_owner_loop(self, body: str, today: date, timezone: str, history: Any,
                       tool: Any) -> str:
        self.loop_calls.append(body)
        if body == "Is anything else on my plate that day?":
            result = tool("get_calendar", {"from": "2026-10-01", "to": "2026-10-01",
                                           "statuses": [], "offset": 0})
            return f"Calendar has {result['total']} items. {result.get('open_offer', '')}"
        if body == "Approve the replacement":
            request = tool("list_pending_requests", {})["requests"][-1]
            result = tool("approve_request", {"ref": request["ref"],
                                              "version": request["version"]})
            return "Approved the replacement." if result["ok"] else "Nothing changed."
        return "Which request do you mean?"


def transition(intent: OwnerTransitionIntent, ref: str | None = None,
               version: int | None = None, day: date | None = None,
               clock: time | None = None) -> OwnerTransitionProposal:
    return OwnerTransitionProposal(intent, ref, request_version=version,
                                   offer_date=day, offer_time=clock)


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

    def offer_state(self) -> OfferState | None:
        offer = self.store.read_active("pilot", OWNER)
        return offer.state if offer is not None else None

    def offer_for(self, request: str, text: str, day: date = THURSDAY,
                  clock: time = time(14, 0)) -> None:
        self.model.transitions[text] = transition(
            OwnerTransitionIntent.PREPARE_COUNTEROFFER, request[:8],
            self.state(request)[1], day, clock)


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


def test_model_prepares_named_offer_without_sending(world: World) -> None:
    first = world.pending(key="a")
    second = world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "b")
    world.offer_for(second, ASK_FRIDAY_2PM, FRIDAY)
    answer = world.owner(ASK_FRIDAY_2PM)
    assert world.model.transition_calls == [ASK_FRIDAY_2PM]
    assert {item.ref for item in world.model.contexts[0].pending} == {first[:8], second[:8]}
    assert "Offer for Blake Example, Fri Oct 2 at 2:00 PM" in answer.text
    assert world.offer_state() == OfferState.PROPOSED
    assert not world.store.outbox and not answer.committed
    assert world.state(first) == world.state(second) == (PENDING, 1)


def test_stale_or_invented_model_offer_changes_nothing(world: World) -> None:
    request = world.pending()
    world.model.transitions[ASK_THURSDAY_2PM] = transition(
        OwnerTransitionIntent.PREPARE_COUNTEROFFER, request[:8], 99, THURSDAY, time(14))
    assert "nothing changed" in world.owner(ASK_THURSDAY_2PM).text.lower()
    world.model.transitions[ASK_FRIDAY_2PM] = transition(
        OwnerTransitionIntent.PREPARE_COUNTEROFFER, "deadbeef", 1, THURSDAY, time(14))
    assert not world.owner(ASK_FRIDAY_2PM).committed
    assert world.store.read_active("pilot", OWNER) is None and not world.store.outbox
    assert world.state(request) == (PENDING, 1)


def test_calendar_question_during_offer_preserves_draft(world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.owner(ASK_THURSDAY_2PM)
    world.model.transitions["Is anything else on my plate that day?"] = transition(
        OwnerTransitionIntent.CALENDAR_QUESTION)
    answer = world.owner("Is anything else on my plate that day?")
    assert "still waits" in answer.text and "YES or NO" in answer.text
    assert world.offer_state() == OfferState.PROPOSED
    assert not world.store.outbox and world.state(request) == (PENDING, 1)
    assert world.owner("YES").text.startswith("Queued the offer")


def test_history_failure_during_open_offer_uses_fixed_reply_without_sending(
        world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.owner(ASK_THURSDAY_2PM)

    class BrokenHistory:
        def read_conversation_history(self, receipt: InboundReceipt,
                                      now: datetime) -> tuple[Any, ...]:
            raise RuntimeError("history unavailable")

    world.service._history_reader = BrokenHistory()  # type: ignore[assignment]
    answer = world.owner("YES")
    assert answer.text == "Something went wrong. Please try again."
    assert world.offer_state() == OfferState.PROPOSED
    assert not world.store.outbox and world.state(request) == (PENDING, 1)


def test_draft_prompt_alone_keeps_natural_affirmative_offer_confirmation(
        world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    prompt = world.owner(ASK_THURSDAY_2PM)

    class SentHistory:
        def read_conversation_history(self, receipt: InboundReceipt,
                                      now: datetime) -> tuple[HistoryMessage, ...]:
            return (HistoryMessage("SM-prompt", "assistant", NOW + timedelta(seconds=1),
                                   prompt.text),)

    world.service._history_reader = SentHistory()
    world.clock[0] = NOW + timedelta(seconds=2)
    assert world.owner("Sure").text.startswith("Queued the offer")
    assert world.state(request) == (PENDING, 1)


def test_later_calendar_answer_makes_ambiguous_affirmative_leave_offer_open(
        world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    prompt = world.owner(ASK_THURSDAY_2PM)

    class SentHistory:
        def read_conversation_history(self, receipt: InboundReceipt,
                                      now: datetime) -> tuple[HistoryMessage, ...]:
            return (HistoryMessage("SM-prompt", "assistant", NOW + timedelta(seconds=1),
                                   prompt.text),
                    HistoryMessage("SM-calendar", "assistant", NOW + timedelta(seconds=2),
                                   "Thursday has one pending request."))

    world.service._history_reader = SentHistory()
    world.model.transitions["Sure"] = transition(OwnerTransitionIntent.UNCLEAR)
    world.clock[0] = NOW + timedelta(seconds=3)
    answer = world.owner("Sure")
    assert "still waiting" in answer.text
    assert world.offer_state() == OfferState.PROPOSED
    assert not world.store.outbox and world.state(request) == (PENDING, 1)


def test_revised_model_offer_replaces_open_draft(world: World) -> None:
    request = world.pending()
    revised = "Make that 3pm Thursday rather than the morning"
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.offer_for(request, revised, clock=time(15))
    world.owner(ASK_THURSDAY_2PM)
    assert "3:00 PM" in world.owner(revised).text
    assert "3:00 PM" in world.owner("YES").text
    assert len(world.store.outbox) == 1


def test_client_acceptance_returns_replacement_to_owner_tool_loop(world: World) -> None:
    request = world.pending()
    world.offer_for(request, ASK_THURSDAY_2PM)
    world.owner(ASK_THURSDAY_2PM)
    assert world.owner("YES").text.startswith("Queued the offer")
    world.clock[0] = NOW + timedelta(minutes=5)
    assert world.client("Yes").committed
    replacement = next(a for a in world.repository._appointments.values()
                       if a.replaces_appointment_id == request)
    assert (replacement.status, replacement.start_at) == (PENDING, THURSDAY_2PM)
    answer = world.owner("Approve the replacement")
    assert answer.committed and answer.text == "Approved the replacement."
    assert world.model.loop_calls == ["Approve the replacement"]
    assert world.state(replacement.appointment_id)[0] == CalendarStatus.CONFIRMED
    assert world.state(request)[0] == CalendarStatus.DECLINED
