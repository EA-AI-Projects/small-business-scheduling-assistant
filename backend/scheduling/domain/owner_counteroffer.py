"""Owner counteroffers: prepare, confirm with the owner, then queue one client text.

The owner asks to offer a pending request's client a different time. This module
resolves the request, shows the owner the exact client-facing text, and queues
that text only on an immediately following plain confirmation. Nothing here
writes the calendar and no model is involved: parsing is deterministic and the
text is built from trusted records. A client's acceptance of the offer (#176)
is handled by ``CounterofferAcceptance`` below, which reads the confirmed offer
through ``CounterofferStore`` and creates one linked pending request.

Preparation and the client send both call ``check_offer`` so eligibility,
consent, request state, and slot availability are verified twice.
"""

import re
import threading
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import (
    AvailabilityPolicy,
    InvalidDuration,
    InvalidPolicy,
    available_starts,
)
from scheduling.domain.calendar import CalendarSnapshot, CalendarStatus
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.conversation_state import (
    AFFIRMATIVE,
    FILLER,
    PROMPT_LIFETIME,
    WEEKDAYS,
    Selection,
    day_text,
    is_affirmative,
    is_negative,
    match_selection,
    normalized,
    when_text,
)
from scheduling.domain.holds import (
    CreateHold,
    IdempotencyKeyReused,
    InvalidReplacement,
    PendingHold,
    ReplacementPending,
    SlotConflict,
    TooManyConflicts,
)
from scheduling.domain.outbox import DeliveryState, OutboxRecord
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt

if TYPE_CHECKING:
    from scheduling.domain.conversation import ConversationOutcome

COUNTEROFFER_TEMPLATE = "owner-counteroffer"
COUNTEROFFER_FAILED_TEMPLATE = "owner-counteroffer-failed"
# Technical default: how long the owner's confirmation prompt stays usable. It is
# the same 30 minutes the client-facing offers use; not a business-validity rule.
CONFIRMATION_LIFETIME = timedelta(minutes=30)
# Decided by the owner (#176, PRD section 6.4): a counteroffer follows the same rules as
# any client offer, so it is open for the shared client-offer lifetime from the moment
# the owner confirms it. Storage TTL (a retention window) never defines it.
CLIENT_OFFER_VALIDITY = PROMPT_LIFETIME
# A YES that arrives after the prompt lapsed is answered (and never approves the
# request) for this long; after that the message is treated as unrelated.
EXPIRED_NOTICE_WINDOW = timedelta(hours=1)

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
INTENT = re.compile(r"\b(?:offer\w*|propos\w*|suggest\w*|counter\w*|instead)\b")
CLOCK = re.compile(
    r"(?<![\w:/-])(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap])\.?m\b\.?"
    r"|(?<![\w:/-])(?P<hour24>[01]?\d|2[0-3]):(?P<minute24>[0-5]\d)(?![\w:])"
    r"|\b(?P<noon>noon)\b")
