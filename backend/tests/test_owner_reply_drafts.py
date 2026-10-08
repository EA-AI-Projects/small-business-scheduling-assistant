"""Owner SMS replies drafted by the model only for read-only answers (#285).

Calendar answers, pending-request summaries, and how-to replies may be reworded; approvals,
declines, offers, and every failure stay fixed backend text. The backend's exact instructions
(paging line, APPROVE/DECLINE, an open offer's YES/NO) follow a draft verbatim, a draft that
changes a date, time, or reference is discarded, and nothing a draft says can act.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import pytest
from test_owner_counteroffer import (
    CLIENT_PHONE,
    NOW,
    OWNER,
    THURSDAY_2PM,
    THURSDAY_9AM,
    Consent,
    Repository,
    profile,
)
from test_owner_model_tools import ASK_THURSDAY_2PM, THURSDAY, call

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_replies import ClientReplyResult
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
)
from scheduling.domain.owner_reply_classification import OwnerReplyContext, OwnerReplyIntent
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.domain.sms_processing import ReceiptProcessor

PENDING = CalendarStatus.PENDING_APPROVAL
ASK_CALENDAR = "What does Thursday look like?"
ASK_PENDING = "what is waiting on me"
ASK_HOW = "how do I do this"
Draft = str | Exception | Callable[[ClientReplyResult], str]


class DraftingModel:
    """Scripted classifier and drafter. An unscripted draft raises, which must fall back."""

    def __init__(self) -> None:
        self.script = {
            ASK_PENDING: call(OwnerReplyIntent.SHOW_REQUESTS),
            ASK_HOW: call(OwnerReplyIntent.HOW_TO),
            "approve it please, thanks": call(OwnerReplyIntent.APPROVE_NAMED_REQUEST),
        }
        self.drafts: dict[str, Draft] = {}
        self.draft_calls: list[tuple[str, MessageContext, ClientReplyResult]] = []
        self.classifier_contexts: list[OwnerReplyContext] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        return MessageProposal("clarify", None, None, None, True)

    def classify_owner_reply(self, body: str, context: OwnerReplyContext):  # type: ignore[no-untyped-def]
        self.classifier_contexts.append(context)
        if body not in self.script:
            raise RuntimeError("model unavailable")
        return self.script[body]

    def draft_owner_reply(self, body: str, context: MessageContext,
                          result: ClientReplyResult) -> str:
        self.draft_calls.append((body, context, result))
        draft = self.drafts.get(body)
        if draft is None:
            raise TimeoutError("synthetic timeout")
        if isinstance(draft, Exception):
            raise draft
        return draft(result) if callable(draft) else draft


class History:
    def __init__(self) -> None:
        self.messages: tuple[HistoryMessage, ...] = ()
        self.reads: list[InboundReceipt] = []

    def read_conversation_history(self, receipt: InboundReceipt,
                                  now: datetime) -> tuple[HistoryMessage, ...]:
        self.reads.append(receipt)
        return self.messages


class OwnerOptedOut(Consent):
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return self.opted_out


@dataclass
class World:
    repository: Repository
    store: InMemoryCounterofferStore
    model: DraftingModel
    history: History
    consent: Consent
    service: ConversationService
    count: int = 0
    clock: list[datetime] = field(default_factory=lambda: [NOW])

    def owner(self, body: str, provider_id: str | None = None,
              sender: str = OWNER, role: SenderRole = SenderRole.OWNER) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "pilot", provider_id or f"SM-o{self.count}", sender, "+14155550000", body,
            self.clock[0], role, None, Keyword.OTHER, True))

    def pending(self, start: datetime = THURSDAY_9AM, client: str = "client-1",
                key: str = "seed") -> str:
        return HoldService(self.repository).create(CreateHold(
            "pilot", client, client, key, start, 120), NOW).hold_id

    def state(self, appointment_id: str) -> tuple[CalendarStatus, int]:
        found = self.repository.read_appointment(appointment_id)
        assert found is not None
        return found.status, found.version


@pytest.fixture
def world() -> World:
    repository = Repository()
    repository.save_profile(profile("client-1", "Avery Sample", CLIENT_PHONE), 0, None)
    repository.save_profile(profile("client-2", "Blake Example", "+15005550007"), 0, None)
    store, consent, model, history = InMemoryCounterofferStore(), OwnerOptedOut(), \
        DraftingModel(), History()
    clock = [NOW]
    holds = HoldService(repository)
    service = ConversationService(
        repository, model, holds, LifecycleService(repository, lambda: clock[0]), consent,
        lambda: clock[0], OWNER,
        counteroffers=CounterofferService(repository, consent, store, OWNER),
        counteroffer_acceptance=CounterofferAcceptance(repository, consent, store, holds),
        history_reader=history)
    return World(repository, store, model, history, consent, service, clock=clock)


def fixed(world: World, body: str) -> str:
    """The existing backend text for ``body``, from a run in which the drafter fails."""
    world.model.drafts.pop(body, None)
    return world.owner(body).text


def test_calendar_answer_is_drafted_from_first_names_only(
        world: World) -> None:
    request = world.pending()
    safe = fixed(world, ASK_CALENDAR)
    assert "Avery Sample" in safe and "pending, ref " in safe
    ref = request[:8]
    draft = f"On Thu Oct 1 Avery has a request, ref {ref}, from 9:00 AM to 11:00 AM."
    world.model.drafts[ASK_CALENDAR] = draft
    reply = world.owner(ASK_CALENDAR)
    assert reply.text == draft and not reply.committed
    result = world.model.draft_calls[-1][2]
    assert result.kind == "owner_calendar" and result.fallback == safe
    # The model sees first names only, never the full name or the stored fallback text.
    assert "Avery" in (result.detail or "") and "Sample" not in (result.detail or "")
    assert world.state(request) == (PENDING, 1)


def test_pending_request_summary_is_drafted_and_the_decision_command_stays_fixed(
        world: World) -> None:
    first = world.pending(key="a")
    second = world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "b")
    safe = fixed(world, ASK_PENDING)
    assert "2 requests are pending" in safe
    world.model.drafts[ASK_PENDING] = (
        f"Two requests wait for you: Avery on Thu Oct 1 at 9:00 AM (ref {first[:8]}) and "
        f"Blake on Fri Oct 2 at 9:00 AM (ref {second[:8]}).")
    reply = world.owner(ASK_PENDING)
    assert reply.text.startswith("Two requests wait for you")
    assert reply.text.endswith(" Nothing has changed. Reply APPROVE or DECLINE with the reference.")
    assert world.state(first) == world.state(second) == (PENDING, 1)


def test_how_to_is_drafted_and_the_exact_instruction_is_appended(world: World) -> None:
    request = world.pending()
    world.model.drafts[ASK_HOW] = (
        f"One request is pending: Avery, Thu Oct 1 at 9:00 AM (ref {request[:8]}). I can answer "
        "calendar questions, draft an offer of another time, and handle approvals.")
    reply = world.owner(ASK_HOW).text
    assert reply.startswith("One request is pending: Avery")
    assert reply.endswith(" To approve or decline a request, reply APPROVE or DECLINE with its "
                          "reference.")


@pytest.mark.parametrize("draft", [
    "On Fri Oct 2 Avery has a request, ref {ref}.",      # Invented day.
    "On Thu Oct 1, 3:00 PM Avery has a request, ref {ref}.",   # Invented time.
    "On Thu Oct 1 Avery has a request, ref deadbeef.",   # Invented reference.
    "On Thu Oct 1 Avery has a request, ref {ref}. I approved it.",  # False claim.
    "On Thu Oct 1 Avery has a request, ref {ref}. Reply YES to approve.",  # Invented prompt.
    "On Thu Oct 1 Avery has a request, ref {ref}. " + "Very long. " * 60,   # Too long.
    "On Thu Oct 1 Avery has a request — ref {ref}ç.",  # Not GSM-7 text.
    "",
])
def test_unsafe_calendar_draft_sends_the_safe_text_and_changes_nothing(
        world: World, draft: str) -> None:
    request = world.pending()
    safe = fixed(world, ASK_CALENDAR)
    world.model.drafts[ASK_CALENDAR] = draft.format(ref=request[:8])
    reply = world.owner(ASK_CALENDAR)
    assert reply.text == safe and len(world.model.draft_calls) == 2
    assert world.state(request) == (PENDING, 1) and not world.store.outbox


def test_timeout_malformed_output_and_missing_history_fall_back(world: World) -> None:
    request = world.pending()
    safe = fixed(world, ASK_CALENDAR)  # Unscripted: the drafter times out.
    assert "Thu Oct 1" in safe
    world.model.drafts[ASK_CALENDAR] = ValueError("malformed")
    assert world.owner(ASK_CALENDAR).text == safe

    def broken_history(*_args: object) -> tuple[HistoryMessage, ...]:
        raise OSError("history unavailable")

    world.history.read_conversation_history = broken_history  # type: ignore[method-assign]
    world.model.drafts[ASK_CALENDAR] = f"Thu Oct 1 9:00 AM ref {request[:8]}"
    assert world.owner(ASK_CALENDAR).text == safe
    assert world.state(request) == (PENDING, 1)


def test_open_offer_reminder_follows_the_draft_verbatim(world: World) -> None:
    request = world.pending()
    world.model.script[ASK_THURSDAY_2PM] = call(
        OwnerReplyIntent.PREPARE_COUNTEROFFER, request[:8], 1, THURSDAY, time(14, 0))
    prepared = world.owner(ASK_THURSDAY_2PM)
    assert "Reply YES to send exactly this" in prepared.text
    assert not world.model.draft_calls  # Offer drafts are fixed text, never drafted.
    safe = fixed(world, ASK_CALENDAR)
    world.model.drafts[ASK_CALENDAR] = (
        f"Thu Oct 1: Avery's request (ref {request[:8]}) holds 9:00 AM to 11:00 AM.")
    reply = world.owner(ASK_CALENDAR).text
    assert reply.startswith("Thu Oct 1: Avery's request")
    result = world.model.draft_calls[-1][2]
    assert result.suffix and reply.endswith(result.suffix)
    assert "reply YES to send it" in reply and " NO " in reply
    # The drafter cannot reword or drop the reminder: a draft that restates it is refused.
    world.model.drafts[ASK_CALENDAR] = (
        f"Thu Oct 1 9:00 AM ref {request[:8]}. Reply YES to send it.")
    assert world.owner(ASK_CALENDAR).text == safe
    assert world.store.read_active("pilot", OWNER) is not None


def test_approvals_declines_offers_and_counteroffers_never_reach_the_drafter(
        world: World) -> None:
    request = world.pending()
    world.model.drafts["APPROVE " + request[:8]] = "Approved already."
    approved = world.owner("APPROVE " + request[:8])
    assert approved.committed and approved.text.endswith("confirmed.")
    other = world.pending(THURSDAY_2PM + timedelta(days=1), "client-2", "other")
    world.model.script["approve it please, thanks"] = call(
        OwnerReplyIntent.APPROVE_NAMED_REQUEST, other[:8], 1)
    asked = world.owner("approve it please, thanks")
    assert asked.text.startswith("Do you want to approve") and world.state(other) == (PENDING, 1)
    world.model.script[ASK_THURSDAY_2PM] = call(
        OwnerReplyIntent.PREPARE_COUNTEROFFER, other[:8], 1, THURSDAY,
        time(14, 0))
    world.model.drafts[ASK_THURSDAY_2PM] = "Sent the offer."
    prepared = world.owner(ASK_THURSDAY_2PM)
    sent = world.owner("YES")
    declined = world.owner("DECLINE " + other[:8])
    assert "Reply YES to send exactly this" in prepared.text
    assert sent.text.startswith("Queued the offer") and declined.text != "Sent the offer."
    assert world.model.draft_calls == []


def test_owner_model_and_drafter_get_the_full_24_hour_transcript(world: World) -> None:
    request = world.pending()
    world.history.messages = (
        HistoryMessage("SM-1", "owner", NOW - timedelta(hours=3), "any news?"),
        HistoryMessage("SM-2", "assistant", NOW - timedelta(hours=3), "No change."),
    )
    world.model.drafts[ASK_PENDING] = (
        f"Avery waits on Thu Oct 1 at 9:00 AM (ref {request[:8]}).")
    world.owner(ASK_PENDING)
    assert world.model.classifier_contexts[-1].history == world.history.messages
    assert world.model.draft_calls[-1][1].history == world.history.messages
    assert world.model.draft_calls[-1][1].actor == SenderRole.OWNER
    # A history failure on the classifier path asks the owner and changes nothing.
    world.history.read_conversation_history = lambda *_a: (_ for _ in ()).throw(  # type: ignore[method-assign]
        OSError("unavailable"))
    failed = world.owner(ASK_PENDING).text
    assert "couldn't tell what you meant" in failed and world.state(request) == (PENDING, 1)


def test_opted_out_or_non_owner_sender_never_reaches_the_model(world: World) -> None:
    world.pending()
    world.consent.opted_out = True
    muted = world.owner(ASK_CALENDAR)
    assert "opted out" in muted.text
    world.consent.opted_out = False
    stranger = world.owner(ASK_CALENDAR, sender="+15005550123")
    assert "no longer the verified owner" in stranger.text
    as_client = world.owner(ASK_CALENDAR, sender=CLIENT_PHONE, role=SenderRole.CLIENT)
    assert "verified client profile" in as_client.text or as_client.reply_result is None
    assert world.model.draft_calls == [] and world.model.classifier_contexts == []


class Store:
    """The receipt store's replay rules: a saved reply or processed receipt is not rerun."""

    def __init__(self, receipt: InboundReceipt) -> None:
        self.receipt = receipt
        self.replies: list[tuple[str, str]] = []
        self.processed: set[str] = set()

    def read_received(self, business_id: str, provider_id: str) -> InboundReceipt | None:
        return self.receipt

    def read_reply_text(self, business_id: str, provider_id: str) -> str | None:
        return next((text for sid, text in self.replies if sid == provider_id), None)

    def has_committed_command(self, receipt: InboundReceipt) -> bool:
        return False

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def claim_processing(self, receipt: InboundReceipt, token: str, now: datetime,
                         lease_until: datetime) -> bool:
        return receipt.provider_id not in self.processed

    def mark_processed(self, receipt: InboundReceipt, token: str, now: datetime) -> None:
        self.processed.add(receipt.provider_id)

    def put_reply(self, receipt: InboundReceipt, text: str, token: str, now: datetime) -> bool:
        self.replies.append((receipt.provider_id, text))
        self.processed.add(receipt.provider_id)
        return True

    def put_committed_reply(self, *args: object) -> bool:
        raise AssertionError("an owner read-only reply is never a committed reply")


