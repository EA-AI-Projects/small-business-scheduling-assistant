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


FRAME = "Here is what I found."


def fixed(world: World, body: str) -> str:
    """The existing backend text for ``body``, from a run in which the drafter fails."""
    world.model.drafts.pop(body, None)
    return world.owner(body).text


def test_calendar_answer_gets_an_opening_and_the_backend_text_unchanged(world: World) -> None:
    request = world.pending()
    safe = fixed(world, ASK_CALENDAR)
    assert "Avery Sample" in safe and f"pending, ref {request[:8]}" in safe
    world.model.drafts[ASK_CALENDAR] = FRAME
    reply = world.owner(ASK_CALENDAR)
    assert reply.text == f"{FRAME}\n{safe}" and not reply.committed
    result = world.model.draft_calls[-1][2]
    assert result.kind == "owner_calendar" and result.fallback == safe
    # The model reads first names only; the owner's text keeps the backend's own names.
    assert "Avery" in (result.detail or "") and "Sample" not in (result.detail or "")
    assert world.state(request) == (PENDING, 1)


def test_pending_summary_and_how_to_keep_commands_and_entries_exactly(world: World) -> None:
    first = world.pending(key="a")
    second = world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "b")
    safe = fixed(world, ASK_PENDING)
    assert "2 requests are pending" in safe
    world.model.drafts[ASK_PENDING] = "Sure, here are your requests."
    reply = world.owner(ASK_PENDING).text
    assert reply == f"Sure, here are your requests.\n{safe}"
    assert reply.count(first[:8]) == 1 and reply.count(second[:8]) == 1
    assert reply.count("Reply APPROVE or DECLINE with the reference.") == 1
    # A how-to whose backend text already exceeds the 480-character cap stays fixed text.
    world.model.drafts[ASK_HOW] = "Happy to help with that."
    assert "Happy" not in world.owner(ASK_HOW).text
    assert world.state(first) == world.state(second) == (PENDING, 1)


def test_how_to_with_one_request_is_framed_with_the_exact_instruction(world: World) -> None:
    world.pending()
    how = fixed(world, ASK_HOW)
    assert len(how) < 480 and "reply APPROVE or DECLINE with its reference." in how
    world.model.drafts[ASK_HOW] = "Happy to help with that."
    assert world.owner(ASK_HOW).text == f"Happy to help with that.\n{how}"


BAD_OPENINGS = [
    "Ana is confirmed for the visit.",                       # False status.
    "Here is what I found. Ana accepted it.",
    "I texted Ana about it.",                                # False sent claim.
    "Here you go. Her request was rejected.",
    "I can also reschedule visits for you.",                 # Invented capability.
    "Here is what is free at ten tomorrow.",                 # Bare hour, relative day.
    "Sunday is free all day.",                               # Lone weekday.
    "Here is the list for 2027.",                            # Wrong year (and digits).
    "Here are 4 confirmed visits.",                          # Changed count.
    "Here are four visits.",
    "Here is a 90 minute visit.",
    "Here is what Avery has.",                               # Client name.
    "Ana, here is what I found.",                            # Unlisted first word.
    "Respond YES to approve.",                               # Prompts and commands.
    'Here you go. Reply "YES" to approve.',
    "Here you go, reply yes.",
    "Here is more. Reply MORE for the rest.",
    "Here you go. APPROVE abcd1234 or DECLINE abcd1234.",
    "Here you go. Approve or decline below.",
    "Here you go.\nSecond line.",                            # Line breaks.
    "Here is what I found. " * 6,                            # Too long.
    "Here is what I found \u2014 enjoy.",                     # Not plain text.
    "Here is the \u00e7 list.",
    "",
    "   ",
]


@pytest.mark.parametrize("draft", BAD_OPENINGS)
def test_any_fact_claim_or_prompt_in_the_opening_sends_the_existing_text(
        world: World, draft: str) -> None:
    request = world.pending()
    safe = fixed(world, ASK_CALENDAR)
    world.model.drafts[ASK_CALENDAR] = draft
    reply = world.owner(ASK_CALENDAR)
    assert reply.text == safe and len(world.model.draft_calls) == 2
    assert world.state(request) == (PENDING, 1) and not world.store.outbox


@pytest.mark.parametrize("draft", [
    "Here is what I found.", "  Sure, here you go.  ", "Certainly! Here you go.",
    "Okay, here's what I see.", "I checked for you."])
def test_plain_framing_is_accepted_and_trimmed(world: World, draft: str) -> None:
    world.pending()
    safe = fixed(world, ASK_CALENDAR)
    world.model.drafts[ASK_CALENDAR] = draft
    assert world.owner(ASK_CALENDAR).text == f"{draft.strip()}\n{safe}"


def test_swapped_entries_cannot_be_written_because_the_model_writes_none(
        world: World) -> None:
    first = world.pending(key="a")
    second = world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "b")
    safe = fixed(world, ASK_PENDING)
    swapped = (f"Avery Sample, Fri Oct 2 at 9:00 AM (ref {second[:8]}); "
               f"Blake Example, Thu Oct 1 at 9:00 AM (ref {first[:8]}).")
    world.model.drafts[ASK_PENDING] = swapped
    assert world.owner(ASK_PENDING).text == safe
    world.model.drafts[ASK_PENDING] = FRAME
    reply = world.owner(ASK_PENDING).text
    assert reply.split("\n", 1)[1] == safe  # Entries and refs are the backend's, in order.