BARE_NUMBER = re.compile(r"(?<![\w:/-])\d{1,2}(?::\d{2})?(?![\w:/-])")
ISO_DAY = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
MONTH_DAY = re.compile(
    rf"\b({'|'.join(MONTHS)})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
SLASH_DAY = re.compile(r"(?<![\w/])(\d{1,2})/(\d{1,2})(?![\w/])")
REFERENCE = re.compile(r"(?<![0-9a-z])[0-9a-f]{8}(?![0-9a-z])")
# Replies made only of these words read like approving or declining the request, so
# while an offer is open they are never allowed to fall through to approval.
APPROVAL_LIKE_WORDS = AFFIRMATIVE | FILLER | frozenset({
    "approve", "approved", "send", "go", "ahead", "decline", "declined", "reject", "deny",
    "now", "offer", "text", "message", "out", "them", "her", "him", "they"})
NO_TEXT = re.compile(
    r"(?:no|nope|nah|cancel|cancel (?:it|that|the offer)|never ?mind|don'?t send(?: it)?|"
    r"decline|declined|stop|discard)(?: please| thanks)?")


class OfferState(StrEnum):
    PROPOSED = "PROPOSED"    # Shown to the owner; nothing has been queued.
    CONFIRMED = "CONFIRMED"  # Owner confirmed; one client text is queued.
    DISCARDED = "DISCARDED"  # Declined, revised, expired, or failed a check.
    ACCEPTED = "ACCEPTED"    # The client accepted; one linked pending request was created.
    SUPERSEDED = "SUPERSEDED"  # The client declined it, or made a new request instead.


SENT_STATES = frozenset({OfferState.CONFIRMED, OfferState.ACCEPTED, OfferState.SUPERSEDED})


@dataclass(frozen=True)
class Counteroffer:
    """A versioned owner confirmation linked to one pending request.

    ``version`` is 1 when proposed and increments on every state change, so a
    reply that read an older version cannot act. The same record is the offer
    #176 matches a client's acceptance against once it is CONFIRMED.
    """

    business_id: str
    offer_id: str
    owner: str
    request_id: str
    request_version: int
    client_id: str
    client_phone: str
    proposed_start: datetime
    duration_minutes: int
    text: str
    state: OfferState
    version: int
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None  # Provider ID of the owner's confirming message.
    failure: str | None = None  # OfferProblem recorded when the send-time recheck failed.
    accepted_request_id: str | None = None  # The pending request an acceptance created.

    def __post_init__(self) -> None:
        if not all((self.business_id, self.offer_id, self.owner, self.request_id,
                    self.client_id, self.client_phone, self.text)):
            raise ValueError("Counteroffer identity and text are required")
        if self.version <= 0 or self.duration_minutes <= 0:
            raise ValueError("Counteroffer version and duration are invalid")
        for instant in (self.proposed_start, self.created_at, self.expires_at):
            if instant.tzinfo is None:
                raise ValueError("Counteroffer instants must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("Counteroffer lifetime is invalid")
        if (self.state in SENT_STATES) != (self.confirmed_by is not None):
            raise ValueError("Only a confirmed counteroffer names its confirming message")
        if (self.state == OfferState.ACCEPTED) != (self.accepted_request_id is not None):
            raise ValueError("Only an accepted counteroffer names its created request")

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


def offer_id_for(business_id: str, provider_id: str) -> str:
    """Stable for one owner message, so a redelivered SMS cannot prepare a second offer."""
    return "co-" + sha256(f"{business_id}\x00{provider_id}".encode()).hexdigest()[:24]


def outbox_id_for(offer_id: str) -> str:
    return f"counteroffer#{offer_id}"


def failure_outbox_id_for(offer_id: str) -> str:
    return f"counteroffer-failed#{offer_id}"


def confirmed_offer(offer: Counteroffer, confirmed_by: str, now: datetime) -> Counteroffer:
    """The record a confirmation writes; both stores use it so they cannot diverge.

    Its expiry is the client-offer validity (see the constant), not the
    owner prompt lifetime it had while proposed.
    """
    return replace(offer, state=OfferState.CONFIRMED, version=offer.version + 1,
                   confirmed_at=now, confirmed_by=confirmed_by,
                   expires_at=now + CLIENT_OFFER_VALIDITY)


def counteroffer_failure_outbox(offer: Counteroffer, now: datetime) -> OutboxRecord:
    """One owner text explaining a send-time failure; entity is the offer."""
    return OutboxRecord(
        offer.business_id, failure_outbox_id_for(offer.offer_id), offer.offer_id, "owner",
        COUNTEROFFER_FAILED_TEMPLATE, offer.version, DeliveryState.PENDING, now, now, now)


def counteroffer_outbox(offer: Counteroffer, now: datetime) -> OutboxRecord:
    """The one client delivery intent for a confirmed offer; entity is the offer."""
    return OutboxRecord(
        offer.business_id, outbox_id_for(offer.offer_id), offer.offer_id, "client",
        COUNTEROFFER_TEMPLATE, offer.version, DeliveryState.PENDING, now, now, now)


class CounterofferStore(Protocol):
    def read(self, business_id: str, offer_id: str) -> Counteroffer | None: ...
    def read_active(self, business_id: str, owner: str) -> Counteroffer | None:
        """The owner's latest offer, proposed or confirmed, if its record remains."""
        ...
    def read_confirmed_for_client(self, business_id: str,
                                  client_id: str) -> Counteroffer | None:
        """The client's most recently confirmed offer (it may since be accepted or superseded)."""
        ...
    def accept(self, offer: Counteroffer, request_id: str) -> Counteroffer | None:
        """CONFIRMED to ACCEPTED at ``offer.version``, naming the created request; None if lost."""
        ...
    def supersede(self, offer: Counteroffer) -> None:
        """CONFIRMED to SUPERSEDED if still at ``offer.version``; otherwise a no-op."""
        ...
    def put_draft(self, offer: Counteroffer) -> bool:
        """Store a PROPOSED offer as the owner's active one; idempotent per offer ID.

        False, with nothing changed, when that offer ID already finished.
        """
        ...
    def discard(self, offer: Counteroffer) -> None:
        """PROPOSED to DISCARDED if still at ``offer.version``; otherwise a no-op.

        The owner's active pointer stays, so the next reply is still recognised as
        following a cancelled offer (see ``CounterofferService._answer``).
        """
        ...
    def clear_active(self, business_id: str, owner: str, offer_id: str) -> None: ...
    def confirm(self, offer: Counteroffer, confirmed_by: str, now: datetime,
                outbox: OutboxRecord) -> Counteroffer | None:
        """Atomically PROPOSED to CONFIRMED and queue ``outbox``; None if it lost the race."""
        ...
    def record_failure(self, offer: Counteroffer, problem: str, now: datetime,
                       outbox: OutboxRecord) -> None:
        """Insert-if-absent: note the send-time failure and queue the one owner text."""
        ...


class OfferRepository(Protocol):
    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...
    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...
    def read_appointment(self, appointment_id: str) -> Appointment | None: ...


class OfferConsent(Protocol):
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool: ...
    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None: ...


class OfferProblem(StrEnum):
    REQUEST_CHANGED = "REQUEST_CHANGED"
    CLIENT_INELIGIBLE = "CLIENT_INELIGIBLE"
    NO_CONSENT = "NO_CONSENT"
    SLOT_UNAVAILABLE = "SLOT_UNAVAILABLE"
    SAME_TIME = "SAME_TIME"


PROBLEM_TEXT = {
    OfferProblem.REQUEST_CHANGED:
        "That request is no longer pending as it was (it was approved, declined, changed, "
        "or it expired).",
    OfferProblem.CLIENT_INELIGIBLE: "That client profile is no longer active and verified.",
    OfferProblem.NO_CONSENT: "That client has not consented to texts or has opted out.",
    OfferProblem.SLOT_UNAVAILABLE: "That time is not open on the calendar.",
    OfferProblem.SAME_TIME: "That is the time the client already requested.",
}


def check_offer(offer: Counteroffer, repository: OfferRepository, consent: OfferConsent,
                now: datetime) -> OfferProblem | None:
    """Current-state gate used when preparing and again before the client send."""
    request = repository.read_appointment(offer.request_id)
    if (request is None or request.business_id != offer.business_id
            or request.client_id != offer.client_id
            or request.status != CalendarStatus.PENDING_APPROVAL
            or request.version != offer.request_version
            or request.replaces_appointment_id is not None
            or request.hold_expires_at is None or request.hold_expires_at <= now):
        return OfferProblem.REQUEST_CHANGED
    profile = repository.read_profile(offer.business_id, offer.client_id)
    if (profile is None or not profile.active or profile.phone_verified_at is None
            or profile.phone_e164 != offer.client_phone):
        return OfferProblem.CLIENT_INELIGIBLE
    evidence = consent.read_consent(offer.business_id, profile.phone_e164)
    if (evidence is None or evidence.client_id != profile.client_id
            or consent.is_opted_out(offer.business_id, profile.phone_e164)):
        return OfferProblem.NO_CONSENT
    if offer.proposed_start == request.start_at:
        return OfferProblem.SAME_TIME
    try:
        policy = repository.read_policy(offer.business_id)
        day = offer.proposed_start.astimezone(ZoneInfo(policy.timezone)).date()
        # The request being countered stays pending, so it does not block its own offer.
        events = tuple(event for event in repository.read_calendar(offer.business_id).events
                       if event.event_id != request.appointment_id)
        starts = available_starts(policy, day, request.duration_minutes, events, now)
    except (InvalidDuration, InvalidPolicy, ValueError):
        return OfferProblem.SLOT_UNAVAILABLE
    return None if offer.proposed_start in starts else OfferProblem.SLOT_UNAVAILABLE


def offer_text(original_start: datetime, proposed_start: datetime, zone: ZoneInfo) -> str:
    """The exact client-facing wording; the owner sees this before anything is sent."""
    return (f"We can't do your cleaning request for {when_text(original_start, zone)}. "
            f"Could {when_text(proposed_start, zone)} work instead? Reply YES to request "
            "that time. The owner still has to approve it, so it is not confirmed yet.")


@dataclass(frozen=True)
class _Ask:
    """The owner's message could not be turned into one offer; ``question`` says why."""

    question: str


@dataclass(frozen=True)
class _Request:
    request: Appointment
    clock: time
    day: date


def _parse_clock(text: str) -> time | _Ask | None:
    """A single clock time, a question, or None when the text names no time at all."""
    found: set[time] = set()
    for match in CLOCK.finditer(text):
        if match["noon"]:
            found.add(time(12, 0))
        elif match["meridiem"]:
            hour = int(match["hour"])
            if not 1 <= hour <= 12:
                return _Ask("I couldn't read that time. Please say it like 2:00 PM.")
            found.add(time(hour % 12 + (12 if match["meridiem"] == "p" else 0),
                           int(match["minute"] or 0)))
        else:
            found.add(time(int(match["hour24"]), int(match["minute24"])))
    if len(found) > 1:
        return _Ask("I saw more than one time. Which single time should I offer?")
    if found:
        return next(iter(found))
    return None


def _parse_days(text: str, today: date) -> tuple[date, ...] | _Ask:
    days: set[date] = set()
    try:
        for match in ISO_DAY.finditer(text):
            days.add(date(int(match[1]), int(match[2]), int(match[3])))
        for match in MONTH_DAY.finditer(text):
            month, number = MONTHS.index(match[1]) + 1, int(match[2])
            candidate = date(today.year, month, number)
            days.add(candidate if candidate >= today else date(today.year + 1, month, number))
        for match in SLASH_DAY.finditer(text):
            candidate = date(today.year, int(match[1]), int(match[2]))
            days.add(candidate if candidate >= today
                     else date(today.year + 1, int(match[1]), int(match[2])))
    except ValueError:
        return _Ask("I couldn't read that date. Please say it like Fri Oct 9.")
    words = re.findall(r"[a-z]+", text)
    for word in words:
        if word == "today":
            days.add(today)
        elif word == "tomorrow":
            days.add(today + timedelta(days=1))
        elif word in WEEKDAYS:
            ahead = (WEEKDAYS.index(word) - today.weekday()) % 7 or 7
            days.add(today + timedelta(days=ahead))
    if len(days) > 1:
        return _Ask("I saw more than one date. Which date should I offer?")
    return tuple(days)


_HANDLED = ("That offer was already handled, so nothing more was sent. The request was "
            "not approved.")


class CounterofferService:
    """Owner SMS handling for counteroffers; every other owner message returns None."""

    def __init__(self, repository: OfferRepository, consent: OfferConsent,
                 store: CounterofferStore, owner_number: str) -> None:
        self._repository = repository
        self._consent = consent
        self._store = store
        self._owner = owner_number

    def handle(self, receipt: InboundReceipt, now: datetime,
               pending: tuple[Appointment, ...]) -> "ConversationOutcome | None":
        from scheduling.domain.conversation import ConversationOutcome

        body = receipt.body or ""
        text = normalized(body)
        active = self._store.read_active(receipt.business_id, receipt.sender)
        if active is not None and now >= active.expires_at + EXPIRED_NOTICE_WINDOW:
            active = None
        # Same owner message delivered twice: answer the same way, never act twice.
        if active is not None and active.confirmed_by == receipt.provider_id:
            return ConversationOutcome(self._queued_text(active))
        if active is not None and active.offer_id == offer_id_for(
                receipt.business_id, receipt.provider_id):
            if active.state == OfferState.PROPOSED and not active.expired(now):
                return ConversationOutcome(self._prompt(active, receipt.business_id))
            return ConversationOutcome(_HANDLED)
        parsed = self._parse(text, now, receipt.business_id, pending)
        if active is not None:
            answered = self._answer(active, receipt, text, now, pending, parsed)
            if answered is not None:
                return answered
        if parsed is None:
            return None
        if isinstance(parsed, _Ask):
            return ConversationOutcome(parsed.question)
        return self._prepare(receipt, now, parsed)

    @staticmethod
    def _approval_like(text: str) -> bool:
        from scheduling.domain.conversation import OWNER_APPROVAL, OWNER_DECLINE

        if OWNER_APPROVAL.fullmatch(text) or OWNER_DECLINE.fullmatch(text):
            return True
        words = re.findall(r"[a-z']+|\d+", text)
        return bool(words) and all(word in APPROVAL_LIKE_WORDS for word in words)

    # An active offer consumes exactly the next owner message. A plain YES confirms a
    # still-proposed offer; NO discards it; a new instruction replaces it. Replies that
    # read like approving, declining, or sending (including "yes send the offer") change
    # nothing and explain what to reply, so they can never approve what the owner just
    # countered. Only an exact "APPROVE <ref>" or "DECLINE <ref>" passes through. Any
    # other reply cancels the offer and says so.
    # The pointer outlives the offer (confirmed, failed, cancelled, or expired) for
    # EXPIRED_NOTICE_WINDOW: while the same request is still pending, a bare yes/ok/
    # approval-like reply is absorbed ("nothing was approved") and writes nothing. Any
    # other message clears the pointer.
    def _answer(self, active: Counteroffer, receipt: InboundReceipt, text: str,
                now: datetime, pending: tuple[Appointment, ...],
                parsed: "_Request | _Ask | None") -> "ConversationOutcome | None":
        from scheduling.domain.conversation import EXACT_COMMAND, ConversationOutcome

        yes = is_affirmative(text)
        no = is_negative(text) or bool(NO_TEXT.fullmatch(text))
        like = not yes and not no and self._approval_like(text)
        ref = active.request_id[:8]
        how = f"To approve the original request, reply APPROVE {ref}."
        still_pending = any(item.appointment_id == active.request_id
                            and item.version == active.request_version for item in pending)
        if active.state != OfferState.PROPOSED:
            if not still_pending:
                self._store.clear_active(active.business_id, active.owner, active.offer_id)
                return None
            if yes or like or no:
                if active.failure is not None:
                    reason = PROBLEM_TEXT[OfferProblem(active.failure)]
                    return ConversationOutcome(
                        f"That offer could not be sent ({reason.rstrip('.')}), so nothing was "
                        f"sent and nothing was approved. Tell me another time to offer. {how}")
                if active.state == OfferState.ACCEPTED:
                    return ConversationOutcome(
                        "The client already accepted that offer, so a new request is waiting "
                        "for your approval (ref "
                        f"{(active.accepted_request_id or '')[:8]}). Nothing was approved by "
                        "this reply. Reply APPROVE or DECLINE with that reference.")
                if active.state == OfferState.CONFIRMED:
                    return ConversationOutcome(
                        "That offer was already queued, so nothing more was sent. The request "
                        f"is still pending and was not approved. {how}")
                return ConversationOutcome(
                    f"Nothing was approved or sent. The request is still pending. {how}")
            self._store.clear_active(active.business_id, active.owner, active.offer_id)
            return None
        if yes:
            if active.expired(now):
                self._store.discard(active)
                return ConversationOutcome(
                    "That offer expired after 30 minutes, so nothing was sent and the request "
                    f"was not approved. Tell me the time to offer and I'll prepare it again. {how}")
            return self._confirm(active, receipt, now)
        if like:
            if active.expired(now):
                self._store.discard(active)
                return ConversationOutcome(
                    f"That offer expired, so nothing was sent and the request was not approved. {how}")
            return ConversationOutcome(
                f"Reply YES to send exactly that offer, or NO to cancel it. {how}")
        self._store.discard(active)
        if no:
            return ConversationOutcome(
                f"OK, I cancelled the offer to {self._client_name(active)}; nothing was sent. "
                f"The request is still pending. {how}")
        if parsed is not None or EXACT_COMMAND.fullmatch((receipt.body or "").strip()):
            self._store.clear_active(active.business_id, active.owner, active.offer_id)
            return None  # A revised instruction, or an exact approve/decline command.
        return ConversationOutcome(
            f"I cancelled the offer to {self._client_name(active)}; nothing was sent. "
            "The request is still pending.")

    def _confirm(self, active: Counteroffer, receipt: InboundReceipt,
                 now: datetime) -> "ConversationOutcome":
        from scheduling.domain.conversation import ConversationOutcome

        problem = check_offer(active, self._repository, self._consent, now)
        if problem is not None:
            self._store.discard(active)
            return ConversationOutcome(
                f"I did not send it. {PROBLEM_TEXT[problem]} Tell me another time to offer.")
        outbox = counteroffer_outbox(confirmed_offer(active, receipt.provider_id, now), now)
        confirmed = self._store.confirm(active, receipt.provider_id, now, outbox)
        if confirmed is None:
            return ConversationOutcome(_HANDLED)
        return ConversationOutcome(self._queued_text(confirmed))

    def _queued_text(self, offer: Counteroffer) -> str:
        zone = ZoneInfo(self._repository.read_policy(offer.business_id).timezone)
        return (f"Queued the offer to {self._client_name(offer)} for "
                f"{when_text(offer.proposed_start, zone)}. It goes out shortly, and I'll tell "
                "you if it can't be sent. The original request is still pending and was not "
                "approved. If they accept, it comes back to you for approval.")

    def _client_name(self, offer: Counteroffer) -> str:
        profile = self._repository.read_profile(offer.business_id, offer.client_id)
        return profile.name if profile is not None else "the client"

    def _prompt(self, offer: Counteroffer, business_id: str) -> str:
        zone = ZoneInfo(self._repository.read_policy(business_id).timezone)
        return (f"Offer for {self._client_name(offer)}, "
                f"{when_text(offer.proposed_start, zone)}. Text I would send: "
                f"\"{offer.text}\" Reply YES to send exactly this, or NO to cancel. "
                "Nothing is sent until you reply YES, and the request stays pending.")

    def _parse(self, text: str, now: datetime, business_id: str,
               pending: tuple[Appointment, ...]) -> "_Request | _Ask | None":
        if not text or not INTENT.search(text):
            return None
        clock = _parse_clock(text)
        if isinstance(clock, _Ask):
            return clock
        if clock is None:
            undated = MONTH_DAY.sub(" ", SLASH_DAY.sub(" ", ISO_DAY.sub(" ", text)))
            if BARE_NUMBER.search(undated) is None:
                return None
            return _Ask("Please include AM or PM with the time, for example 2:00 PM. "
                        "Nothing was sent.")
        zone = ZoneInfo(self._repository.read_policy(business_id).timezone)
        days = _parse_days(CLOCK.sub(" ", text), now.astimezone(zone).date())
        if isinstance(days, _Ask):
            return days
        requests = tuple(request for request in pending
                         if request.replaces_appointment_id is None)
        if not requests:
            return _Ask("No request is waiting for approval right now, so I have "
                        "nothing to counter. Nothing was sent.")
        chosen = self._select(text, requests, days, zone)
        if isinstance(chosen, _Ask):
            return chosen
        day = days[0] if days else chosen.start_at.astimezone(zone).date()
        return _Request(chosen, clock, day)

    def _select(self, text: str, requests: tuple[Appointment, ...], days: tuple[date, ...],
                zone: ZoneInfo) -> "Appointment | _Ask":
        references = {match for match in REFERENCE.findall(text)}
        if references:
            named = [r for r in requests if r.appointment_id[:8] in references]
            if len(named) == 1 and len(references) == 1:
                return named[0]
            return _Ask(self._which(requests, zone, "I couldn't match that reference."))
        words = set(re.findall(r"[a-z]{3,}", text))
        by_name = [r for r in requests
                   if (profile := self._repository.read_profile(r.business_id, r.client_id))
                   is not None and words & set(re.findall(r"[a-z]{3,}", profile.name.lower()))]
        if len(by_name) == 1:
            return by_name[0]
        if len(requests) == 1:
            return requests[0]
        if days:
            same_day = [r for r in requests if r.start_at.astimezone(zone).date() == days[0]]
            if len(same_day) == 1:
                return same_day[0]
        return _Ask(self._which(requests, zone, "Which request do you mean?"))

    def _which(self, requests: tuple[Appointment, ...], zone: ZoneInfo, lead: str) -> str:
        shown = "; ".join(
            f"{self._line(r, zone)}" for r in requests[:4])
        more = f"; and {len(requests) - 4} more" if len(requests) > 4 else ""
        return (f"{lead} Pending: {shown}{more}. Nothing was sent. Say it again with the "
                "reference, like: Offer 2:00 PM instead for ref " + requests[0].appointment_id[:8]
                + ".")

    def _line(self, request: Appointment, zone: ZoneInfo) -> str:
        profile = self._repository.read_profile(request.business_id, request.client_id)
        name = profile.name if profile is not None else "client"
        return f"{name}, {when_text(request.start_at, zone)} (ref {request.appointment_id[:8]})"

    def _prepare(self, receipt: InboundReceipt, now: datetime,
                 parsed: "_Request") -> "ConversationOutcome":
        from scheduling.domain.conversation import ConversationOutcome

        request = parsed.request
        policy = self._repository.read_policy(receipt.business_id)
        zone = ZoneInfo(policy.timezone)
        wall = datetime.combine(parsed.day, parsed.clock)
        instants = {candidate.astimezone(UTC) for fold in (0, 1)
                    if (candidate := wall.replace(tzinfo=zone, fold=fold))
                    .astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall}
        if len(instants) != 1:
            return ConversationOutcome(
                "That local time is missing or ambiguous. Tell me another time to offer.")
        start = next(iter(instants))
        profile = self._repository.read_profile(request.business_id, request.client_id)
        if profile is None:
            return ConversationOutcome(
                f"I did not prepare it. {PROBLEM_TEXT[OfferProblem.CLIENT_INELIGIBLE]} "
                "Nothing was sent.")
        draft = Counteroffer(
            receipt.business_id, offer_id_for(receipt.business_id, receipt.provider_id),
            receipt.sender, request.appointment_id, request.version, request.client_id,
            profile.phone_e164, start, request.duration_minutes,
            offer_text(request.start_at, start, zone), OfferState.PROPOSED, 1, now,
            now + CONFIRMATION_LIFETIME)
        problem = check_offer(draft, self._repository, self._consent, now)
        if problem is not None:
            return ConversationOutcome(
                f"I did not prepare an offer for {day_text(parsed.day)}. "
                f"{PROBLEM_TEXT[problem]} Nothing was sent. Tell me another time to offer.")
        if not self._store.put_draft(draft):
            return ConversationOutcome(_HANDLED)
        return ConversationOutcome(self._prompt(draft, receipt.business_id))


class HoldCreator(Protocol):
    def create(self, command: CreateHold, now: datetime) -> PendingHold: ...


def acceptance_key(offer_id: str) -> str:
    """One idempotency key per offer, so a repeated YES can never create a second request."""
    return f"counteroffer-accept#{offer_id}"


class CounterofferAcceptance:
    """A client's reply to an owner counteroffer.

    Only the client's own current, unexpired, confirmed offer can be accepted, by a
    plain yes (or the offered time). Acceptance rechecks consent, the source request's
    state and version, and availability, then creates exactly one pending request linked
    to the original through the existing replacement hold. It never confirms anything:
    the owner still approves, and the original stays pending until then.
    """

    def __init__(self, repository: OfferRepository, consent: OfferConsent,
                 store: CounterofferStore, holds: HoldCreator) -> None:
        self._repository = repository
        self._consent = consent
        self._store = store
        self._holds = holds

    def _open_offer(self, receipt: InboundReceipt) -> Counteroffer | None:
        if receipt.client_id is None:
            return None
        offer = self._store.read_confirmed_for_client(receipt.business_id, receipt.client_id)
        if (offer is None or offer.client_id != receipt.client_id
                or offer.client_phone != receipt.sender):
            return None
        return offer

    def supersede_for_new_request(self, receipt: InboundReceipt) -> None:
        """A new client request replaces the open offer, as it replaces any other prompt."""
        offer = self._open_offer(receipt)
        if offer is not None and offer.state == OfferState.CONFIRMED:
            self._store.supersede(offer)

    def handle(self, receipt: InboundReceipt, now: datetime) -> "ConversationOutcome | None":
        from scheduling.domain.conversation import ConversationOutcome

        offer = self._open_offer(receipt)
        if offer is None or offer.state not in (OfferState.CONFIRMED, OfferState.ACCEPTED):
            return None
        body = receipt.body or ""
        zone = ZoneInfo(self._repository.read_policy(receipt.business_id).timezone)
        selection, _ = match_selection(
            body, (offer.proposed_start,), zone, now.astimezone(zone).date())
        negative = is_negative(body)
        if offer.state == OfferState.ACCEPTED:
            if selection != Selection.MATCH:
                return None
            return ConversationOutcome(
                f"You already asked for {when_text(offer.proposed_start, zone)} (ref "
                f"{(offer.accepted_request_id or '')[:8]}). It's pending owner approval, "
                "so nothing more was requested.")
        if selection == Selection.NONE and not negative:
            return None
        if offer.expired(now):
            self._store.supersede(offer)
            return ConversationOutcome(
                "That offer expired after 30 minutes, so nothing changed. Tell me what day "
                "works and I'll send the current open times.")
        if negative:
            self._store.supersede(offer)
            return ConversationOutcome(
                "OK, I won't request that time. Nothing was requested, and your original "
                "request is unchanged.")
        if selection == Selection.AMBIGUOUS:
            return ConversationOutcome(
                "I couldn't tell if you meant that time, so nothing was requested. Reply YES "
                "to request it, or tell me another day.")
        if selection == Selection.UNMATCHED:
            return ConversationOutcome(
                "That isn't the time offered, so nothing was requested. Reply YES to request "
                f"{when_text(offer.proposed_start, zone)}, or tell me another day.")
        return self._accept(offer, now, zone)

    def _accept(self, offer: Counteroffer, now: datetime,
                zone: ZoneInfo) -> "ConversationOutcome":
        from scheduling.domain.conversation import ConversationOutcome

        gone = ConversationOutcome(
            "Sorry, that offer is no longer available, so nothing was requested. Tell me what "
            "day works and I'll send the current open times.")
        if check_offer(offer, self._repository, self._consent, now) is not None:
            return gone
        try:
            hold = self._holds.create(CreateHold(
                offer.business_id, offer.client_id, offer.client_id,
                acceptance_key(offer.offer_id), offer.proposed_start, offer.duration_minutes,
                offer.request_id, offer.request_version), now)
        except (SlotConflict, InvalidReplacement, ReplacementPending, TooManyConflicts,
                IdempotencyKeyReused, InvalidDuration, InvalidPolicy, ValueError):
            return ConversationOutcome(
                f"Sorry, {when_text(offer.proposed_start, zone)} is no longer open, so nothing "
                "was requested. Tell me what day works and I'll send the current open times.")
        self._store.accept(offer, hold.hold_id)
        original = self._repository.read_appointment(offer.request_id)
        kept = (f" Your earlier request for {when_text(original.start_at, zone)} (ref "
                f"{original.appointment_id[:8]}) stays pending until then."
                if original is not None else "")
        return ConversationOutcome(
            f"Requested {when_text(hold.start_at, zone)} (ref {hold.hold_id[:8]}). It's "
            f"pending owner approval, not confirmed yet.{kept}", True, hold.hold_id)


class InMemoryCounterofferStore:
    """Process-local store with the all-or-nothing contract of the DynamoDB one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._offers: dict[tuple[str, str], Counteroffer] = {}
        self._active: dict[tuple[str, str], str] = {}
        self._by_client: dict[tuple[str, str], str] = {}
        self.outbox: dict[str, OutboxRecord] = {}

    def read(self, business_id: str, offer_id: str) -> Counteroffer | None:
        with self._lock:
            return self._offers.get((business_id, offer_id))

    def read_active(self, business_id: str, owner: str) -> Counteroffer | None:
        with self._lock:
            offer_id = self._active.get((business_id, owner))
            return self._offers.get((business_id, offer_id)) if offer_id else None

    def read_confirmed_for_client(self, business_id: str,
                                  client_id: str) -> Counteroffer | None:
        with self._lock:
            offer_id = self._by_client.get((business_id, client_id))
            return self._offers.get((business_id, offer_id)) if offer_id else None

    def put_draft(self, offer: Counteroffer) -> bool:
        with self._lock:
            key = (offer.business_id, offer.offer_id)
            existing = self._offers.get(key)
            if existing is not None and existing.state != OfferState.PROPOSED:
                return False
            self._offers[key] = offer
            self._active[(offer.business_id, offer.owner)] = offer.offer_id
            return True

    def discard(self, offer: Counteroffer) -> None:
        with self._lock:
            key = (offer.business_id, offer.offer_id)
            current = self._offers.get(key)
            if (current is not None and current.state == OfferState.PROPOSED
                    and current.version == offer.version):
                self._offers[key] = replace(current, state=OfferState.DISCARDED,
                                            version=current.version + 1)

    def clear_active(self, business_id: str, owner: str, offer_id: str) -> None:
        with self._lock:
            if self._active.get((business_id, owner)) == offer_id:
                del self._active[(business_id, owner)]

    def confirm(self, offer: Counteroffer, confirmed_by: str, now: datetime,
                outbox: OutboxRecord) -> Counteroffer | None:
        with self._lock:
            key = (offer.business_id, offer.offer_id)
            current = self._offers.get(key)
            if (current is None or current.state != OfferState.PROPOSED
                    or current.version != offer.version
                    or outbox.entity_id != offer.offer_id):
                return None
            confirmed = confirmed_offer(current, confirmed_by, now)
            self._offers[key] = confirmed
            self.outbox.setdefault(outbox.outbox_id, outbox)
            self._by_client[(offer.business_id, offer.client_id)] = offer.offer_id
            return confirmed

    def accept(self, offer: Counteroffer, request_id: str) -> Counteroffer | None:
        with self._lock:
            key = (offer.business_id, offer.offer_id)
            current = self._offers.get(key)
            if (current is None or current.state != OfferState.CONFIRMED
                    or current.version != offer.version):
                return None
            accepted = replace(current, state=OfferState.ACCEPTED, version=current.version + 1,
                               accepted_request_id=request_id)
            self._offers[key] = accepted
            return accepted

    def supersede(self, offer: Counteroffer) -> None:
        with self._lock:
            key = (offer.business_id, offer.offer_id)
            current = self._offers.get(key)
            if (current is not None and current.state == OfferState.CONFIRMED
                    and current.version == offer.version):
                self._offers[key] = replace(current, state=OfferState.SUPERSEDED,
                                            version=current.version + 1)

    def record_failure(self, offer: Counteroffer, problem: str, now: datetime,
                       outbox: OutboxRecord) -> None:
        with self._lock:
            key = (offer.business_id, offer.offer_id)
            current = self._offers.get(key)
            if (current is None or current.failure is not None
                    or outbox.entity_id != offer.offer_id):
                return
            self._offers[key] = replace(current, failure=problem)
            self.outbox.setdefault(outbox.outbox_id, outbox)
