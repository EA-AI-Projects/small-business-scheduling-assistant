"""Validated scheduling commands from a trusted, verified SMS receipt.

The interpreter proposes intent only. This layer checks actor, target, date,
current state, and policy before calling the existing transactional services.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Protocol
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
from scheduling.domain.client_records import ClientProfile
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
    normalized,
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
from scheduling.domain.owner_calendar_questions import OwnerCalendarQuestions, ReplyLookup
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyClassifier,
    OwnerReplyContext,
    OwnerReplyIntent,
    PendingRef,
    may_be_approval,
)
from scheduling.domain.sms_ingress import (
    ConsentEvidence,
    InboundReceipt,
    Keyword,
    SenderRole,
    normalize_phone,
)

if TYPE_CHECKING:
    from scheduling.domain.owner_counteroffer import CounterofferService

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


OWNER_APPROVAL = re.compile(
    r"(?:yes|yep|yeah|y|ok|okay|sure|approve|approved|confirm|confirmed)"
    r"(?:,? (?:approve|approved|please|approve it|it|thanks|thank you))?")
OWNER_DECLINE = re.compile(r"(?:decline|declined|reject|deny)(?: it| (?:the )?request)?"
                           r"(?:,? (?:please|thanks|thank you))?")


@dataclass(frozen=True)
class MessageContext:
    actor: SenderRole
    today: date
    timezone: str
    references: tuple[str, ...]
    horizon_days: int = 14


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


@dataclass(frozen=True)
class ConversationOutcome:
    text: str
    committed: bool = False
    appointment_id: str | None = None


class MessageInterpreter(Protocol):
    def propose(self, body: str, context: MessageContext) -> MessageProposal: ...


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
                 counteroffers: "CounterofferService | None" = None) -> None:
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

    def handle(self, receipt: InboundReceipt) -> ConversationOutcome:
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
            if len(targets) > MAX_CONTEXT_APPOINTMENTS:
                return ConversationOutcome("Please contact the owner to review the current schedule.")
            context = MessageContext(
                receipt.role, now.astimezone(ZoneInfo(policy.timezone)).date(),
                policy.timezone, tuple(target.appointment_id[:8] for target in targets),
                policy.booking_horizon_days,
            )
            prompt = (self._states.read_state(receipt.business_id, receipt.sender)
                      if receipt.role == SenderRole.CLIENT else None)
            proposal = self._exact_command(receipt.body, context, targets)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return ConversationOutcome("I couldn't understand that message. Please try again later.")
        if receipt.role == SenderRole.OWNER and self._counteroffers is not None:
            # Owner counteroffers (#175) run first so a YES that confirms an offer
            # is never read as approval of the pending request.
            countered = self._counteroffers.handle(receipt, now, targets)
            if countered is not None:
                return countered
        exact = proposal is not None
        if proposal is None:
            answered = self._answer(receipt, prompt, targets, policy, now)
            if answered is not None:
                return answered
            try:
                proposal = self._interpreter.propose(receipt.body, context)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                return ConversationOutcome(
                    "I couldn't understand that message. Please try again later.")
        # Any new request replaces the previous offer or question; only a
        # "which day" question for a known visit carries into its answer.
        moving = (prompt if prompt is not None and prompt.kind == PromptKind.RESCHEDULE_DAY
                  and not prompt.expired(now) else None)
        self._forget(prompt)
        if proposal.needs_clarification or proposal.intent in ("clarify", "unsupported"):
            return ConversationOutcome(self._clarify(
                receipt.role, proposal.intent, receipt.body or "", targets))
        if proposal.intent == "owner_decision":
            return self._owner_decision(receipt, proposal, targets)
        if exact and proposal.intent == "cancel":
            return self._cancel(receipt, proposal, targets)
        if exact:  # BOOK or RESCHEDULE syntax; _request rechecks the literal text.
            return self._request(receipt, proposal, targets, policy, now)
        # From here on the proposal came from the model: it can only lead to an
        # offer or a question, never directly to a write.
        if receipt.role != SenderRole.CLIENT:
            return ConversationOutcome(self._clarify(receipt.role, "clarify"))
        if proposal.intent == "cancel":
            return self._ask_cancel(receipt, proposal, targets, policy, now)
        if proposal.intent == "reschedule":
            return self._ask_reschedule(receipt, proposal, targets, policy, now)
        if proposal.intent in ("availability", "request_booking"):
            original = (self._active_target(receipt, moving.appointment_id, targets)
                        if moving is not None else None)
            return self._offer_from_proposal(receipt, proposal, policy, now, original)
        return ConversationOutcome("Please describe the scheduling change you want.")

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
            return ("Reply YES to approve or DECLINE to decline when one request is pending, "
                    "or APPROVE or DECLINE with the request reference.")
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
            # Only plain YES/APPROVE/DECLINE act without a reference (see _owner_reply).
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
        except (HoldExpired, InvalidTransition, StaleVersion, SlotConflict,
                TooManyConflicts, IdempotencyKeyReused):
            return ConversationOutcome("That request changed. Please review the current calendar.")
        state = "confirmed" if action == Action.APPROVE else "declined"
        return ConversationOutcome(f"Request {target.appointment_id[:8]} {state}.", True,
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
        return ConversationOutcome(f"Appointment {target.appointment_id[:8]} cancelled.", True,
                                   result.appointment.appointment_id)

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
        if prompt is None or prompt.kind == PromptKind.RESCHEDULE_DAY:
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
            return ConversationOutcome(
                "That offer expired after 30 minutes, so nothing changed. Tell me what day "
                "works and I'll send the current open times.")
        if prompt.kind == PromptKind.CONFIRM_CANCEL:
            if is_negative(body):
                self._forget(prompt)
                target = self._active_target(receipt, prompt.appointment_id, targets)
                kept = f"your {when_text(target.start_at, zone)} visit" if target else "your visit"
                return ConversationOutcome(f"OK, I kept {kept}. Nothing was cancelled.")
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
                return ConversationOutcome(
                    "The visit you wanted to move has changed, so nothing was booked. "
                    "Please tell me which visit to move.")
        try:
            result = self._holds.create(CreateHold(
                receipt.business_id, receipt.client_id, receipt.client_id, receipt.provider_id,
                start, profile.default_duration_minutes,
                original.appointment_id if original is not None else None), now)
        except (SlotConflict, InvalidReplacement, ReplacementPending, TooManyConflicts,
                IdempotencyKeyReused, InvalidDuration, ValueError):
            self._forget(prompt)
            return ConversationOutcome(
                f"Sorry, {when_text(start, zone)} is no longer open, so nothing was booked. "
                "Tell me what day works and I'll send the current open times.")
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
        return ConversationOutcome(text, True, hold_id)

    def _cancel_confirmed(self, receipt: InboundReceipt, prompt: ConversationState,
                          targets: tuple[Appointment, ...], zone: ZoneInfo) -> ConversationOutcome:
        self._forget(prompt)
        target = self._active_target(receipt, prompt.appointment_id, targets)
        if (target is None or target.version != prompt.appointment_version
                or receipt.client_id is None):
            return ConversationOutcome(
                "That visit changed since I asked, so nothing was cancelled. "
                "Please tell me again which visit to cancel.")
        try:
            result = self._lifecycle.apply(AppointmentCommand(
                receipt.business_id, target.appointment_id, receipt.client_id,
                ActorRole.CLIENT, Action.CANCEL, receipt.provider_id, target.version))
        except (InvalidTransition, StaleVersion, ReplacementPending,
                TooManyConflicts, IdempotencyKeyReused):
            return ConversationOutcome("That appointment changed. Please review it before retrying.")
        what = "visit" if target.status == CalendarStatus.CONFIRMED else "request"
        return ConversationOutcome(
            f"Cancelled your {when_text(target.start_at, zone)} {what} "
            f"(ref {target.appointment_id[:8]}).", True, result.appointment.appointment_id)

    def _owner_answer(self, receipt: InboundReceipt, targets: tuple[Appointment, ...],
                      now: datetime) -> ConversationOutcome | None:
        """Read-only calendar questions; an open clarifying question gets the first look."""
        questions, body = self._owner_questions, receipt.body or ""
        if questions.awaiting_answer(receipt.business_id, receipt.sender, now):
            text = questions.answer(receipt.business_id, receipt.sender, body, now,
                                    receipt.provider_id)
            if text is not None:
                return ConversationOutcome(text)
        classified = self._classified_owner_reply(receipt, targets, now)
        if classified is not None:
            return classified
        reply = self._owner_reply(receipt, targets)
        if reply is not None:
            return reply
        text = questions.answer(receipt.business_id, receipt.sender, body, now,
                                receipt.provider_id)
        return ConversationOutcome(text) if text is not None else None

    def _classified_owner_reply(self, receipt: InboundReceipt,
                                targets: tuple[Appointment, ...],
                                now: datetime) -> ConversationOutcome | None:
        """Let the model say what an approval-like reply means inside a calendar conversation.

        The model proposes; this method acts only on a confident approve or decline that
        names the request the assistant last asked about, while that request is still the
        single pending one at the same version. Anything else asks and writes nothing.
        """
        questions, body = self._owner_questions, receipt.body or ""
        context = questions.open_context(receipt.business_id, receipt.sender, now)
        if context is None or not targets or not may_be_approval(body):
            return None
        policy = self._repository.read_policy(receipt.business_id)
        zone = ZoneInfo(policy.timezone)
        if questions.is_fresh_question(body, now.astimezone(zone).date()):
            return None  # A complete calendar question; it cannot approve anything.
        # A request counts as named only once the question's reply was SENT, and only for a
        # message our webhook received after that send. A redelivery of the asking message,
        # an earlier message, a pending or failed question, or a lookup error all ask again.
        was_named = (
            bool(context.clarified_request) and context.clarified_by != receipt.provider_id
            and context.clarified_at is not None and receipt.received_at > context.clarified_at
            and self._question_was_sent_before(receipt, context.clarified_by))
        named = next((target for target in targets
                      if was_named and target.appointment_id == context.clarified_request), None)
        if self._owner_classifier is None:
            return self._ask_owner(receipt, targets, now, "I wasn't sure what you meant.")
        try:
            proposal = self._owner_classifier.classify_owner_reply(body, OwnerReplyContext(
                now.astimezone(zone).date(), policy.timezone,
                "approval_question" if named is not None else "calendar_answer",
                context.view.value, context.first, context.last,
                tuple(sorted(status.value for status in context.statuses or ())),
                self._pending_ref(receipt.business_id, named, zone) if named else None,
                tuple(self._pending_ref(receipt.business_id, target, zone)
                      for target in targets[:MAX_CONTEXT_APPOINTMENTS])))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return self._ask_owner(receipt, targets, now, "I couldn't tell what you meant.")
        if proposal.confidence != Confidence.HIGH:
            return self._ask_owner(receipt, targets, now, "I wasn't sure what you meant.")
        if proposal.intent == OwnerReplyIntent.CALENDAR_FOLLOWUP:
            text = questions.apply_followup(
                receipt.business_id, receipt.sender, now, receipt.provider_id,
                proposal.statuses, proposal.range_first, proposal.range_last)
            if text is not None:
                return ConversationOutcome(text)
            return self._ask_owner(receipt, targets, now, "I wasn't sure what you meant.")
        if proposal.intent in (OwnerReplyIntent.APPROVE_NAMED_REQUEST,
                               OwnerReplyIntent.DECLINE_NAMED_REQUEST):
            reference = (proposal.request_reference or "").lower()
            if (named is not None and len(targets) == 1 and named.version == context.clarified_version
                    and reference and named.appointment_id.startswith(reference)
                    and len(reference) >= 8):
                action = (Action.APPROVE if proposal.intent == OwnerReplyIntent.APPROVE_NAMED_REQUEST
                          else Action.DECLINE)
                return self._decide(receipt, named, action)
            return self._ask_owner(
                receipt, targets, now,
                "That request may have changed." if was_named else "I wasn't sure what you meant.")
        return self._ask_owner(receipt, targets, now, "I wasn't sure what you meant.")

    def _question_was_sent_before(self, receipt: InboundReceipt, asked_by: str) -> bool:
        if self._reply_lookup is None:
            return False
        try:
            saved = self._reply_lookup.read_reply_text(receipt.business_id, asked_by)
            sent_at = self._reply_lookup.read_reply_sent_at(receipt.business_id, asked_by)
        except Exception:  # noqa: BLE001 - any lookup failure must fail closed
            return False
        return saved is not None and sent_at is not None and receipt.received_at > sent_at

    def _pending_ref(self, business_id: str, target: Appointment, zone: ZoneInfo) -> PendingRef:
        profile = self._repository.read_profile(business_id, target.client_id)
        name = profile.name.split()[0] if profile is not None and profile.name.split() else "client"
        return PendingRef(target.appointment_id[:8], name, when_text(target.start_at, zone))

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
        return ConversationOutcome(
            f"{lead} {self._pending_summary(receipt.business_id, targets)}")

    def _owner_reply(self, receipt: InboundReceipt,
                     targets: tuple[Appointment, ...]) -> ConversationOutcome | None:
        text = normalized(receipt.body or "")
        if OWNER_APPROVAL.fullmatch(text):
            action = Action.APPROVE
        elif OWNER_DECLINE.fullmatch(text):
            action = Action.DECLINE
        else:
            return None
        if len(targets) != 1:
            return ConversationOutcome(self._pending_summary(receipt.business_id, targets))
        return self._decide(receipt, targets[0], action)

    def _decide(self, receipt: InboundReceipt, target: Appointment,
                action: Action) -> ConversationOutcome:
        try:
            result = self._lifecycle.apply(AppointmentCommand(
                receipt.business_id, target.appointment_id, receipt.sender,
                ActorRole.OWNER, action, receipt.provider_id, target.version))
        except (HoldExpired, InvalidTransition, StaleVersion, SlotConflict,
                TooManyConflicts, IdempotencyKeyReused):
            return ConversationOutcome("That request changed. Please review the current calendar.")
        state = "Approved" if action == Action.APPROVE else "Declined"
        return ConversationOutcome(
            f"{state}: {self._request_line(receipt.business_id, target)}.", True,
            result.appointment.appointment_id)

    def _request_line(self, business_id: str, target: Appointment) -> str:
        zone = ZoneInfo(self._repository.read_policy(business_id).timezone)
        profile = self._repository.read_profile(business_id, target.client_id)
        name = profile.name if profile is not None else "client"
        return f"{name}, {when_text(target.start_at, zone)} (ref {target.appointment_id[:8]})"

    def _pending_summary(self, business_id: str, targets: tuple[Appointment, ...]) -> str:
        if not targets:
            return "No request is waiting for approval right now, so nothing changed."
        if len(targets) == 1:
            return (f"Reply YES to approve or DECLINE to decline: "
                    f"{self._request_line(business_id, targets[0])}.")
        shown = "; ".join(self._request_line(business_id, target) for target in targets[:3])
        more = f"; and {len(targets) - 3} more" if len(targets) > 3 else ""
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
            return ConversationOutcome(question or "Which visit would you like to cancel?")
        return self._confirm_cancel(receipt, target, zone, now)

    def _confirm_cancel(self, receipt: InboundReceipt, target: Appointment, zone: ZoneInfo,
                        now: datetime) -> ConversationOutcome:
        self._remember(receipt, PromptKind.CONFIRM_CANCEL, now, appointment=target)
        what = ("visit" if target.status == CalendarStatus.CONFIRMED
                else "request (still pending approval)")
        return ConversationOutcome(
            f"Cancel your {when_text(target.start_at, zone)} {what}? Reply YES to confirm.")

    def _ask_day(self, receipt: InboundReceipt, target: Appointment, zone: ZoneInfo,
                 now: datetime) -> ConversationOutcome:
        self._remember(receipt, PromptKind.RESCHEDULE_DAY, now, appointment=target)
        return ConversationOutcome(
            f"What day would you like instead of your {when_text(target.start_at, zone)} "
            "visit? It stays booked until the owner approves a new time.")

    def _ask_reschedule(self, receipt: InboundReceipt, proposal: MessageProposal,
                        targets: tuple[Appointment, ...], policy: AvailabilityPolicy,
                        now: datetime) -> ConversationOutcome:
        zone = ZoneInfo(policy.timezone)
        target, question = self._resolve_target(
            receipt, proposal, targets, zone, "move", (CalendarStatus.CONFIRMED,), now)
        if target is None:
            if any(item.status == CalendarStatus.PENDING_APPROVAL for item in targets) and \
                    not any(item.status == CalendarStatus.CONFIRMED for item in targets):
                return ConversationOutcome(
                    "Your request is still pending owner approval. Rescheduling needs a "
                    "confirmed visit; you can cancel the request and ask for another time.")
            return ConversationOutcome(question or "Which visit would you like to move?")
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
            return ConversationOutcome(self._clarify(SenderRole.CLIENT, "clarify"))
        try:
            first = date.fromisoformat(proposal.date_from)
            last = date.fromisoformat(proposal.date_to or proposal.date_from)
            earliest = time.fromisoformat(proposal.time_from) if proposal.time_from else None
            latest = time.fromisoformat(proposal.time_to) if proposal.time_to else None
            if (earliest is None) != (latest is None):  # "After 2" or "before noon".
                earliest, latest = earliest or time(0), latest or time(23, 59)
        except ValueError:
            return ConversationOutcome("I couldn't tell which day you meant. What date works for you?")
        if last < first or (earliest is not None and latest is not None and latest < earliest):
            return ConversationOutcome("I couldn't tell which day you meant. What date works for you?")
        return self._offer(receipt, profile, policy, now, first, last, earliest, latest, original)

    def _offer(self, receipt: InboundReceipt, profile: ClientProfile, policy: AvailabilityPolicy,
               now: datetime, first: date, last: date, earliest: time | None,
               latest: time | None, original: Appointment | None) -> ConversationOutcome:
        """Offer real open times. Offering writes nothing to the calendar."""
        zone = ZoneInfo(policy.timezone)
        today = now.astimezone(zone).date()
        horizon = today + timedelta(days=policy.booking_horizon_days)
        if last < today:
            return ConversationOutcome("That date has passed. What day works for you?")
        if first > horizon:
            return ConversationOutcome(
                f"I can book up to {policy.booking_horizon_days} days ahead, through "
                f"{day_text(horizon)}. What day in that range works for you?")
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
            return ConversationOutcome("Please contact the owner to schedule this visit.")
        span = (f"on {day_text(first)}" if first == last
                else f"between {day_text(first)} and {day_text(last)}")
        if not starts:
            return ConversationOutcome(f"I don't have any openings {span}. Would another day work?")
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
        return ConversationOutcome(
            f"{note}{lead}{body} The owner approves every request.{keeps} "
            "This offer is good for 30 minutes.")