def test_timeout_malformed_output_and_missing_history_fall_back(world: World) -> None:
    world.pending()
    safe = fixed(world, ASK_CALENDAR)  # Unscripted: the drafter times out.
    world.model.drafts[ASK_CALENDAR] = ValueError("malformed")
    assert world.owner(ASK_CALENDAR).text == safe
    world.model.drafts[ASK_CALENDAR] = FRAME
    world.history.read_conversation_history = _throttled  # type: ignore[method-assign]
    assert world.owner(ASK_CALENDAR).text == safe


class ThrottlingError(Exception):
    """Stands in for botocore's ClientError, which is not an OSError or RuntimeError."""


def _throttled(*_args: object) -> tuple[HistoryMessage, ...]:
    raise ThrottlingError("ProvisionedThroughputExceededException")


def test_history_failure_still_answers_and_never_causes_a_write(world: World) -> None:
    request = world.pending()
    world.history.read_conversation_history = _throttled  # type: ignore[method-assign]
    world.model.drafts[ASK_PENDING] = FRAME
    reply = world.owner(ASK_PENDING)
    # The classifier ran without a transcript and the fixed answer was sent.
    assert world.model.classifier_contexts[-1].history == ()
    assert reply.text.startswith("Reply APPROVE or DECLINE with its reference:")
    assert world.state(request) == (PENDING, 1) and not world.store.outbox
    # The same read failure on a decision-shaped text asks or does nothing; it never approves.
    request2 = world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "b")
    world.model.script["approve it please, thanks"] = call(
        OwnerReplyIntent.APPROVE_NAMED_REQUEST, request2[:8], 1)
    world.owner("approve it please, thanks")
    assert world.state(request) == world.state(request2) == (PENDING, 1)


def test_open_offer_reminder_is_kept_verbatim_and_exactly_once(world: World) -> None:
    request = world.pending()
    world.model.script[ASK_THURSDAY_2PM] = call(
        OwnerReplyIntent.PREPARE_COUNTEROFFER, request[:8], 1, THURSDAY, time(14, 0))
    prepared = world.owner(ASK_THURSDAY_2PM)
    assert "Reply YES to send exactly this" in prepared.text
    assert not world.model.draft_calls  # Offer drafts are fixed text, never drafted.
    safe = fixed(world, ASK_CALENDAR)
    assert "reply YES to send it" in safe
    world.model.drafts[ASK_CALENDAR] = FRAME
    reply = world.owner(ASK_CALENDAR).text
    assert reply == f"{FRAME}\n{safe}" and reply.count("reply YES to send it") == 1
    # A model-invented YES prompt is refused, so the owner never sees two YES instructions.
    for draft in ("Respond YES to approve.", 'Reply "YES" to approve.'):
        world.model.drafts[ASK_CALENDAR] = draft
        assert world.owner(ASK_CALENDAR).text == safe
    assert world.store.read_active("pilot", OWNER) is not None and not world.store.outbox


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
    world.pending()
    world.history.messages = (
        HistoryMessage("SM-1", "owner", NOW - timedelta(hours=3), "any news?"),
        HistoryMessage("SM-2", "assistant", NOW - timedelta(hours=3), "No change."),
    )
    world.model.drafts[ASK_PENDING] = FRAME
    world.owner(ASK_PENDING)
    assert world.model.classifier_contexts[-1].history == world.history.messages
    assert world.model.draft_calls[-1][1].history == world.history.messages
    assert world.model.draft_calls[-1][1].actor == SenderRole.OWNER


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
    world.model.drafts[ASK_PENDING] = FRAME
    store = Store(receipt)
    processor = ReceiptProcessor(store, world.service, "pilot",  # type: ignore[arg-type]
                                 lambda: NOW)
    first = processor.process("pilot", "SM-dup")
    second = processor.process("pilot", "SM-dup")
    assert first is not None and second is None
    assert [text for _id, text in store.replies] == [first.text]
    assert first.text.startswith(FRAME) and request[:8] in first.text
    assert len(world.model.draft_calls) == 1 and len(world.model.classifier_contexts) == 1
    assert world.state(request) == (PENDING, 1)


def test_paging_footer_stays_inside_the_backend_text(world: World) -> None:
    for number, offset in enumerate((4, 5, 6, 7, 8)):
        for hour in (0, 5):
            world.pending(THURSDAY_9AM + timedelta(days=offset, hours=hour),
                          "client-1" if number % 2 else "client-2", f"k{number}{hour}")
    ask = "What does next week look like?"
    safe = fixed(world, ask)
    assert safe.endswith("Reply MORE for the rest.")
    world.model.drafts[ask] = FRAME
    reply = world.owner(ask).text
    assert reply == f"{FRAME}\n{safe}" and len(reply) <= 480
    assert reply.count("Reply MORE for the rest.") == 1


def test_eval_cases_describe_only_read_only_owner_results() -> None:
    from evals.scheduling_messages import OWNER_DRAFT_CASES
    from scheduling.domain.owner_replies import DRAFTABLE_OWNER_KINDS, framed
    assert {case.result.kind for case in OWNER_DRAFT_CASES} == DRAFTABLE_OWNER_KINDS
    for case in OWNER_DRAFT_CASES:
        assert framed(FRAME, case.result.fallback) == f"{FRAME}\n{case.result.fallback}"
        assert framed("Ana is confirmed.", case.result.fallback) is None
