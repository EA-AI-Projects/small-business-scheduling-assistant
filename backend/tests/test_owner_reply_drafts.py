"""Model-authored calendar SMS remains a read-only owner path in #298.

These checks use an in-memory calendar and scripted model; no live model or SMS is called.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from test_owner_counteroffer import (
    CLIENT_PHONE,
    NOW,
    OWNER,
    THURSDAY_9AM,
    Consent,
    Repository,
    profile,
)

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_replies import ClientReplyResult
from scheduling.domain.conversation import (
    OWNER_FAILURE_TEXT,
    ConversationOutcome,
    ConversationService,
    MessageContext,
    MessageProposal,
)
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

ASK_CALENDAR = "What does Thursday look like?"


class Model:
    def __init__(self) -> None:
        self.drafts: dict[str, str | Exception] = {}
        self.draft_calls: list[tuple[str, MessageContext, ClientReplyResult]] = []
        self.loop_calls: list[str] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        raise AssertionError("Owner text must not use client proposal")

    def run_owner_loop(self, body: str, today: Any, timezone: str, history: Any,
                       tool: Any) -> str:
        self.loop_calls.append(body)
        return "Which request do you mean?"

    def draft_owner_reply(self, body: str, context: MessageContext,
                          result: ClientReplyResult) -> str:
        self.draft_calls.append((body, context, result))
        draft = self.drafts.get(body, TimeoutError("synthetic timeout"))
        if isinstance(draft, Exception):
            raise draft
        return draft


class History:
    def __init__(self) -> None:
        self.messages: tuple[HistoryMessage, ...] = ()

    def read_conversation_history(self, receipt: InboundReceipt,
                                  now: datetime) -> tuple[HistoryMessage, ...]:
        return self.messages


class OwnerOptedOut(Consent):
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return self.opted_out


@dataclass
class World:
    repository: Repository
    model: Model
    history: History
    consent: OwnerOptedOut
    service: ConversationService
    count: int = 0
    clock: list[datetime] = field(default_factory=lambda: [NOW])

    def owner(self, body: str, sender: str = OWNER,
              role: SenderRole = SenderRole.OWNER) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "pilot", f"SM-o{self.count}", sender, "+14155550000", body,
            self.clock[0], role, None, Keyword.OTHER, True))

    def pending(self, start: datetime = THURSDAY_9AM, client: str = "client-1",
                key: str = "seed") -> str:
        return HoldService(self.repository).create(CreateHold(
            "pilot", client, client, key, start, 120), NOW).hold_id

    def state(self, appointment_id: str) -> CalendarStatus:
        found = self.repository.read_appointment(appointment_id)
        assert found is not None
        return found.status


@pytest.fixture
def world() -> World:
    repository = Repository()
    repository.save_profile(profile("client-1", "Avery Sample", CLIENT_PHONE), 0, None)
    repository.save_profile(profile("client-2", "Blake Example", "+15005550007"), 0, None)
    consent, model, history, clock = OwnerOptedOut(), Model(), History(), [NOW]
    service = ConversationService(
        repository, model, HoldService(repository),
        LifecycleService(repository, lambda: clock[0]), consent, lambda: clock[0], OWNER,
        history_reader=history)
    return World(repository, model, history, consent, service, clock=clock)


def test_calendar_answer_uses_model_draft_but_changes_no_request(world: World) -> None:
    request = world.pending()
    draft = f"Avery has a request on Thu Oct 1 from 9:00 AM to 11:00 AM (ref {request[:8]})."
    world.model.drafts[ASK_CALENDAR] = draft
    answer = world.owner(ASK_CALENDAR)
    assert answer.text == draft and not answer.committed
    result = world.model.draft_calls[-1][2]
    assert result.kind == "owner_calendar"
    assert "Avery Sample" in result.fallback and request[:8] in result.fallback
    assert "Avery" in (result.detail or "") and "Sample" not in (result.detail or "")
    assert world.state(request) == CalendarStatus.PENDING_APPROVAL
    assert world.model.loop_calls == []


@pytest.mark.parametrize("draft", [
    "For Fri Oct 2, Avery has a request ref {ref}.",
    "For Thu Oct 1 at 3:00 PM, Avery has a request ref {ref}.",
    "For Thu Oct 1, Avery has a request ref deadbeef.",
])
def test_syntactically_valid_calendar_draft_is_sent_as_written(
        world: World, draft: str) -> None:
    request = world.pending()
    rendered = draft.format(ref=request[:8])
    world.model.drafts[ASK_CALENDAR] = rendered
    answer = world.owner(ASK_CALENDAR)
    assert answer.text == rendered and not answer.committed
    assert len(world.model.draft_calls) == 1
    assert world.state(request) == CalendarStatus.PENDING_APPROVAL


@pytest.mark.parametrize("bad", [
    "For Thu Oct 1, Avery has a request ref {ref}. " + "Very long. " * 60,
    "For Thu Oct 1, Avery has a request ref {ref} 😀.",
    "",
])
def test_undeliverable_calendar_draft_retries_then_uses_fixed_failure(
        world: World, bad: str) -> None:
    request = world.pending()
    world.model.drafts[ASK_CALENDAR] = bad.format(ref=request[:8])
    answer = world.owner(ASK_CALENDAR)
    assert answer.text == OWNER_FAILURE_TEXT and not answer.committed
    assert len(world.model.draft_calls) == 2
    assert world.state(request) == CalendarStatus.PENDING_APPROVAL


def test_draft_error_uses_fixed_failure(world: World) -> None:
    request = world.pending()
    world.model.drafts[ASK_CALENDAR] = ValueError("malformed")
    assert world.owner(ASK_CALENDAR).text == OWNER_FAILURE_TEXT
    assert world.state(request) == CalendarStatus.PENDING_APPROVAL


def test_calendar_drafter_receives_owner_history(world: World) -> None:
    world.pending()
    world.history.messages = (
        HistoryMessage("SM-1", "owner", NOW - timedelta(hours=3), "any news?"),
        HistoryMessage("SM-2", "assistant", NOW - timedelta(hours=3), "No change."),
    )
    world.owner(ASK_CALENDAR)
    _, context, _ = world.model.draft_calls[-1]
    assert context.history == world.history.messages
    assert context.actor == SenderRole.OWNER


def test_opted_out_and_non_owner_sender_do_not_reach_owner_model(world: World) -> None:
    world.pending()
    world.consent.opted_out = True
    assert "opted out" in world.owner(ASK_CALENDAR).text
    world.consent.opted_out = False
    assert "no longer the verified owner" in world.owner(
        ASK_CALENDAR, sender="+15005550123").text
    world.owner(ASK_CALENDAR, sender=CLIENT_PHONE, role=SenderRole.CLIENT)
    assert world.model.draft_calls == [] and world.model.loop_calls == []
