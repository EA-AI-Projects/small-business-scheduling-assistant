"""Validated scheduling commands from a trusted, verified SMS receipt.

The interpreter proposes intent only. This layer checks actor, target, date,
current state, and policy before calling the existing transactional services.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import (
    AvailabilityPolicy,
    AvailabilityService,
    InvalidDuration,
    InvalidPolicy,
    available_starts,
)
from scheduling.domain.calendar import CalendarSnapshot, CalendarStatus
from scheduling.domain.client_calendar_questions import (
    BOTH,
    MAX_RANGE_DAYS,
    ClientQuestion,
    View,
    is_action_like,
    mentions_status,
    range_from_proposal,
    statuses_from_proposal,
)
from scheduling.domain.client_calendar_questions import answer as answer_client_question
from scheduling.domain.client_calendar_questions import compact_list as compact_client_list
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.client_replies import (
    DRAFTABLE_OWNER_KINDS,
    ClientReplyFact,
    ClientReplyResult,
    facts_from_text,
    valid_draft,
    valid_owner_draft,
)
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.conversation_state import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    PROMPT_LIFETIME,
    ConversationState,
    ConversationStateStore,
    InMemoryConversationStates,
    PromptKind,
    Selection,
    clock_text,
    day_text,
    is_affirmative,
    is_negative,
    match_selection,
    spread,
    when_text,
)
from scheduling.domain.holds import (
    CreateHold,
    HoldService,
    IdempotencyKeyReused,
    InvalidReplacement,
    ReplacementPending,
    SlotConflict,
    TooManyConflicts,
)
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    HoldExpired,
    InvalidTransition,
    LifecycleService,
    StaleVersion,
)
from scheduling.domain.owner_calendar_questions import (
    CLARIFICATION_LIFETIME,
    CalendarAnswer,
    OwnerCalendarQuestions,
    ReplyLookup,
    last_answered_at,
)
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyClassifier,
    OwnerReplyContext,
    OwnerReplyIntent,
    PendingRef,
    may_be_approval,
    supports_approval,
    supports_decline,
    supports_offer_cancel,
    supports_offer_send,
)
from scheduling.domain.sms_ingress import (
    ConsentEvidence,
    InboundReceipt,
    Keyword,
    SenderRole,
    normalize_phone,
)

if TYPE_CHECKING:
    from scheduling.domain.owner_counteroffer import (
        CounterofferAcceptance,
        CounterofferService,
        OfferView,
    )

MAX_CONTEXT_APPOINTMENTS = 8
MAX_MESSAGE_LENGTH = 1000
EXPLICIT_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
EXPLICIT_START = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?!\d)")
REFERENCE = r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8})"
DATE_AND_OPTIONAL_TIME = r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?"
EXACT_COMMAND = re.compile(
    r"^(approve|decline|cancel)\s+(?:(?:request|appointment)\s+)?"
    rf"({REFERENCE})[.!]?$", re.IGNORECASE)
BOOK_COMMAND = re.compile(rf"^book\s+({DATE_AND_OPTIONAL_TIME})[.!]?$", re.IGNORECASE)
CANCEL_WORDS = frozenset({"cancel", "it", "please", "visit", "appointment", "request"})
RESCHEDULE_COMMAND = re.compile(
    rf"^reschedule\s+({REFERENCE})\s+to\s+({DATE_AND_OPTIONAL_TIME})[.!]?$",
    re.IGNORECASE,
)


CAPABILITIES = (
    "I can answer calendar questions (for example: What is next week looking like?), "
    "prepare an offer of another time for a pending request (for example: Offer 2:00 PM "
    "instead), and approve or decline a request (APPROVE or DECLINE with its reference).")


@dataclass(frozen=True)
class MessageContext:
    actor: SenderRole
    today: date
    timezone: str
    references: tuple[str, ...]
    horizon_days: int = 14
    # The client's last calendar answer, still open for follow-ups, e.g.
    # "2026-09-28 to 2026-10-04; statuses: confirmed; view: list", or None.
    calendar_answer: str | None = None
    history: tuple[HistoryMessage, ...] = ()
    prompt_kind: str = "none"


@dataclass(frozen=True)
class MessageProposal:
    """A model or exact-command interpretation. It never selects a write.

    ``date_from``/``date_to`` and ``time_from``/``time_to`` are the requested
    local day range and time window (ISO text); ``target_date`` is the local
    day of an existing visit the sender wants to cancel or move.
    """

    intent: str
    request_reference: str | None
    date_text: str | None
    owner_decision: str | None
    needs_clarification: bool
    date_from: str | None = None
    date_to: str | None = None
    time_from: str | None = None
    time_to: str | None = None
    target_date: str | None = None
    # A calendar question (#241): "confirmed" and/or "pending", and "list" or "count";
    # None when the text does not say (a follow-up then keeps the last answer's).
    statuses: tuple[str, ...] | None = None
    view: str | None = None
    # "dates" (date_from/date_to), "all_upcoming", or "keep" the open answer's range.
    range_scope: str | None = None


@dataclass(frozen=True)
class ConversationOutcome:
    text: str
    committed: bool = False
    appointment_id: str | None = None
    client_outbox_id: str | None = None
    reply_result: ClientReplyResult | None = field(default=None, compare=False)


class MessageInterpreter(Protocol):
    def propose(self, body: str, context: MessageContext) -> MessageProposal: ...


class ConversationHistoryReader(Protocol):
    def read_conversation_history(self, receipt: InboundReceipt,
                                  now: datetime) -> tuple[HistoryMessage, ...]: ...


@runtime_checkable
class ClientReplyDrafter(Protocol):
    def draft_client_reply(self, body: str, context: MessageContext,
                           result: ClientReplyResult) -> str: ...


@runtime_checkable
class OwnerReplyDrafter(Protocol):
    def draft_owner_reply(self, body: str, context: MessageContext,
                          result: ClientReplyResult) -> str: ...


class ConversationRepository(Protocol):
    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...
    def read_pending_requests(self, business_id: str, now: datetime) -> tuple[Appointment, ...]: ...
    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...
    def read_appointment(self, appointment_id: str) -> Appointment | None: ...


class ConsentLookup(Protocol):
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool: ...
    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None: ...


class ConversationService:
    def __init__(self, repository: ConversationRepository, interpreter: MessageInterpreter,
                 holds: HoldService, lifecycle: LifecycleService,
                 consent: ConsentLookup, clock: Callable[[], datetime],
                 owner_number: str, states: ConversationStateStore | None = None,
                 owner_questions: OwnerCalendarQuestions | None = None,
                 owner_reply_classifier: OwnerReplyClassifier | None = None,
                 reply_lookup: ReplyLookup | None = None,
                 counteroffers: "CounterofferService | None" = None,
                 counteroffer_acceptance: "CounterofferAcceptance | None" = None,
                 history_reader: ConversationHistoryReader | None = None) -> None:
        self._repository = repository
        self._interpreter = interpreter
        self._holds = holds
        self._lifecycle = lifecycle
        self._consent = consent
        self._clock = clock
        self._owner_number = normalize_phone(owner_number)
        self._availability = AvailabilityService(repository)
        self._states = states if states is not None else InMemoryConversationStates()
        self._owner_questions = (owner_questions if owner_questions is not None
                                 else OwnerCalendarQuestions(repository))
        self._owner_classifier = (
            owner_reply_classifier if owner_reply_classifier is not None
            else interpreter if isinstance(interpreter, OwnerReplyClassifier) else None)
        # The SMS store that holds saved replies; without it no request ever counts as named.
        self._reply_lookup = (reply_lookup if reply_lookup is not None
                              else consent if isinstance(consent, ReplyLookup) else None)
        self._counteroffers = counteroffers
        self._acceptance = counteroffer_acceptance
        self._history_reader = history_reader

    def handle(self, receipt: InboundReceipt) -> ConversationOutcome:
        outcome = self._handle(receipt)
        if (not receipt.authorized_for_commands or receipt.body is None
                or receipt.keyword != Keyword.OTHER
                or (outcome.committed and outcome.client_outbox_id is None)
                or outcome.reply_result is None):
            return outcome
        if receipt.role == SenderRole.OWNER:
            return self._draft_owner(receipt, outcome)
        if (receipt.role != SenderRole.CLIENT
                or not isinstance(self._interpreter, ClientReplyDrafter)):
            return outcome
        try:
            profile = (self._repository.read_profile(receipt.business_id, receipt.client_id)
                       if receipt.client_id is not None else None)
            consent = self._consent.read_consent(receipt.business_id, receipt.sender)
            if (profile is None or not profile.active or profile.phone_verified_at is None
                    or profile.phone_e164 != receipt.sender or consent is None
                    or consent.client_id != profile.client_id
                    or self._consent.is_opted_out(receipt.business_id, receipt.sender)):
                return outcome
            now = self._clock()
            policy = self._repository.read_policy(receipt.business_id)
            context = MessageContext(
                receipt.role, now.astimezone(ZoneInfo(policy.timezone)).date(),
                policy.timezone, (), policy.booking_horizon_days,
                history=(self._history_reader.read_conversation_history(receipt, now)
                         if self._history_reader is not None else ()),
            )
            result = outcome.reply_result
            draft = self._interpreter.draft_client_reply(receipt.body, context, result)
            if valid_draft(draft, result):
                return replace(outcome, text=draft)
        except Exception:  # noqa: BLE001 - the draft is optional; any failure keeps the safe text
            return outcome
        return outcome

    def _draft_owner(self, receipt: InboundReceipt, outcome: ConversationOutcome
                     ) -> ConversationOutcome:
        """Reword an owner calendar answer, request summary, or how-to (#285).

        Only read-only results carry a draftable kind; every approval, decline, offer, and
        counteroffer text stays fixed. The model sees the owner's 24-hour thread, never writes,
        and its text is used only if it keeps exactly the backend's dates, times, and
        references. The backend's fixed suffix (commands, paging footer, open-offer reminder)
        follows it verbatim. Any failure or mismatch sends the existing fallback text.
        """
        result = outcome.reply_result
        if (result is None or outcome.committed or receipt.body is None
                or receipt.sender != self._owner_number
                or not isinstance(self._interpreter, OwnerReplyDrafter)):
            return outcome
        try:
            if (result.kind not in DRAFTABLE_OWNER_KINDS
                    or self._consent.is_opted_out(receipt.business_id, receipt.sender)):
                return outcome
            now = self._clock()
            policy = self._repository.read_policy(receipt.business_id)
            context = MessageContext(
                receipt.role, now.astimezone(ZoneInfo(policy.timezone)).date(),
                policy.timezone, (), policy.booking_horizon_days,
                history=(self._history_reader.read_conversation_history(receipt, now)
                         if self._history_reader is not None else ()))
            draft = self._interpreter.draft_owner_reply(receipt.body, context, result).strip()
            if valid_owner_draft(draft, result):
                return replace(outcome, text=draft + result.suffix)
        except Exception:  # noqa: BLE001 - the draft is optional; any failure keeps the safe text
            return outcome
        return outcome

    @staticmethod
    def _owner_result(outcome: ConversationOutcome, kind: str, model_body: str,
                      suffix: str) -> ConversationOutcome:
        """Mark a read-only owner reply as draftable; its facts are those of ``model_body``."""
        return replace(outcome, reply_result=ClientReplyResult(
            kind, "read_only", outcome.text, facts_from_text(model_body), None, model_body,
            suffix))

    @staticmethod
    def _client_fact(start: datetime, zone: ZoneInfo, status: str,
                     reference: str | None = None) -> ClientReplyFact:
        return ClientReplyFact(day_text(start.astimezone(zone).date()),
                               clock_text(start, zone), status, reference)

    @staticmethod
    def _client_date_fact(day: date, status: str) -> ClientReplyFact:
        return ClientReplyFact(day_text(day), None, status)

    @staticmethod
    def _client_calendar_status(status: CalendarStatus) -> str:
        return {CalendarStatus.CONFIRMED: "confirmed",
                CalendarStatus.PENDING_APPROVAL: "pending owner approval"}.get(
                    status, "unknown")

    @staticmethod
    def _with_result(outcome: ConversationOutcome, kind: str, status: str,
                     facts: tuple[ClientReplyFact, ...] = (),
                     reason: str | None = None, detail: str | None = None
                     ) -> ConversationOutcome:
        return replace(outcome, reply_result=ClientReplyResult(
            kind, status, outcome.text, facts, reason, detail))

    def _handle(self, receipt: InboundReceipt) -> ConversationOutcome:
        if (not receipt.authorized_for_commands or receipt.body is None
                or receipt.keyword != Keyword.OTHER
                or receipt.role not in (SenderRole.OWNER, SenderRole.CLIENT)):
            return ConversationOutcome("This message cannot change the schedule.")
        if len(receipt.body) > MAX_MESSAGE_LENGTH:
            return ConversationOutcome("Please send one scheduling request in a shorter message.")
        if receipt.role == SenderRole.OWNER and receipt.sender != self._owner_number:
            return ConversationOutcome("This sender is no longer the verified owner number.")
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Processing clock must be timezone-aware")
        if self._consent.is_opted_out(receipt.business_id, receipt.sender):
            return ConversationOutcome("This sender is opted out of scheduling texts.")
        if receipt.role == SenderRole.CLIENT:
            profile = (self._repository.read_profile(receipt.business_id, receipt.client_id)
                       if receipt.client_id is not None else None)
            consent = self._consent.read_consent(receipt.business_id, receipt.sender)
            if (profile is None or not profile.active or profile.phone_verified_at is None
                    or profile.phone_e164 != receipt.sender or consent is None
                    or consent.client_id != profile.client_id):
                return ConversationOutcome("This sender needs a verified client profile and consent.")
        try:
            policy = self._repository.read_policy(receipt.business_id)
            targets = self._targets(receipt, now)
            # Only a client's own visits are bounded: an owner may have any number of pending
            # requests, and the model sees at most the first MAX_CONTEXT_APPOINTMENTS of them.
            if receipt.role == SenderRole.CLIENT and len(targets) > MAX_CONTEXT_APPOINTMENTS:
                return ConversationOutcome("Please contact the owner to review the current schedule.")
            prompt = (self._states.read_state(receipt.business_id, receipt.sender)
                      if receipt.role == SenderRole.CLIENT else None)
            calendar = self._calendar_context(prompt, now)
            context = MessageContext(
                receipt.role, now.astimezone(ZoneInfo(policy.timezone)).date(),
                policy.timezone,
                tuple(target.appointment_id[:8] for target in targets[:MAX_CONTEXT_APPOINTMENTS]),
                policy.booking_horizon_days,
                self._calendar_line(calendar),
                (self._history_reader.read_conversation_history(receipt, now)
                 if self._history_reader is not None and receipt.role == SenderRole.CLIENT
                 else ()),
                (prompt.kind.value if prompt is not None and not prompt.expired(now)
                 else "none"),
            )
            proposal = self._exact_command(receipt.body, context, targets)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            if receipt.role == SenderRole.CLIENT:
                return ConversationOutcome(
                    "I can't check the current schedule right now. Please try again later.")
            return ConversationOutcome("I couldn't understand that message. Please try again later.")
        if receipt.role == SenderRole.OWNER and self._counteroffers is not None:
            # Owner counteroffers (#175) run first so a YES that confirms an offer
            # is never read as approval of the pending request.
            countered = self._counteroffers.handle(
                receipt, now, targets, self._calendar_last(receipt, targets, now))
            if countered is not None:
                return countered
        # Inside a calendar conversation only a plain YES or NO answers an owner counteroffer;
        # "Just the confirmed one", "1", or "the first one" is about the calendar answer.
        body = receipt.body or ""
        plain = is_negative(body) or (is_affirmative(body) and not mentions_status(body))
        if (receipt.role == SenderRole.CLIENT and self._acceptance is not None
                and (calendar is None or plain)):
            # A reply to the owner's counteroffer (#176) is matched only to this client's
            # own current offer; every other message falls through unchanged.
            # A calendar answer asks nothing, so it is not a newer prompt to answer.
            accepted = self._acceptance.handle(
                receipt, now, prompt is not None and not prompt.expired(now)
                and prompt.kind != PromptKind.CALENDAR)
            if accepted is not None:
                if accepted.committed:
                    self._forget(prompt)
                return accepted
        exact = proposal is not None
        if proposal is None:
            answered = self._answer(receipt, prompt, targets, policy, now)
            if answered is not None:
                return answered
            if (receipt.role == SenderRole.CLIENT and calendar is not None
                    and is_action_like(receipt.body)):
                # Write guard: the calendar answer offered nothing to pick or confirm.
                return self._with_offer_note(receipt, now, ConversationOutcome(
                    "I only listed your visits, so nothing was booked or cancelled. "
                    "To change one, tell me which visit and what you'd like (for "
                    "example, cancel my Friday visit), or tell me a day to request "
                    "a new cleaning."))
            try:
                proposal = self._interpreter.propose(receipt.body, context)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                return ConversationOutcome(
                    "I couldn't understand that message. Please try again later.")
        # Any new request replaces the previous offer or question; only a
        # "which day" question for a known visit carries into its answer.
        moving = (prompt if prompt is not None and prompt.kind == PromptKind.RESCHEDULE_DAY
                  and not prompt.expired(now) else None)
        unclear = proposal.needs_clarification or proposal.intent in ("clarify", "unsupported")
        if unclear and calendar is not None and receipt.role == SenderRole.CLIENT:
            # Keep the calendar conversation open for the answer; nothing changes.
            return self._with_offer_note(receipt, now, ConversationOutcome(
                "I wasn't sure what you meant. Should I check another day or status, or do "
                "you want to request or change a visit?"))
        if not (receipt.role == SenderRole.CLIENT
                and proposal.intent in ("calendar_question", "clarify_booking")):
            self._forget(prompt)  # A calendar answer below replaces the prompt itself.
        if unclear:
            outcome = ConversationOutcome(self._clarify(
                receipt.role, proposal.intent, receipt.body or "", targets))
            return (self._with_result(outcome, "clarification", "none",
                                      reason="needs_clarification", detail=outcome.text)
                    if receipt.role == SenderRole.CLIENT else outcome)
        if (receipt.role == SenderRole.CLIENT and self._acceptance is not None
                and proposal.intent in ("request_booking", "availability", "reschedule",
                                        "cancel")):
            self._acceptance.supersede_for_new_request(receipt)  # A new request replaces it.
        if proposal.intent == "owner_decision":
            return self._owner_decision(receipt, proposal, targets)
        if exact and proposal.intent == "cancel":
            return self._cancel(receipt, proposal, targets)
        if exact:  # BOOK or RESCHEDULE syntax; _request rechecks the literal text.
            return self._request(receipt, proposal, targets, policy, now)
        # From here on the proposal came from the model: it can lead to an offer, a question,
        # or request_booking for a time this client was just offered; nothing else writes.
        if receipt.role != SenderRole.CLIENT:
            return ConversationOutcome(self._clarify(receipt.role, "clarify"))
        if proposal.intent in ("calendar_question", "clarify_booking"):
            # The model reads the question in any wording or language: range, statuses, and
            # list or count. What it leaves unset keeps the open conversation's value. The
            # answer itself is built from the client's own visits, never from model text.
            span = range_from_proposal(proposal.date_from, proposal.date_to)
            if proposal.range_scope == "all_upcoming":
                span = (None, None)
            elif span == (None, None) and calendar is not None:
                span = (calendar.first, calendar.last)  # A follow-up keeps the last range.
            statuses = (statuses_from_proposal(proposal.statuses)
                        or (calendar.statuses if calendar is not None else BOTH))
            view = (View(proposal.view) if proposal.view in ("list", "count")
                    else calendar.view if calendar is not None else View.LIST)
            question = ClientQuestion(
                view,
                *(span if span is not None else (date.max, date.min)),
                statuses if proposal.intent == "calendar_question" else BOTH,
                ambiguous_booking=proposal.intent == "clarify_booking")
            try:
                outcome = self._client_calendar(receipt, question, prompt, targets, policy, now)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                return ConversationOutcome(
                    "I can't check your current visits right now. Please try again later.")
            return self._bounded_read(outcome)
        if proposal.intent in ("confirm_cancel", "keep_visit"):
            return self._answer_cancel_prompt(receipt, proposal, prompt, targets, policy, now)
        if proposal.intent == "cancel":
            return self._ask_cancel(receipt, proposal, targets, policy, now)
        if proposal.intent == "reschedule":
            return self._ask_reschedule(receipt, proposal, targets, policy, now)
        if proposal.intent in ("availability", "request_booking"):
            original = (self._active_target(receipt, moving.appointment_id, targets)
                        if moving is not None else None)
            try:
                if proposal.intent == "request_booking":
                    requested = self._request_offered_time(
                        receipt, proposal, prompt, targets, policy, now)
                    if requested is not None:
                        # Committed: the client gets the request notification instead.
                        return requested
                outcome = self._offer_from_proposal(receipt, proposal, policy, now, original)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                return ConversationOutcome(
                    "I can't check current openings right now. Please try again later.")
            return self._bounded_read(outcome)
        return ConversationOutcome("Please describe the scheduling change you want.")

    def _request_offered_time(self, receipt: InboundReceipt, proposal: MessageProposal,
                              prompt: ConversationState | None,
                              targets: tuple[Appointment, ...], policy: AvailabilityPolicy,
                              now: datetime) -> ConversationOutcome | None:
        """The request_booking tool (#272): book one time this client was just offered.

        The model reads the client's acceptance in any wording and names the offered
        local date and time. The backend writes only if that instant is in this
        sender's own unexpired stored offer; anything else returns None and the caller
        offers current times. The hold service rechecks availability and the receipt
        ID makes a retried text replay. The result is pending owner approval.
        """
        if (prompt is None or prompt.kind != PromptKind.OFFER or prompt.expired(now)
                or prompt.client_id != receipt.client_id or proposal.date_from is None or proposal.date_from != proposal.date_to
                or proposal.time_from is None or proposal.time_from != proposal.time_to):
            return None
        try:
            wall = datetime.combine(date.fromisoformat(proposal.date_from),
                                    time.fromisoformat(proposal.time_from))
        except ValueError:
            return None
        zone = ZoneInfo(policy.timezone)
        matches = [option for option in prompt.options
                   if option.astimezone(zone).replace(tzinfo=None) == wall]
        if len(matches) != 1:
            return None
        return self._book_option(receipt, prompt, matches[0], targets, policy, now)

    def _answer_cancel_prompt(self, receipt: InboundReceipt, proposal: MessageProposal,
                              prompt: ConversationState | None,
                              targets: tuple[Appointment, ...], policy: AvailabilityPolicy,
                              now: datetime) -> ConversationOutcome:
        """The confirm_cancel and keep_visit tools (#273): the model reads a yes or no.

        The model names no visit. The backend acts only on this sender's own unexpired
        stored cancel confirmation, which already binds one appointment and version;
        _cancel_confirmed rechecks both and the receipt ID is the idempotency key.
        """
        zone = ZoneInfo(policy.timezone)
        if proposal.intent == "keep_visit" and (
                prompt is None or prompt.kind != PromptKind.CONFIRM_CANCEL
                or prompt.expired(now)):
            # Nothing is waiting to be cancelled, so there is nothing to ask about.
            outcome = ConversationOutcome("OK, nothing changed. Your visit stays booked.")
            return self._with_result(outcome, "nothing_changed", "none",
                                     reason="no_open_confirmation",
                                     detail="No cancellation was waiting.")
        if prompt is None or prompt.kind != PromptKind.CONFIRM_CANCEL:
            outcome = ConversationOutcome(
                "I don't have a cancellation waiting for your answer, so nothing was "
                "cancelled. Tell me which visit you'd like to cancel.")
            return self._with_result(outcome, "nothing_changed", "none",
                                     reason="no_open_confirmation",
                                     detail="No cancellation was waiting.")
        if prompt.expired(now):
            self._forget(prompt)
            outcome = ConversationOutcome(
                "That confirmation expired after 30 minutes, so nothing was cancelled. "
                "Tell me which visit you'd like to cancel.")
            return self._with_result(outcome, "nothing_changed", "none",
                                     reason="confirmation_expired",
                                     detail="The cancellation question expired.")
        if prompt.client_id != receipt.client_id:
            self._forget(prompt)
            outcome = ConversationOutcome(
                "I don't have a cancellation waiting for your answer, so nothing was "
                "cancelled. Tell me which visit you'd like to cancel.")
            return self._with_result(outcome, "nothing_changed", "none",
                                     reason="wrong_client_prompt",
                                     detail="No cancellation was waiting.")
        if proposal.intent == "keep_visit":
            self._forget(prompt)
            target = self._active_target(receipt, prompt.appointment_id, targets)
            kept = f"your {when_text(target.start_at, zone)} visit" if target else "your visit"
            outcome = ConversationOutcome(f"OK, I kept {kept}. Nothing was cancelled.")
            return (self._with_result(outcome, "cancel_kept",
                                      self._client_calendar_status(target.status),
                                      (self._client_fact(target.start_at, zone,
                                                         self._client_calendar_status(target.status),
                                                         target.appointment_id[:8]),))
                    if target is not None else outcome)
        return self._cancel_confirmed(receipt, prompt, targets, zone)

    @staticmethod
    def _bounded_read(outcome: ConversationOutcome) -> ConversationOutcome:
        """A read answer must fit one text; the client reply contract drafts it afterward."""
        if len(outcome.text) > 500:
            return ConversationOutcome(
                "I can't fit the current schedule in one text. Please ask about a shorter range.")
        return outcome

    def _targets(self, receipt: InboundReceipt, now: datetime) -> tuple[Appointment, ...]:
        if receipt.role == SenderRole.OWNER:
            return self._repository.read_pending_requests(receipt.business_id, now)
        if receipt.client_id is None:
            return ()
        events = self._repository.read_calendar(receipt.business_id).events
        appointments = [self._repository.read_appointment(event.event_id) for event in events
                        if event.status in (CalendarStatus.CONFIRMED,
                                            CalendarStatus.PENDING_APPROVAL)]
        return tuple(sorted((appointment for appointment in appointments
                             if appointment is not None
                             and appointment.client_id == receipt.client_id
                             and appointment.business_id == receipt.business_id
                             and appointment.end_at > now and appointment.occupies_time(now)),
                            key=lambda appointment: appointment.start_at))

    @staticmethod
    def _clarify(role: SenderRole, intent: str, body: str = "",
                 targets: tuple[Appointment, ...] = ()) -> str:
        if role == SenderRole.OWNER:
            return f"I wasn't sure what you meant. {CAPABILITIES}"
        if re.search(r"\breschedule\b", body, re.IGNORECASE):
            match = re.search(rf"\breschedule\s+({REFERENCE})(?=\s|$)",
                              body, re.IGNORECASE)
            target = (ConversationService._target(body, match.group(1), targets)
                      if match is not None else None)
            if target is not None and target.status != CalendarStatus.CONFIRMED:
                return "That request is pending owner approval. Rescheduling needs a confirmed visit."
            if target is not None:
                reference = target.appointment_id[:8]
                return (f"Which day would you like instead? Say something like 'Friday "
                        f"morning', or reply RESCHEDULE {reference} to YYYY-MM-DD for options.")
            return "Which visit would you like to move, and to what day?"
        if re.search(r"\bcancel\b", body, re.IGNORECASE):
            return "Which visit would you like to cancel? Tell me its day."
        if intent == "unsupported":
            return "I can help you book, reschedule, or cancel a cleaning. What would you like to do?"
        return ("What day would you like a cleaning? You can say something like "
                "'tomorrow morning' or 'Friday afternoon'.")

    @staticmethod
    def _exact_command(body: str, context: MessageContext,
                       targets: tuple[Appointment, ...]) -> MessageProposal | None:
        if context.actor == SenderRole.CLIENT:
            booking = BOOK_COMMAND.fullmatch(body.strip())
            if booking is not None:
                return MessageProposal("request_booking", None, booking.group(1), None, False)
            replacement = RESCHEDULE_COMMAND.fullmatch(body.strip())
            if replacement is not None:
                reference = replacement.group(1).lower()
                if ConversationService._target(body, reference, targets) is not None:
                    return MessageProposal("reschedule", reference, replacement.group(2),
                                           None, False)
        match = EXACT_COMMAND.fullmatch(body.strip())
        if match is None:
            return None
        action, reference = match.group(1).lower(), match.group(2).lower()
        if ConversationService._target(body, reference, targets) is None:
            return None
        if action in ("approve", "decline") and context.actor == SenderRole.OWNER:
            return MessageProposal("owner_decision", reference, None, action, False)
        if action == "cancel" and context.actor == SenderRole.CLIENT:
            return MessageProposal("cancel", reference, None, None, False)
        return None

    @staticmethod
    def _target(body: str, reference: str | None,
                targets: tuple[Appointment, ...]) -> Appointment | None:
        if not reference or not body:
            return None
        # A model-supplied reference must occur literally in the inbound text.
        mentioned = [target for target in targets if re.search(
            rf"(?<![A-Za-z0-9]){re.escape(target.appointment_id[:8])}(?![A-Za-z0-9])",
            body, re.IGNORECASE)]
        matches = [target for target in mentioned
                   if reference.lower() in (target.appointment_id[:8], target.appointment_id)]
        return matches[0] if len(mentioned) == len(matches) == 1 else None

    def _owner_decision(self, receipt: InboundReceipt, proposal: MessageProposal,
                        targets: tuple[Appointment, ...]) -> ConversationOutcome:
        if receipt.role != SenderRole.OWNER or proposal.owner_decision not in ("approve", "decline"):
            return ConversationOutcome("Owner approval needs an exact request reference.")
        if proposal.request_reference is None:
            # Without a reference nothing acts; the owner is told what is pending.
            return ConversationOutcome(self._pending_summary(receipt.business_id, targets))
        target = self._target(receipt.body or "", proposal.request_reference, targets)
        if (target is None or not re.fullmatch(
                rf"{proposal.owner_decision}\s+(?:request\s+)?"
                rf"(?:{re.escape(target.appointment_id[:8])}|"
                rf"{re.escape(target.appointment_id)})[.!]?",
                (receipt.body or "").strip(), re.IGNORECASE)):
            return ConversationOutcome("Reply APPROVE or DECLINE with one exact request reference.")
        action = Action.APPROVE if proposal.owner_decision == "approve" else Action.DECLINE
        try:
            result = self._lifecycle.apply(AppointmentCommand(
                receipt.business_id, target.appointment_id, receipt.sender,
                ActorRole.OWNER, action, receipt.provider_id, target.version))
        except ReplacementPending:
            return ConversationOutcome(
                "The client accepted a counteroffer for that request, so a replacement request "
                "is waiting. Nothing was approved. Approve or decline the replacement instead.")
        except (HoldExpired, InvalidTransition, StaleVersion, SlotConflict,
                TooManyConflicts, IdempotencyKeyReused):
            return ConversationOutcome("That request changed. Please review the current calendar.")
        state = "confirmed" if action == Action.APPROVE else "declined"
        return ConversationOutcome(
            f"Request {target.appointment_id[:8]} {state}."
            f"{self._resolved_note(receipt.business_id, target, result.replaced_appointment)}", True,
            result.appointment.appointment_id)

    def _cancel(self, receipt: InboundReceipt, proposal: MessageProposal,
                targets: tuple[Appointment, ...]) -> ConversationOutcome:
        target = self._target(receipt.body or "", proposal.request_reference, targets)
        if (target is None or (receipt.role == SenderRole.CLIENT and receipt.client_id is None)
                or not re.fullmatch(rf"cancel\s+(?:appointment\s+)?"
                                    rf"(?:{re.escape(target.appointment_id[:8])}|"
                                    rf"{re.escape(target.appointment_id)})[.!]?",
                                    (receipt.body or "").strip(), re.IGNORECASE)):
            return ConversationOutcome("Please give one exact appointment reference to cancel.")
        role = ActorRole.OWNER if receipt.role == SenderRole.OWNER else ActorRole.CLIENT
        actor_id = receipt.sender if role == ActorRole.OWNER else receipt.client_id
        if actor_id is None:
            return ConversationOutcome("This message cannot change the schedule.")
        try:
            result = self._lifecycle.apply(AppointmentCommand(
                receipt.business_id, target.appointment_id, actor_id, role,
                Action.CANCEL, receipt.provider_id, target.version))
        except (InvalidTransition, StaleVersion, ReplacementPending,
                TooManyConflicts, IdempotencyKeyReused):
            return ConversationOutcome("That appointment changed. Please review it before retrying.")
        outcome = ConversationOutcome(f"Appointment {target.appointment_id[:8]} cancelled.", True,
                                      result.appointment.appointment_id,
                                      result.client_outbox_id)
        if receipt.role == SenderRole.CLIENT:
            zone = ZoneInfo(self._repository.read_policy(receipt.business_id).timezone)
            return self._with_result(outcome, "cancelled", "cancelled",
                                     (self._client_fact(result.appointment.start_at, zone,
                                                        "cancelled", target.appointment_id[:8]),))
        return outcome

    def _request(self, receipt: InboundReceipt, proposal: MessageProposal,
                 targets: tuple[Appointment, ...], policy: AvailabilityPolicy,
                 now: datetime) -> ConversationOutcome:
        if receipt.role != SenderRole.CLIENT or receipt.client_id is None:
            return ConversationOutcome("Only a verified client can request a visit by text.")
        profile = self._repository.read_profile(receipt.business_id, receipt.client_id)
        if profile is None or not profile.active or profile.phone_verified_at is None:
            return ConversationOutcome("Please contact the owner to complete your client profile.")
        original = None
        if proposal.intent == "reschedule":
            original = self._target(receipt.body or "", proposal.request_reference, targets)
            if original is None or original.status != CalendarStatus.CONFIRMED:
                return ConversationOutcome("Please give the exact confirmed visit reference to reschedule.")
        reply_command = (f"RESCHEDULE {original.appointment_id[:8]} to" if original is not None
                         else "BOOK")
        body = (receipt.body or "").strip()
        if proposal.intent == "reschedule":
            command = RESCHEDULE_COMMAND.fullmatch(body)
            if (command is None or command.group(1).lower()
                    != (proposal.request_reference or "").lower()):
                return ConversationOutcome(
                    "Reply RESCHEDULE REFERENCE to YYYY-MM-DD HH:MM in local time.")
            date_text = command.group(2)
        else:
            command = BOOK_COMMAND.fullmatch(body)
            if command is None or proposal.request_reference is not None:
                return ConversationOutcome("Reply BOOK YYYY-MM-DD HH:MM in local time.")
            date_text = command.group(1)
        if not isinstance(date_text, str):
            return ConversationOutcome(self._clarify(
                receipt.role, "clarify", receipt.body or "", targets))
        if proposal.date_text != date_text:
            return ConversationOutcome(self._clarify(
                receipt.role, "clarify", receipt.body or "", targets))
        if EXPLICIT_DATE.fullmatch(date_text):
            try:
                day = date.fromisoformat(date_text)
            except ValueError:
                return ConversationOutcome(
                    f"That date is unavailable. Reply {reply_command} YYYY-MM-DD for options.")
            return self._offer(receipt, profile, policy, now, day, day, None, None, original)
        if not EXPLICIT_START.fullmatch(date_text):
            return ConversationOutcome(self._clarify(
                receipt.role, "clarify", receipt.body or "", targets))
        try:
            wall = datetime.fromisoformat(date_text.replace(" ", "T"))
        except ValueError:
            return ConversationOutcome("That date is invalid. Please send another YYYY-MM-DD date.")
        zone = ZoneInfo(policy.timezone)
        instants = {candidate.astimezone(UTC) for fold in (0, 1)
                    if (candidate := wall.replace(tzinfo=zone, fold=fold))
                    .astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall}
        if len(instants) != 1:
            return ConversationOutcome("That local time is missing or ambiguous. Please choose another.")
        try:
            result = self._holds.create(CreateHold(
                receipt.business_id, receipt.client_id, receipt.client_id,
                receipt.provider_id, next(iter(instants)), profile.default_duration_minutes,
                original.appointment_id if original else None), now)
        except (SlotConflict, InvalidReplacement, ReplacementPending,
                TooManyConflicts, IdempotencyKeyReused, InvalidDuration, ValueError):
            return ConversationOutcome("That time is unavailable. Please request another exact date and time.")
        return self._requested(receipt, original, next(iter(instants)), result.hold_id, policy)

    # Conversational flow. The model resolves words like "tomorrow" into a day
    # range; the backend offers real open times and remembers them. Only a
    # reply that maps to exactly one remembered option or prompt writes.

    def _forget(self, prompt: ConversationState | None) -> None:
        if prompt is not None:
            self._states.clear_state(prompt.business_id, prompt.sender, prompt.state_id)

    def _remember(self, receipt: InboundReceipt, kind: PromptKind, now: datetime, *,
                  options: tuple[datetime, ...] = (),
                  appointment: Appointment | None = None) -> None:
        self._states.put_state(ConversationState(
            receipt.business_id, receipt.sender, receipt.provider_id, kind, now,
            now + PROMPT_LIFETIME, options,
            appointment.appointment_id if appointment is not None else None,
            appointment.version if appointment is not None else None,
            receipt.client_id))

    def _answer(self, receipt: InboundReceipt, prompt: ConversationState | None,
                targets: tuple[Appointment, ...], policy: AvailabilityPolicy,
                now: datetime) -> ConversationOutcome | None:
        """Handle a reply to the business's own offer or question, or return None."""
        body = receipt.body or ""
        if receipt.role == SenderRole.OWNER:
            return self._owner_answer(receipt, targets, now)
        if prompt is None or prompt.kind in (PromptKind.RESCHEDULE_DAY, PromptKind.CALENDAR):
            return None
        zone = ZoneInfo(policy.timezone)
        today = now.astimezone(zone).date()
        if prompt.kind != PromptKind.CONFIRM_CANCEL:
            selection, index = match_selection(body, prompt.options, zone, today)
            answered = selection != Selection.NONE
        else:
            answered = is_affirmative(body, CANCEL_WORDS) or is_negative(body)
        if prompt.expired(now):
            self._forget(prompt)
            if not answered:
                return None
            outcome = ConversationOutcome(
                "That offer expired after 30 minutes, so nothing changed. Tell me what day "
                "works and I'll send the current open times.")
            return self._with_result(outcome,
                                     "nothing_changed" if prompt.kind == PromptKind.CONFIRM_CANCEL
                                     else "expired", "none", reason="prompt_expired",
                                     detail="The earlier prompt expired.")
        if prompt.kind == PromptKind.CONFIRM_CANCEL:
            if is_negative(body):
                self._forget(prompt)
                target = self._active_target(receipt, prompt.appointment_id, targets)
                kept = f"your {when_text(target.start_at, zone)} visit" if target else "your visit"
                outcome = ConversationOutcome(f"OK, I kept {kept}. Nothing was cancelled.")
                return (self._with_result(outcome, "cancel_kept",
                                          self._client_calendar_status(target.status),
                                          (self._client_fact(target.start_at, zone,
                                                             self._client_calendar_status(target.status),
                                                             target.appointment_id[:8]),))
                        if target is not None else outcome)
            if not answered:
                return None
            return self._cancel_confirmed(receipt, prompt, targets, zone)
        count = len(prompt.options)
        if selection == Selection.AMBIGUOUS:
            return ConversationOutcome(
                "I couldn't tell which time you meant, so nothing was booked. Reply with "
                f"'option 1' to 'option {count}', or the time with AM or PM." if count > 1 else
                "I couldn't tell if you meant that time, so nothing was booked. Reply YES "
                "to request it, or tell me another day.")
        if selection == Selection.UNMATCHED:
            return ConversationOutcome(
                "That isn't one of the times I offered, so nothing was booked. Reply with "
                + (f"an option number, 1 to {count}," if count > 1 else "YES")
                + " or tell me another day.")
        if selection != Selection.MATCH or index is None:
            return None
        if prompt.kind == PromptKind.OFFER:
            return self._book_option(receipt, prompt, prompt.options[index], targets, policy, now)
        # A chosen visit leads to the usual confirmation or day question; no write yet.
        self._forget(prompt)
        cancelling = prompt.kind == PromptKind.CHOOSE_CANCEL
        chosen = next((target for target in targets if target.start_at == prompt.options[index]
                       and (cancelling or target.status == CalendarStatus.CONFIRMED)), None)
        if chosen is None:
            return ConversationOutcome("That visit changed. Please tell me again which one you mean.")
        return (self._confirm_cancel(receipt, chosen, zone, now) if cancelling
                else self._ask_day(receipt, chosen, zone, now))

    def _client_calendar(self, receipt: InboundReceipt, question: ClientQuestion,
                         prompt: ConversationState | None, targets: tuple[Appointment, ...],
                         policy: AvailabilityPolicy, now: datetime) -> ConversationOutcome:
        """Answer from the client's own current visits. Writes nothing to the calendar.

        A question closes any open offer or question, and says so, so a later "yes" or
        number cannot act on a prompt the client has moved on from.
        """
        if prompt is not None and prompt.kind != PromptKind.CALENDAR:
            self._forget(prompt)  # An offer or confirmation is closed; the reply says so.
        zone = ZoneInfo(policy.timezone)
        text = answer_client_question(question, targets, now, zone)
        closed_note = ""
        if (prompt is not None and not prompt.expired(now)
                and prompt.kind != PromptKind.CALENDAR):
            closed_note = ("\nI closed my earlier question, so nothing was booked or cancelled. "
                           "Ask again when you're ready.")
            text += closed_note
        today = now.astimezone(zone).date()
        answered = (not question.ambiguous_booking
                    and (question.first is None or question.last is None
                         or (question.first <= question.last and question.last >= today
                             and (question.last - question.first).days < MAX_RANGE_DAYS)))
        if answered:
            # Remember only the range, statuses, and view, so a follow-up rereads the visits.
            self._states.put_state(ConversationState(
                receipt.business_id, receipt.sender, receipt.provider_id, PromptKind.CALENDAR,
                now, now + PROMPT_LIFETIME, client_id=receipt.client_id,
                calendar_first=question.first, calendar_last=question.last,
                calendar_statuses=tuple(sorted(status.value for status in question.statuses)),
                calendar_view=question.view.value))
        elif prompt is not None and prompt.kind == PromptKind.CALENDAR and prompt.expired(now):
            self._forget(prompt)
        # A question this answer only clarified leaves an open calendar conversation as it was.
        outcome = self._with_offer_note(receipt, now, ConversationOutcome(text))
        if len(outcome.text) > 500 and question.view == View.LIST and answered:
            # Every current visit still fits when rendered with its local start and
            # status, even if the detailed lines and safety notes do not.
            compact_note = ("\nEarlier question closed; nothing was booked or cancelled."
                            if closed_note else "")
            outcome = self._with_offer_note(receipt, now, ConversationOutcome(
                compact_client_list(question, targets, now, zone) + compact_note))
        if (answered and question.view == View.LIST and not closed_note
                and outcome.text == text):
            shown = tuple(target for target in targets
                          if (question.first is None or question.last is None
                              or max(question.first, today)
                              <= target.start_at.astimezone(zone).date() <= question.last))
            facts = tuple(self._client_fact(target.start_at, zone,
                                           "confirmed" if target.status == CalendarStatus.CONFIRMED
                                           else "pending owner approval",
                                           target.appointment_id[:8]) for target in shown)
            statuses = {fact.status for fact in facts}
            status = next(iter(statuses)) if len(statuses) == 1 else "mixed" if statuses else "none"
            return self._with_result(outcome, "calendar_list", status, facts,
                                     detail="No upcoming visits in this range." if not facts else None)
        return outcome

    def _with_offer_note(self, receipt: InboundReceipt, now: datetime,
                         outcome: ConversationOutcome) -> ConversationOutcome:
        """Say that an owner counteroffer is still open, so a later YES has one meaning."""
        note = (self._acceptance.reminder(receipt, now) if self._acceptance is not None
                else None)
        return ConversationOutcome(f"{outcome.text}\n{note}") if note else outcome

    @staticmethod
    def _calendar_line(calendar: ClientQuestion | None) -> str | None:
        """What the model may know of the open calendar answer: no visits, only its terms."""
        if calendar is None:
            return None
        span = ("all upcoming" if calendar.first is None or calendar.last is None
                else f"{calendar.first.isoformat()} to {calendar.last.isoformat()}")
        names = ", ".join(name for name, status in (("confirmed", CalendarStatus.CONFIRMED),
                                                    ("pending", CalendarStatus.PENDING_APPROVAL))
                          if status in calendar.statuses)
        return f"{span}; statuses: {names}; view: {calendar.view.value}"

    @staticmethod
    def _calendar_context(prompt: ConversationState | None,
                          now: datetime) -> ClientQuestion | None:
        """The client's last calendar question, while it is open for follow-ups."""
        if prompt is None or prompt.kind != PromptKind.CALENDAR or prompt.expired(now):
            return None
        return ClientQuestion(
            View(prompt.calendar_view or View.LIST), prompt.calendar_first, prompt.calendar_last,
            frozenset(CalendarStatus(value) for value in prompt.calendar_statuses))

    def _book_option(self, receipt: InboundReceipt, prompt: ConversationState, start: datetime,
                     targets: tuple[Appointment, ...], policy: AvailabilityPolicy,
                     now: datetime) -> ConversationOutcome:
        zone = ZoneInfo(policy.timezone)
        if receipt.client_id is None:
            return ConversationOutcome("Only a verified client can request a visit by text.")
        profile = self._repository.read_profile(receipt.business_id, receipt.client_id)
        if profile is None or not profile.active or profile.phone_verified_at is None:
            return ConversationOutcome("Please contact the owner to complete your client profile.")
        original = None
        if prompt.appointment_id is not None:
            original = self._active_target(receipt, prompt.appointment_id, targets)
            if original is None or original.status != CalendarStatus.CONFIRMED:
                self._forget(prompt)
                outcome = ConversationOutcome(
                    "The visit you wanted to move has changed, so nothing was booked. "
                    "Please tell me which visit to move.")
                return self._with_result(outcome, "nothing_changed", "none",
                                         reason="original_changed")
        try:
            result = self._holds.create(CreateHold(
                receipt.business_id, receipt.client_id, receipt.client_id, receipt.provider_id,
                start, profile.default_duration_minutes,
                original.appointment_id if original is not None else None), now)
        except (SlotConflict, InvalidReplacement, ReplacementPending, TooManyConflicts,
                IdempotencyKeyReused, InvalidDuration, ValueError):
            self._forget(prompt)
            outcome = ConversationOutcome(
                f"Sorry, {when_text(start, zone)} is no longer open, so nothing was booked. "
                "Tell me what day works and I'll send the current open times.")
            return self._with_result(outcome, "request_failed", "none",
                                     (self._client_fact(start, zone, "unavailable"),),
                                     "slot_taken")
        self._forget(prompt)
        return self._requested(receipt, original, start, result.hold_id, policy)

    @staticmethod
    def _requested(receipt: InboundReceipt, original: Appointment | None, start: datetime,
                   hold_id: str, policy: AvailabilityPolicy) -> ConversationOutcome:
        zone = ZoneInfo(policy.timezone)
        when = when_text(start, zone)
        if original is not None:
            text = (f"Requested a move to {when} (ref {hold_id[:8]}). It's pending owner "
                    f"approval. Your {when_text(original.start_at, zone)} visit "
                    f"(ref {original.appointment_id[:8]}) remains confirmed until then.")
        else:
            text = (f"Requested {when} (ref {hold_id[:8]}). It's pending owner approval, "
                    "not confirmed yet.")
        facts = [ConversationService._client_fact(start, zone, "pending owner approval",
                                                   hold_id[:8])]
        if original is not None:
            facts.append(ConversationService._client_fact(original.start_at, zone,
                                                           "confirmed", original.appointment_id[:8]))
        kind = "move_requested" if original is not None else "request_created"
        return ConversationOutcome(text, True, hold_id, f"{hold_id}#client",
                                   ClientReplyResult(kind, "pending", text, tuple(facts)))

    def _cancel_confirmed(self, receipt: InboundReceipt, prompt: ConversationState,
                          targets: tuple[Appointment, ...], zone: ZoneInfo) -> ConversationOutcome:
        self._forget(prompt)
        target = self._active_target(receipt, prompt.appointment_id, targets)
        if (target is None or target.version != prompt.appointment_version
                or receipt.client_id is None):
            outcome = ConversationOutcome(
                "That visit changed since I asked, so nothing was cancelled. "
                "Please tell me again which visit to cancel.")
            return self._with_result(outcome, "nothing_changed", "none",
                                     reason="visit_changed")
        try:
            result = self._lifecycle.apply(AppointmentCommand(
                receipt.business_id, target.appointment_id, receipt.client_id,
                ActorRole.CLIENT, Action.CANCEL, receipt.provider_id, target.version))
        except (InvalidTransition, StaleVersion, ReplacementPending,
                TooManyConflicts, IdempotencyKeyReused):
            outcome = ConversationOutcome(
                "That appointment changed. Please review it before retrying.")
            return self._with_result(outcome, "nothing_changed", "none",
                                     reason="stale_or_uncertain")
        what = "visit" if target.status == CalendarStatus.CONFIRMED else "request"
        outcome = ConversationOutcome(
            f"Cancelled your {when_text(target.start_at, zone)} {what} "
            f"(ref {target.appointment_id[:8]}).", True, result.appointment.appointment_id,
            result.client_outbox_id)
        return self._with_result(outcome, "cancelled", "cancelled",
                                 (self._client_fact(result.appointment.start_at, zone,
                                                    "cancelled", target.appointment_id[:8]),))

    def _calendar_last(self, receipt: InboundReceipt, targets: tuple[Appointment, ...],
                       now: datetime) -> bool:
        """The assistant's latest message to the owner was a calendar answer."""
        context = self._owner_questions.open_context(receipt.business_id, receipt.sender, now)
        if context is None:
            return False
        view = (self._counteroffers.open_offer(receipt.business_id, receipt.sender, now, targets)
                if self._counteroffers is not None else None)
        return view is None or not view.live or last_answered_at(context) > view.offer.created_at

    def _owner_answer(self, receipt: InboundReceipt, targets: tuple[Appointment, ...],
                      now: datetime) -> ConversationOutcome:
        """Route an owner message that is not an exact command or an offer instruction.

        Order: an open clarifying calendar question gets the first look; then a complete
        calendar question; then a calendar follow-up that carries no approval-like word;
        then the model reads the reply in context. Every path here is read-only unless the
        backend validates an approval, decline, or offer confirmation (see the model route).
        """
        questions, body = self._owner_questions, receipt.body or ""
        offers = self._counteroffers
        view = (offers.open_offer(receipt.business_id, receipt.sender, now, targets)
                if offers is not None else None)
        today = now.astimezone(ZoneInfo(
            self._repository.read_policy(receipt.business_id).timezone)).date()
        text = None
        if questions.awaiting_answer(receipt.business_id, receipt.sender, now):
            text = questions.answer_detailed(receipt.business_id, receipt.sender, body, now,
                                             receipt.provider_id)
        if text is None and questions.is_fresh_question(body, today):
            text = questions.answer_detailed(receipt.business_id, receipt.sender, body, now,
                                             receipt.provider_id)
        if text is None and not may_be_approval(body):
            text = questions.answer_detailed(receipt.business_id, receipt.sender, body, now,
                                             receipt.provider_id)
        if text is not None:
            return self._calendar_reply(text, view)
        return self._route_with_model(receipt, targets, now, view)

    def _calendar_reply(self, answer: CalendarAnswer,
                        view: "OfferView | None") -> ConversationOutcome:
        """A calendar answer never cancels an open offer: it says the offer still waits.

        A rendered page may be reworded by the model (#285); the paging footer and the
        open-offer reminder are then appended to the draft verbatim, so the owner always
        sees the exact YES/NO instruction and the offer's own text.
        """
        text, note = answer.text, ""
        if self._counteroffers is not None and view is not None:
            if view.live:
                # Do not stack a long reminder under "Reply MORE": two competing instructions.
                note = (self._counteroffers.short_reminder(view.offer) if "Reply MORE" in text
                        else self._counteroffers.reminder(view.offer))
            else:
                self._counteroffers.settle(view)
        outcome = ConversationOutcome(f"{text} {note}" if note else text)
        if answer.model_body is None:
            return outcome
        return self._owner_result(outcome, "owner_calendar", answer.model_body,
                                  answer.footer + (f" {note}" if note else ""))

    def _route_with_model(self, receipt: InboundReceipt, targets: tuple[Appointment, ...],
                          now: datetime, view: "OfferView | None") -> ConversationOutcome:
        """Let the model say what the reply means; the backend validates what it may do.

        The model proposes. Approve or decline acts only with no offer open (or just closed),
        exactly one pending request, a confident answer, a literal reference match, approval
        wording in the text, and, inside a calendar conversation, a clarifying question about
        that same request version that was actually sent first. An open offer is confirmed
        or cancelled only while its prompt is the latest message. Failures and doubt ask.
        """
        questions, body, offers = self._owner_questions, receipt.body or "", self._counteroffers
        context = questions.open_context(receipt.business_id, receipt.sender, now)
        policy = self._repository.read_policy(receipt.business_id)
        zone = ZoneInfo(policy.timezone)
        live = view is not None and view.live
        calendar_last = context is not None and (
            view is None or not view.live or last_answered_at(context) > view.offer.created_at)
        # The assistant's last clarifying question about a request, even one asked in a calendar
        # conversation that has since expired. A request counts as named only once that
        # question's reply was SENT, for a message our webhook received after the send, within
        # CLARIFICATION_LIFETIME of it, and not superseded by a later calendar answer. An unsent,
        # failed, unsaved, redelivered, or earlier-received question, or a lookup error, keeps
        # approvals blocked (the question is asked again); only a sent one that lapsed is ignored.
        asked = questions.read_clarification(receipt.business_id, receipt.sender, now)
        was_named = False
        asking = False
        named = None
        if asked is not None and asked.clarified_at is not None:
            sent_at = self._question_sent_at(receipt, asked.clarified_by)
            if (sent_at is None or asked.clarified_by == receipt.provider_id
                    or receipt.received_at <= sent_at or receipt.received_at <= asked.clarified_at
                    or (context is not None and asked.clarified_at < last_answered_at(context))):
                asking = True
            elif now < sent_at + CLARIFICATION_LIFETIME:
                asking = was_named = True
                named = next((target for target in targets
                              if target.appointment_id == asked.clarified_request
                              and target.version == asked.clarified_version), None)
            else:
                # Lapsed after 30 minutes, but the record lives on: a plain yes still means
                # the asked-about request only while it is the single pending one at the
                # same version. Otherwise ask again, naming the current request.
                asking = not (len(targets) == 1
                              and targets[0].appointment_id == asked.clarified_request
                              and targets[0].version == asked.clarified_version)
        gated = context is not None or asking
        if view is not None and view.live:
            kind = "offer_with_calendar_answer" if calendar_last else "offer_prompt"
        elif view is not None:
            kind = "offer_closed"
        elif named is not None:
            kind = "approval_question"
        elif context is not None:
            kind = "calendar_answer"
        else:
            kind = "none"

        def unsure(lead: str) -> ConversationOutcome:
            if view is not None and offers is not None:
                note = (offers.reminder(view.offer) if live else offers.closed_note(view.offer))
                return ConversationOutcome(f"{lead} {note}")
            return self._ask_owner(receipt, targets, now, lead)

        if self._owner_classifier is None:
            return unsure("I wasn't sure what you meant.")
        try:
            proposal = self._owner_classifier.classify_owner_reply(body, OwnerReplyContext(
                now.astimezone(zone).date(), policy.timezone, kind,
                context.view.value if context is not None else "none",
                context.first if context is not None else None,
                context.last if context is not None else None,
                tuple(sorted(status.value for status in context.statuses or ()))
                if context is not None else (),
                self._pending_ref(receipt.business_id, named, zone) if named else None,
                tuple(self._pending_ref(receipt.business_id, target, zone)
                      for target in targets[:MAX_CONTEXT_APPOINTMENTS]),
                self._offer_ref(view) if view is not None else None,
                self._owner_history(receipt, now)))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return unsure("I couldn't tell what you meant.")
        if proposal.confidence != Confidence.HIGH:
            return unsure("I wasn't sure what you meant.")
        intent = proposal.intent
        if intent in (OwnerReplyIntent.APPROVE_NAMED_REQUEST,
                      OwnerReplyIntent.DECLINE_NAMED_REQUEST):
            if view is not None:
                # A reply to an offer prompt or a finished offer never approves the request;
                # only an exact APPROVE <ref> does.
                return unsure("That reply doesn't approve or decline the request.")
            approving = intent == OwnerReplyIntent.APPROVE_NAMED_REQUEST
            reference = (proposal.request_reference or "").lower()
            chosen = [target for target in targets if len(reference) >= 8
                      and target.appointment_id.startswith(reference)]
            if len(chosen) == 1 and proposal.request_version != chosen[0].version:
                # A missing or different version means the model mis-copied or used an old
                # snapshot. It is never a decision; nothing changes.
                return unsure("I couldn't match that to a current request, so nothing changed.")
            if len(targets) > 1:
                # Several are pending: a decision is never inferred from a short reply. If the
                # model names exactly one current request and its current version, the owner
                # is asked to confirm it with the exact command. Nothing changes.
                plain = supports_approval(body) if approving else supports_decline(body)
                if len(chosen) == 1 and not plain:
                    return self._ask_decision(receipt, chosen[0], approving, now, True)
                return unsure("I wasn't sure which request you meant.")
            candidate = named if gated else (targets[0] if len(targets) == 1 else None)
            if (candidate is not None and len(targets) == 1 and len(reference) >= 8
                    and candidate.appointment_id.startswith(reference)):
                if supports_approval(body) if approving else supports_decline(body):
                    return self._decide(receipt, candidate,
                                        Action.APPROVE if approving else Action.DECLINE)
                # A decision in words other than a short plain reply: ask, naming the request
                # and the plain reply that would act. Nothing changes.
                return self._ask_decision(receipt, candidate, approving, now)
            return unsure("That request may have changed." if was_named
                          else "I wasn't sure what you meant.")
        if intent == OwnerReplyIntent.PREPARE_COUNTEROFFER:
            if offers is None:
                return unsure("I wasn't sure what you meant.")
            # Owner tool (#274): drafts the offer text only; sending still needs the owner's YES.
            try:
                return offers.prepare_from_model(
                    receipt, now, targets, proposal.request_reference, proposal.request_version,
                    proposal.offer_date, proposal.offer_time, calendar_last)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                return ConversationOutcome(
                    "I couldn't prepare that offer right now, so nothing was sent. "
                    "Please try again later.")
        if intent == OwnerReplyIntent.SHOW_REQUESTS:
            return self._show_requests(receipt.business_id, targets, proposal.request_reference,
                                       view)
        if intent == OwnerReplyIntent.CONFIRM_OFFER:
            if (offers is not None and view is not None and live and kind == "offer_prompt"
                    and supports_offer_send(body)):
                return offers.confirm_offer(view.offer, receipt, now, targets)
            if offers is not None and view is not None and live and kind == "offer_prompt":
                return ConversationOutcome(
                    "I wasn't sure what you meant. Reply YES to send exactly that offer, or NO "
                    f"to cancel it. To approve the original request, reply APPROVE "
                    f"{view.offer.request_id[:8]}.")
            return unsure("I wasn't sure what you meant.")
        if intent == OwnerReplyIntent.CANCEL_OFFER:
            if offers is not None and view is not None and live and supports_offer_cancel(body):
                return offers.cancel_offer(view.offer)
            return unsure("I wasn't sure what you meant.")
        if intent == OwnerReplyIntent.CALENDAR_FOLLOWUP and context is not None:
            followed = questions.apply_followup(
                receipt.business_id, receipt.sender, now, receipt.provider_id,
                proposal.statuses, proposal.range_first, proposal.range_last)
            if followed is not None:
                return self._calendar_reply(followed, view)
            return unsure("I wasn't sure what you meant.")
        if intent in (OwnerReplyIntent.HOW_TO, OwnerReplyIntent.CALENDAR_QUESTION):
            return self._explain(receipt.business_id, receipt.sender, now, targets, view,
                                 intent == OwnerReplyIntent.CALENDAR_QUESTION)
        return unsure("I wasn't sure what you meant.")

    def _owner_history(self, receipt: InboundReceipt, now: datetime
                       ) -> tuple[HistoryMessage, ...]:
        """The owner's 24-hour thread (#285) for the classifier. It is optional context: any
        read failure (a DynamoDB throttle included) means no transcript, as before #285. The
        backend still validates everything the model proposes, so this can never cause a write."""
        if self._history_reader is None:
            return ()
        try:
            return self._history_reader.read_conversation_history(receipt, now)
        except Exception:  # noqa: BLE001
            return ()

    def _ask_decision(self, receipt: InboundReceipt, target: Appointment, approving: bool,
                      now: datetime, exact: bool = False) -> ConversationOutcome:
        """Ask for confirmation; ``exact`` names the full command because several are pending."""
        self._owner_questions.mark_clarified(
            receipt.business_id, receipt.sender, now, target.appointment_id, target.version,
            receipt.provider_id)
        verb, other = ("approve", "DECLINE") if approving else ("decline", "APPROVE")
        ref = f" {target.appointment_id[:8]}" if exact else ""
        return ConversationOutcome(
            f"Do you want to {verb} {self._request_line(receipt.business_id, target)}? "
            f"Reply {verb.upper()}{ref} to confirm, or {other}{ref}. Nothing has changed.")

    def _show_requests(self, business_id: str, targets: tuple[Appointment, ...],
                       reference: str | None, view: "OfferView | None") -> ConversationOutcome:
        """The show_requests owner tool (#274): scoped, read-only details of pending requests.

        The model may reword it (#285); the command instruction and any open-offer reminder
        are fixed text that follows the draft.
        """
        ref = (reference or "").lower()
        chosen = [target for target in targets
                  if len(ref) >= 8 and target.appointment_id.startswith(ref)]
        if len(chosen) == 1:
            target = chosen[0]
            short = target.appointment_id[:8]
            text = (f"Pending owner approval, not confirmed: "
                    f"{self._request_line(business_id, target)}, "
                    f"{target.duration_minutes} minutes. Reply APPROVE {short} "
                    f"or DECLINE {short} to decide it. Nothing has changed.")
            model_body = (f"Pending owner approval, not confirmed: "
                          f"{self._request_line(business_id, target, True)}, "
                          f"{target.duration_minutes} minutes.")
            suffix = (f" Reply APPROVE {short} or DECLINE {short} to decide it. "
                      "Nothing has changed.")
        else:
            text = self._pending_summary(business_id, targets)
            model_body = self._pending_summary(business_id, targets, False, True)
            suffix = (" Nothing has changed." if not targets else
                      " Nothing has changed. Reply APPROVE or DECLINE with "
                      + ("its reference." if len(targets) == 1 else "the reference."))
        note = ""
        if view is not None and view.live and self._counteroffers is not None:
            note = f" {self._counteroffers.reminder(view.offer)}"
            text = f"{text}{note}"
        return self._owner_result(ConversationOutcome(text), "owner_requests", model_body,
                                  suffix + note)

    def _offer_ref(self, view: "OfferView") -> PendingRef:
        assert self._counteroffers is not None
        client, when = self._counteroffers.offer_line(view.offer)
        return PendingRef(view.offer.request_id[:8], client, when)

    def _explain(self, business_id: str, sender: str, now: datetime,
                 targets: tuple[Appointment, ...], view: "OfferView | None",
                 calendar_question: bool) -> ConversationOutcome:
        """Capability help, only when the owner asked for it. Writes nothing.

        The how-to wording may be drafted (#285); the exact approve/decline instruction and
        any open-offer reminder are fixed text that follows the draft.
        """
        live = view is not None and view.live and self._counteroffers is not None
        if calendar_question:
            text = self._owner_questions.ask_range(business_id, sender, now)
        else:
            decide = ("To approve or decline a request, reply APPROVE or DECLINE with its "
                      "reference.")
            summary = ("" if live else f"{self._pending_summary(business_id, targets, False)} ")
            text = f"{summary}{decide} {CAPABILITIES}"
        note = ""
        if live:
            assert view is not None and self._counteroffers is not None
            note = f" {self._counteroffers.reminder(view.offer)}"
            text = f"{text}{note}"
        outcome = ConversationOutcome(text)
        if calendar_question:
            return outcome  # A question back to the owner is fixed text.
        pending = ("" if live else self._pending_summary(business_id, targets, False, True))
        return self._owner_result(
            outcome, "owner_how_to", pending,
            f" To approve or decline a request, reply APPROVE or DECLINE with its reference.{note}")

    def _question_sent_at(self, receipt: InboundReceipt, asked_by: str) -> datetime | None:
        """When the saved reply to ``asked_by`` was sent; None unless it surely was."""
        if self._reply_lookup is None:
            return None
        try:
            saved = self._reply_lookup.read_reply_text(receipt.business_id, asked_by)
            sent_at = self._reply_lookup.read_reply_sent_at(receipt.business_id, asked_by)
        except Exception:  # noqa: BLE001 - any lookup failure must fail closed
            return None
        return sent_at if saved is not None else None

    def _pending_ref(self, business_id: str, target: Appointment, zone: ZoneInfo) -> PendingRef:
        profile = self._repository.read_profile(business_id, target.client_id)
        name = profile.name.split()[0] if profile is not None and profile.name.split() else "client"
        return PendingRef(target.appointment_id[:8], name, when_text(target.start_at, zone),
                          target.version)

    def _ask_owner(self, receipt: InboundReceipt, targets: tuple[Appointment, ...],
                   now: datetime, lead: str) -> ConversationOutcome:
        """One focused question; remembers the named request for the answer. Writes nothing."""
        if len(targets) == 1:
            target = targets[0]
            self._owner_questions.mark_clarified(
                receipt.business_id, receipt.sender, now, target.appointment_id, target.version,
                receipt.provider_id)
            return ConversationOutcome(
                f"{lead} Do you mean approve {self._request_line(receipt.business_id, target)}, "
                "or something about the calendar? Reply APPROVE or DECLINE to decide it, "
                "or ask me about the calendar.")
        if not targets:
            return ConversationOutcome(
                f"{lead} No request is waiting for approval, so nothing changed. {CAPABILITIES}")
        return ConversationOutcome(
            f"{lead} {self._pending_summary(receipt.business_id, targets)}")

    def _decide(self, receipt: InboundReceipt, target: Appointment,
                action: Action) -> ConversationOutcome:
        try:
            result = self._lifecycle.apply(AppointmentCommand(
                receipt.business_id, target.appointment_id, receipt.sender,
                ActorRole.OWNER, action, receipt.provider_id, target.version))
        except ReplacementPending:
            return ConversationOutcome(
                "The client accepted a counteroffer for that request, so a replacement request "
                "is waiting. Nothing was approved. Approve or decline the replacement instead.")
        except (HoldExpired, InvalidTransition, StaleVersion, SlotConflict,
                TooManyConflicts, IdempotencyKeyReused):
            return ConversationOutcome("That request changed. Please review the current calendar.")
        state = "Approved" if action == Action.APPROVE else "Declined"
        return ConversationOutcome(
            f"{state}: {self._request_line(receipt.business_id, target)}."
            f"{self._resolved_note(receipt.business_id, target, result.replaced_appointment)}", True,
            result.appointment.appointment_id)

    def _resolved_note(self, business_id: str, approved: Appointment,
                       replaced: Appointment | None) -> str:
        """Approval of an accepted counteroffer also resolves the request it replaced."""
        if replaced is None or replaced.status != CalendarStatus.DECLINED:
            return ""
        return (" Both requests are resolved: the accepted counteroffer "
                f"{self._request_line(business_id, approved)} is approved, and the original "
                f"request {self._request_line(business_id, replaced)} is closed.")

    def _request_line(self, business_id: str, target: Appointment,
                      first_name_only: bool = False) -> str:
        zone = ZoneInfo(self._repository.read_policy(business_id).timezone)
        profile = self._repository.read_profile(business_id, target.client_id)
        name = profile.name if profile is not None else "client"
        if first_name_only and name.split():
            name = name.split()[0]  # What a model sees, as in the owner classifier context.
        return f"{name}, {when_text(target.start_at, zone)} (ref {target.appointment_id[:8]})"

    def _pending_summary(self, business_id: str, targets: tuple[Appointment, ...],
                         how: bool = True, for_model: bool = False) -> str:
        """The pending-request text. ``for_model`` is the facts only: first names, and no
        command instruction (the fixed suffix carries it)."""
        if not targets:
            return ("No request is waiting for approval right now." if for_model else
                    "No request is waiting for approval right now, so nothing changed.")
        if len(targets) == 1:
            line = self._request_line(business_id, targets[0], for_model)
            if not how or for_model:
                return f"One request is pending: {line}."
            return f"Reply APPROVE or DECLINE with its reference: {line}."
        shown = "; ".join(self._request_line(business_id, target, for_model)
                          for target in targets[:3])
        more = f"; and {len(targets) - 3} more" if len(targets) > 3 else ""
        if for_model:
            return f"{len(targets)} requests are pending: {shown}{more}."
        return (f"{len(targets)} requests are pending, so nothing changed: {shown}{more}. "
                "Reply APPROVE or DECLINE with the reference.")

    @staticmethod
    def _active_target(receipt: InboundReceipt, appointment_id: str | None,
                       targets: tuple[Appointment, ...]) -> Appointment | None:
        """Re-resolve a remembered appointment against the sender's current visits."""
        return next((target for target in targets if target.appointment_id == appointment_id
                     and target.client_id == receipt.client_id), None)

    def _resolve_target(self, receipt: InboundReceipt, proposal: MessageProposal,
                        targets: tuple[Appointment, ...], zone: ZoneInfo, verb: str,
                        statuses: tuple[CalendarStatus, ...], now: datetime
                        ) -> tuple[Appointment | None, str | None]:
        candidates = tuple(target for target in targets if target.status in statuses)
        if proposal.request_reference:
            named = self._target(receipt.body or "", proposal.request_reference, targets)
            if named is not None and named.status in statuses:
                return named, None
        day = None
        if proposal.target_date:
            try:
                day = date.fromisoformat(proposal.target_date)
            except ValueError:
                return None, f"Which visit would you like to {verb}? Tell me its day."
            candidates = tuple(target for target in candidates
                               if target.start_at.astimezone(zone).date() == day)
        if len(candidates) == 1:
            return candidates[0], None
        listed = "; ".join(when_text(target.start_at, zone) for target in targets
                           if target.status in statuses)
        if not candidates:
            if not listed:
                return None, f"I don't see an upcoming visit to {verb}."
            where = f" on {day_text(day)}" if day is not None else ""
            return None, f"I don't see a visit{where} to {verb}. Your upcoming visits: {listed}."
        if len(candidates) > MAX_OPTIONS:
            return None, (f"Which visit would you like to {verb}? You have {listed}. "
                          "Tell me the day.")
        kind = PromptKind.CHOOSE_CANCEL if verb == "cancel" else PromptKind.CHOOSE_MOVE
        self._states.put_state(ConversationState(
            receipt.business_id, receipt.sender, receipt.provider_id, kind, now,
            now + PROMPT_LIFETIME, tuple(target.start_at for target in candidates),
            client_id=receipt.client_id))
        numbered = ", ".join(f"{number}) {when_text(target.start_at, zone)}"
                             for number, target in enumerate(candidates, 1))
        return None, (f"Which visit would you like to {verb}? {numbered}. "
                      "Reply with the number.")

    def _ask_cancel(self, receipt: InboundReceipt, proposal: MessageProposal,
                    targets: tuple[Appointment, ...], policy: AvailabilityPolicy,
                    now: datetime) -> ConversationOutcome:
        zone = ZoneInfo(policy.timezone)
        target, question = self._resolve_target(
            receipt, proposal, targets, zone, "cancel",
            (CalendarStatus.CONFIRMED, CalendarStatus.PENDING_APPROVAL), now)
        if target is None:
            outcome = ConversationOutcome(question or "Which visit would you like to cancel?")
            facts = tuple(self._client_fact(item.start_at, zone,
                                            self._client_calendar_status(item.status))
                          for item in targets)
            return self._with_result(outcome, "cancel_clarification", "none", facts,
                                     reason="visit_not_resolved")
        return self._confirm_cancel(receipt, target, zone, now)

    def _confirm_cancel(self, receipt: InboundReceipt, target: Appointment, zone: ZoneInfo,
                        now: datetime) -> ConversationOutcome:
        self._remember(receipt, PromptKind.CONFIRM_CANCEL, now, appointment=target)
        what = ("visit" if target.status == CalendarStatus.CONFIRMED
                else "request (still pending approval)")
        outcome = ConversationOutcome(
            f"Cancel your {when_text(target.start_at, zone)} {what}? Reply YES to confirm.")
        return self._with_result(outcome, "cancel_question",
                                 self._client_calendar_status(target.status),
                                 (self._client_fact(target.start_at, zone,
                                                    self._client_calendar_status(target.status),
                                                    target.appointment_id[:8]),))

    def _ask_day(self, receipt: InboundReceipt, target: Appointment, zone: ZoneInfo,
                 now: datetime) -> ConversationOutcome:
        self._remember(receipt, PromptKind.RESCHEDULE_DAY, now, appointment=target)
        outcome = ConversationOutcome(
            f"What day would you like instead of your {when_text(target.start_at, zone)} "
            "visit? It stays booked until the owner approves a new time.")
        return self._with_result(outcome, "reschedule_day_question", "confirmed",
                                 (self._client_fact(target.start_at, zone, "confirmed",
                                                    target.appointment_id[:8]),))

    def _ask_reschedule(self, receipt: InboundReceipt, proposal: MessageProposal,
                        targets: tuple[Appointment, ...], policy: AvailabilityPolicy,
                        now: datetime) -> ConversationOutcome:
        zone = ZoneInfo(policy.timezone)
        target, question = self._resolve_target(
            receipt, proposal, targets, zone, "move", (CalendarStatus.CONFIRMED,), now)
        if target is None:
            if any(item.status == CalendarStatus.PENDING_APPROVAL for item in targets) and \
                    not any(item.status == CalendarStatus.CONFIRMED for item in targets):
                outcome = ConversationOutcome(
                    "Your request is still pending owner approval. Rescheduling needs a "
                    "confirmed visit; you can cancel the request and ask for another time.")
                return self._with_result(outcome, "reschedule_clarification", "pending",
                                         reason="no_confirmed_visit")
            outcome = ConversationOutcome(question or "Which visit would you like to move?")
            facts = tuple(self._client_fact(item.start_at, zone, "confirmed")
                          for item in targets if item.status == CalendarStatus.CONFIRMED)
            return self._with_result(outcome, "reschedule_clarification", "none", facts,
                                     reason="visit_not_resolved")
        if proposal.date_from is None:
            return self._ask_day(receipt, target, zone, now)
        return self._offer_from_proposal(receipt, proposal, policy, now, target)

    def _offer_from_proposal(self, receipt: InboundReceipt, proposal: MessageProposal,
                             policy: AvailabilityPolicy, now: datetime,
                             original: Appointment | None) -> ConversationOutcome:
        if receipt.client_id is None:
            return ConversationOutcome("Only a verified client can request a visit by text.")
        profile = self._repository.read_profile(receipt.business_id, receipt.client_id)
        if profile is None or not profile.active or profile.phone_verified_at is None:
            return ConversationOutcome("Please contact the owner to complete your client profile.")
        if proposal.date_from is None:
            if original is not None:
                self._remember(receipt, PromptKind.RESCHEDULE_DAY, now, appointment=original)
            outcome = ConversationOutcome(self._clarify(SenderRole.CLIENT, "clarify"))
            facts = ((self._client_fact(original.start_at, ZoneInfo(policy.timezone),
                                        "confirmed", original.appointment_id[:8]),)
                     if original is not None else ())
            return self._with_result(outcome, "date_clarification", "none", facts,
                                     reason="missing_day")
        try:
            first = date.fromisoformat(proposal.date_from)
            last = date.fromisoformat(proposal.date_to or proposal.date_from)
            earliest = time.fromisoformat(proposal.time_from) if proposal.time_from else None
            latest = time.fromisoformat(proposal.time_to) if proposal.time_to else None
            if (earliest is None) != (latest is None):  # "After 2" or "before noon".
                earliest, latest = earliest or time(0), latest or time(23, 59)
        except ValueError:
            outcome = ConversationOutcome(
                "I couldn't tell which day you meant. What date works for you?")
            return self._with_result(outcome, "date_clarification", "none",
                                     reason="invalid_day")
        if last < first or (earliest is not None and latest is not None and latest < earliest):
            outcome = ConversationOutcome(
                "I couldn't tell which day you meant. What date works for you?")
            return self._with_result(outcome, "date_clarification", "none",
                                     reason="invalid_range")
        return self._offer(receipt, profile, policy, now, first, last, earliest, latest, original)

    def _offer(self, receipt: InboundReceipt, profile: ClientProfile, policy: AvailabilityPolicy,
               now: datetime, first: date, last: date, earliest: time | None,
               latest: time | None, original: Appointment | None) -> ConversationOutcome:
        """Offer real open times. Offering writes nothing to the calendar."""
        zone = ZoneInfo(policy.timezone)
        today = now.astimezone(zone).date()
        horizon = today + timedelta(days=policy.booking_horizon_days)
        if last < today:
            outcome = ConversationOutcome("That date has passed. What day works for you?")
            return self._with_result(outcome, "date_clarification", "none",
                                     reason="date_in_past")
        if first > horizon:
            outcome = ConversationOutcome(
                f"I can book up to {policy.booking_horizon_days} days ahead, through "
                f"{day_text(horizon)}. What day in that range works for you?")
            return self._with_result(outcome, "date_clarification", "none",
                                     (self._client_date_fact(horizon, "booking horizon"),),
                                     reason="outside_horizon")
        first, last = max(first, today), min(last, horizon)
        events = None
        if original is not None:
            events = tuple(event for event in self._repository.read_calendar(
                receipt.business_id).events if event.event_id != original.appointment_id)
        starts: list[datetime] = []
        try:
            for offset in range((last - first).days + 1):
                day = first + timedelta(days=offset)
                starts.extend(
                    self._availability.find_starts(
                        receipt.business_id, day, profile.default_duration_minutes, now)
                    if events is None else
                    available_starts(policy, day, profile.default_duration_minutes, events, now))
        except (InvalidDuration, InvalidPolicy, ValueError):
            outcome = ConversationOutcome("Please contact the owner to schedule this visit.")
            return self._with_result(outcome, "request_failed", "none",
                                     reason="invalid_availability_policy")
        span = (f"on {day_text(first)}" if first == last
                else f"between {day_text(first)} and {day_text(last)}")
        if not starts:
            outcome = ConversationOutcome(
                f"I don't have any openings {span}. Would another day work?")
            dates = (first,) if first == last else (first, last)
            return self._with_result(outcome, "request_failed", "none",
                                     tuple(self._client_date_fact(day, "no openings")
                                           for day in dates),
                                     reason="no_openings", detail=span)
        note = ""
        chosen: tuple[datetime, ...]
        if earliest is not None and latest is not None:
            wall = {start: start.astimezone(zone).time() for start in starts}
            if earliest == latest:
                exact = tuple(start for start in starts if wall[start] == earliest)
                target = earliest.hour * 60 + earliest.minute
                nearest = sorted(starts, key=lambda start: (
                    abs(wall[start].hour * 60 + wall[start].minute - target), start))
                chosen = (spread(exact, zone) if exact
                          else tuple(sorted(nearest[:MIN_OPTIONS])))
                if not exact:
                    note = "That exact time isn't open. "
            else:
                window = tuple(start for start in starts if earliest <= wall[start] <= latest)
                chosen = spread(window or tuple(starts), zone)
                if not window:
                    note = "Nothing is open in that part of the day. "
        else:
            chosen = spread(tuple(starts), zone)
        # A replacement offer remembers the visit it would replace.
        self._remember(receipt, PromptKind.OFFER, now, options=chosen, appointment=original)
        lead = (f"To move your {when_text(original.start_at, zone)} visit: "
                if original is not None else "")
        minutes = profile.default_duration_minutes
        length = f"{minutes // 60}-hour" if minutes % 60 == 0 else f"{minutes}-minute"
        if len(chosen) == 1:
            body = (f"{when_text(chosen[0], zone)} is open for your {length} cleaning. "
                    "Reply YES to request it.")
        else:
            days = {start.astimezone(zone).date() for start in chosen}
            if len(days) == 1:
                listed = ", ".join(f"{number}) {clock_text(start, zone)}"
                                   for number, start in enumerate(chosen, 1))
                body = f"Open times on {day_text(next(iter(days)))}: {listed}."
            else:
                listed = ", ".join(f"{number}) {when_text(start, zone)}"
                                   for number, start in enumerate(chosen, 1))
                body = f"Open times for your {length} cleaning: {listed}."
            body += " Reply with the number or time you want."
        keeps = " Your current visit stays booked until then." if original is not None else ""
        outcome = ConversationOutcome(
            f"{note}{lead}{body} The owner approves every request.{keeps} "
            "This offer is good for 30 minutes.")
        facts = tuple(self._client_fact(start, zone, "open") for start in chosen)
        if original is not None:
            facts += (self._client_fact(original.start_at, zone, "confirmed",
                                        original.appointment_id[:8]),)
        return self._with_result(outcome, "offer_made", "none", facts,
                                 "requested_time_unavailable" if note else None)
