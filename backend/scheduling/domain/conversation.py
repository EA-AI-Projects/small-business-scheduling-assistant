"""Validated scheduling commands from a trusted, verified SMS receipt.

The interpreter proposes intent only. This layer checks actor, target, date,
current state, and policy before calling the existing transactional services.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
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
from scheduling.domain.sms_ingress import (
    ConsentEvidence,
    InboundReceipt,
    Keyword,
    SenderRole,
    normalize_phone,
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
RESCHEDULE_COMMAND = re.compile(
    rf"^reschedule\s+({REFERENCE})\s+to\s+({DATE_AND_OPTIONAL_TIME})[.!]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MessageContext:
    actor: SenderRole
    today: date
    timezone: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class MessageProposal:
    intent: str
    request_reference: str | None
    date_text: str | None
    owner_decision: str | None
    needs_clarification: bool


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
                 owner_number: str) -> None:
        self._repository = repository
        self._interpreter = interpreter
        self._holds = holds
        self._lifecycle = lifecycle
        self._consent = consent
        self._clock = clock
        self._owner_number = normalize_phone(owner_number)
        self._availability = AvailabilityService(repository)

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
            )
            proposal = self._exact_command(receipt.body, context, targets)
            if proposal is None:
                proposal = self._interpreter.propose(receipt.body, context)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return ConversationOutcome("I couldn't understand that message. Please try again later.")
        if proposal.needs_clarification or proposal.intent in ("clarify", "unsupported"):
            return ConversationOutcome(self._clarify(
                receipt.role, proposal.intent, receipt.body or "", targets))
        if proposal.intent == "owner_decision":
            return self._owner_decision(receipt, proposal, targets)
        if proposal.intent == "cancel":
            return self._cancel(receipt, proposal, targets)
        if proposal.intent in ("request_booking", "reschedule"):
            return self._request(receipt, proposal, targets, policy, now)
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
            return "Reply APPROVE or DECLINE with one exact request reference."
        if re.search(r"\breschedule\b", body, re.IGNORECASE):
            match = re.search(rf"\breschedule\s+({REFERENCE})(?=\s|$)",
                              body, re.IGNORECASE)
            target = (ConversationService._target(body, match.group(1), targets)
                      if match is not None else None)
            if target is not None and target.status != CalendarStatus.CONFIRMED:
                return "That request is pending owner approval. Rescheduling needs a confirmed visit."
            reference = target.appointment_id[:8] if target is not None else "REFERENCE"
            return (f"Reply RESCHEDULE {reference} to YYYY-MM-DD for options, or "
                    f"RESCHEDULE {reference} to YYYY-MM-DD HH:MM to request a time.")
        if re.search(r"\bcancel\b", body, re.IGNORECASE):
            return "Reply CANCEL REFERENCE with one exact confirmed appointment reference."
        if intent == "unsupported":
            return "Please ask for a booking, cancellation, or reschedule."
        return "Reply BOOK YYYY-MM-DD for options, or BOOK YYYY-MM-DD HH:MM to request a time."

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
                if original is None:
                    starts = self._availability.find_starts(
                        receipt.business_id, day, profile.default_duration_minutes, now)
                else:
                    events = tuple(event for event in self._repository.read_calendar(
                        receipt.business_id).events if event.event_id != original.appointment_id)
                    starts = available_starts(policy, day, profile.default_duration_minutes,
                                              events, now)
            except (ValueError, InvalidDuration, InvalidPolicy):
                return ConversationOutcome(
                    f"That date is unavailable. Reply {reply_command} YYYY-MM-DD for options.")
            if not starts:
                return ConversationOutcome(
                    f"No openings on that date. Reply {reply_command} YYYY-MM-DD for options.")
            zone = ZoneInfo(policy.timezone)
            choices = ", ".join(start.astimezone(zone).strftime("%H:%M") for start in starts[:5])
            return ConversationOutcome(
                f"Available starts on {day.isoformat()}: {choices}. "
                f"Reply {reply_command} {day.isoformat()} HH:MM to request one; "
                "owner approval is required.")
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
        if original is not None:
            text = (f"Replacement request {result.hold_id[:8]} is pending owner approval. "
                    f"Your original visit {original.appointment_id[:8]} remains confirmed.")
        else:
            text = f"Request {result.hold_id[:8]} is pending owner approval, not confirmed yet."
        return ConversationOutcome(text, True, result.hold_id)