def test_duplicate_inbound_delivery_stores_and_drafts_one_reply(world: World) -> None:
    request = world.pending()
    receipt = InboundReceipt("pilot", "SM-dup", OWNER, "+14155550000", ASK_PENDING,
                             NOW, SenderRole.OWNER, None, Keyword.OTHER, True)
    world.model.drafts[ASK_PENDING] = (
        f"Avery waits on Thu Oct 1 at 9:00 AM (ref {request[:8]}).")
    store = Store(receipt)
    processor = ReceiptProcessor(store, world.service, "pilot",  # type: ignore[arg-type]
                                 lambda: NOW)
    first = processor.process("pilot", "SM-dup")
    second = processor.process("pilot", "SM-dup")
    assert first is not None and second is None
    assert [text for _id, text in store.replies] == [first.text]
    assert first.text.startswith("Avery waits on Thu Oct 1")
    assert len(world.model.draft_calls) == 1 and len(world.model.classifier_contexts) == 1
    assert world.state(request) == (PENDING, 1)


def test_paging_footer_is_fixed_text_after_a_drafted_page(world: World) -> None:
    for number, offset in enumerate((4, 5, 6, 7, 8)):
        for hour in (0, 5):
            world.pending(THURSDAY_9AM + timedelta(days=offset, hours=hour),
                          "client-1" if number % 2 else "client-2", f"k{number}{hour}")
    ask = "What does next week look like?"
    safe = fixed(world, ask)
    assert "Reply MORE for the rest." in safe
    world.model.drafts[ask] = lambda result: result.detail or ""
    reply = world.owner(ask).text
    result = world.model.draft_calls[-1][2]
    assert result.suffix.startswith("\nShowing 1-") and result.suffix.endswith("for the rest.")
    assert reply == (result.detail or "") + result.suffix
    assert "Sample" not in reply and "Example" not in reply  # First names only to the model.
    # A draft that invents its own paging instruction is refused.
    world.model.drafts[ask] = lambda result: (result.detail or "") + " Reply MORE."
    assert world.owner(ask).text == safe


def test_eval_cases_describe_only_valid_read_only_owner_results() -> None:
    from evals.scheduling_messages import OWNER_DRAFT_CASES
    from scheduling.domain.client_replies import valid_owner_draft
    assert {case.result.kind for case in OWNER_DRAFT_CASES} == {
        "owner_calendar", "owner_requests", "owner_how_to"}
    for case in OWNER_DRAFT_CASES:
        # The backend's own first-name text is a valid draft of itself; a changed fact is not.
        assert valid_owner_draft(case.result.detail or "", case.result), case.name
        assert not valid_owner_draft((case.result.detail or "") + " At 11:45 PM.", case.result)
