"""Validated owner counteroffer tools and client acceptance.

A draft stores the model-written client text, but sends nothing. Sending rechecks
eligibility, consent, request state, and availability and queues one client outbox.
"""

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
    PROMPT_LIFETIME,
    Selection,
    is_negative,
    match_selection,
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
        if self.state == OfferState.ACCEPTED and self.accepted_request_id is None:
            raise ValueError("An accepted counteroffer names its created request")
        if self.state in (OfferState.PROPOSED, OfferState.CONFIRMED, OfferState.DISCARDED) and \
                self.accepted_request_id is not None:
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
        """CONFIRMED or ACCEPTED to SUPERSEDED if still at ``offer.version``; else a no-op."""
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


class CounterofferService:
    """Backend gates for model-chosen owner counteroffer tools."""

    def __init__(self, repository: OfferRepository, consent: OfferConsent,
                 store: CounterofferStore, owner_number: str) -> None:
        self._repository = repository
        self._consent = consent
        self._store = store
        self._owner = owner_number

    def _facts(self, offer: Counteroffer) -> dict[str, object]:
        zone = ZoneInfo(self._repository.read_policy(offer.business_id).timezone)
        profile = self._repository.read_profile(offer.business_id, offer.client_id)
        name = (profile.name.split()[0] if profile is not None and profile.name.split()
                else "client")
        return {"draft_id": offer.offer_id, "ref": offer.request_id[:8],
                "version": offer.request_version, "client": name,
                "time": when_text(offer.proposed_start, zone), "client_text": offer.text,
                "expires_at": offer.expires_at.isoformat()}

    def open_offer(self, business_id: str, owner: str, now: datetime,
                   pending: tuple[Appointment, ...]) -> Counteroffer | None:
        active = self._store.read_active(business_id, owner)
        if active is None or active.owner != owner or active.business_id != business_id:
            return None
        if active.state != OfferState.PROPOSED or active.expired(now):
            return None
        if not any(item.appointment_id == active.request_id
                   and item.version == active.request_version for item in pending):
            return None
        return active

    def open_facts(self, business_id: str, owner: str, now: datetime,
                   pending: tuple[Appointment, ...]) -> dict[str, object] | None:
        offer = self.open_offer(business_id, owner, now, pending)
        return self._facts(offer) if offer is not None else None

    @staticmethod
    def _newer_request(offer: Counteroffer, pending: tuple[Appointment, ...]) -> bool:
        return any(item.appointment_id != offer.request_id
                   and (item.created_at is None or item.created_at > offer.created_at)
                   for item in pending)

    def draft_counteroffer(self, receipt: InboundReceipt, now: datetime,
                           pending: tuple[Appointment, ...], ref: str, version: int,
                           day: date, clock: time, client_text: str) -> dict[str, object]:
        """Validate current state, then store exactly the model's client text without sending."""
        if receipt.sender != self._owner:
            return {"ok": False, "error": "owner_mismatch"}
        matches = [item for item in pending if ref.lower() in
                   (item.appointment_id[:8], item.appointment_id)
                   and item.replaces_appointment_id is None]
        if len(matches) != 1:
            return {"ok": False, "error": "not_found"}
        request = matches[0]
        if request.version != version:
            return {"ok": False, "error": "stale_version",
                    "current_version": request.version}
        zone = ZoneInfo(self._repository.read_policy(receipt.business_id).timezone)
        wall = datetime.combine(day, clock)
        instants = {candidate.astimezone(UTC) for fold in (0, 1)
                    if (candidate := wall.replace(tzinfo=zone, fold=fold))
                    .astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall}
        if len(instants) != 1:
            return {"ok": False, "error": "invalid_time"}
        profile = self._repository.read_profile(receipt.business_id, request.client_id)
        if profile is None:
            return {"ok": False, "error": OfferProblem.CLIENT_INELIGIBLE.value.lower()}
        draft = Counteroffer(
            receipt.business_id, offer_id_for(receipt.business_id, receipt.provider_id),
            receipt.sender, request.appointment_id, request.version, request.client_id,
            profile.phone_e164, next(iter(instants)), request.duration_minutes,
            client_text, OfferState.PROPOSED, 1, now, now + CONFIRMATION_LIFETIME)
        problem = check_offer(draft, self._repository, self._consent, now)
        if problem is not None:
            return {"ok": False, "error": problem.value.lower()}
        existing = self._store.read(receipt.business_id, draft.offer_id)
        if existing is not None:
            active_existing = self._store.read_active(receipt.business_id, receipt.sender)
            if (existing.state == OfferState.PROPOSED and active_existing is not None
                    and active_existing.offer_id == existing.offer_id):
                return {"ok": True, **self._facts(existing)}
            return {"ok": False, "error": "draft_conflict"}
        active = self._store.read_active(receipt.business_id, receipt.sender)
        if active is not None and active.offer_id != draft.offer_id \
                and active.state == OfferState.PROPOSED:
            self._store.discard(active)
            previous = self._store.read(receipt.business_id, active.offer_id)
            if previous is None or previous.state != OfferState.DISCARDED:
                return {"ok": False, "error": "draft_conflict"}
        if not self._store.put_draft(draft):
            return {"ok": False, "error": "draft_conflict"}
        return {"ok": True, **self._facts(draft)}

    def _owned_draft(self, receipt: InboundReceipt, draft_id: str) -> Counteroffer | None:
        if receipt.sender != self._owner:
            return None
        offer = self._store.read(receipt.business_id, draft_id)
        if offer is None or offer.business_id != receipt.business_id or offer.owner != receipt.sender:
            return None
        return offer

    def send_counteroffer(self, receipt: InboundReceipt, now: datetime,
                          pending: tuple[Appointment, ...], draft_id: str
                          ) -> dict[str, object]:
        """Recheck an owned open draft and atomically queue its exact stored text once."""
        offer = self._owned_draft(receipt, draft_id)
        if offer is None:
            return {"ok": False, "error": "not_found"}
        if offer.state == OfferState.CONFIRMED and offer.confirmed_by == receipt.provider_id:
            return {"ok": True, "already_sent": True, **self._facts(offer)}
        if offer.state != OfferState.PROPOSED:
            return {"ok": False, "error": "draft_not_open"}
        if offer.expired(now):
            self._store.discard(offer)
            return {"ok": False, "error": "expired"}
        active = self._store.read_active(receipt.business_id, receipt.sender)
        if active is None or active.offer_id != draft_id:
            return {"ok": False, "error": "draft_not_open"}
        if self._newer_request(offer, pending):
            return {"ok": False, "error": "newer_request"}
        problem = check_offer(offer, self._repository, self._consent, now)
        if problem is not None:
            self._store.discard(offer)
            return {"ok": False, "error": problem.value.lower()}
        outbox = counteroffer_outbox(confirmed_offer(offer, receipt.provider_id, now), now)
        confirmed = self._store.confirm(offer, receipt.provider_id, now, outbox)
        if confirmed is None:
            return {"ok": False, "error": "draft_conflict"}
        return {"ok": True, "queued": True, **self._facts(confirmed)}

    def cancel_counteroffer(self, receipt: InboundReceipt, now: datetime,
                            draft_id: str) -> dict[str, object]:
        offer = self._owned_draft(receipt, draft_id)
        if offer is None:
            return {"ok": False, "error": "not_found"}
        if offer.state != OfferState.PROPOSED:
            return {"ok": False, "error": "draft_not_open"}
        if offer.expired(now):
            self._store.discard(offer)
            return {"ok": False, "error": "expired"}
        active = self._store.read_active(receipt.business_id, receipt.sender)
        if active is None or active.offer_id != draft_id:
            return {"ok": False, "error": "draft_not_open"}
        self._store.discard(offer)
        current = self._store.read(receipt.business_id, draft_id)
        if current is None or current.state != OfferState.DISCARDED:
            return {"ok": False, "error": "draft_conflict"}
        return {"ok": True, "cancelled": True, "draft_id": draft_id}

    def discard_open_for_decision(self, business_id: str, owner: str) -> None:
        """An exact owner APPROVE/DECLINE command supersedes its open draft."""
        active = self._store.read_active(business_id, owner)
        if active is not None and active.state == OfferState.PROPOSED:
            self._store.discard(active)


class HoldCreator(Protocol):
    def create(self, command: CreateHold, now: datetime) -> PendingHold: ...
    def existing(self, command: CreateHold) -> PendingHold | None:
        """The request this exact command already created, if any (idempotent replay)."""
        ...


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

    def reminder(self, receipt: InboundReceipt, now: datetime) -> str | None:
        """One line naming the client's open offer and what YES does, or None."""
        offer = self._open_offer(receipt)
        if offer is None or offer.state != OfferState.CONFIRMED or offer.expired(now):
            return None
        zone = ZoneInfo(self._repository.read_policy(receipt.business_id).timezone)
        return (f"The owner's offer of {when_text(offer.proposed_start, zone)} is still open: "
                "reply YES to request it, or NO to turn it down.")

    def supersede_for_new_request(self, receipt: InboundReceipt) -> None:
        """A new client request replaces the open offer, as it replaces any other prompt."""
        offer = self._open_offer(receipt)
        if offer is not None and offer.state in (OfferState.CONFIRMED, OfferState.ACCEPTED):
            self._store.supersede(offer)

    def _command(self, offer: Counteroffer) -> CreateHold:
        return CreateHold(
            offer.business_id, offer.client_id, offer.client_id, acceptance_key(offer.offer_id),
            offer.proposed_start, offer.duration_minutes, offer.request_id,
            offer.request_version)

    def handle(self, receipt: InboundReceipt, now: datetime,
               newer_prompt: bool = False) -> "ConversationOutcome | None":
        """``newer_prompt``: the client has an open normal prompt made after the offer."""
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
            # A repeat of the same YES is answered only while it can still be a repeat: the
            # request it created is still pending, the offer's window is open, and the client
            # has no newer prompt. Otherwise the message is handled like any other.
            created = (self._repository.read_appointment(offer.accepted_request_id)
                       if offer.accepted_request_id is not None else None)
            waiting = (created is not None and created.status == CalendarStatus.PENDING_APPROVAL
                       and created.hold_expires_at is not None and created.hold_expires_at > now)
            if selection != Selection.MATCH or not waiting or newer_prompt or offer.expired(now):
                return None
            return ConversationOutcome(
                f"You already asked for {when_text(offer.proposed_start, zone)} (ref "
                f"{(offer.accepted_request_id or '')[:8]}). It's pending owner approval, "
                "so nothing more was requested.")
        if selection == Selection.NONE and not negative:
            return None
        try:
            made = self._holds.existing(self._command(offer))
        except IdempotencyKeyReused:
            made = None
        if made is not None:
            # An earlier delivery created the request but its offer update was lost: repair
            # the offer, then speak only to the request's current state.
            self._store.accept(offer, made.hold_id)
            created = self._repository.read_appointment(made.hold_id)
            if (created is None or created.status != CalendarStatus.PENDING_APPROVAL
                    or created.hold_expires_at is None or created.hold_expires_at <= now
                    or offer.expired(now) or newer_prompt):
                return None  # Decided, expired, or stale: handled like any other message.
            if selection == Selection.MATCH:
                return self._requested(offer, made, zone)
            return ConversationOutcome(
                f"Your request for {when_text(made.start_at, zone)} (ref {made.hold_id[:8]}) "
                "was already made and is pending owner approval. To withdraw it, tell me to "
                "cancel it.")
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
            hold = self._holds.create(self._command(offer), now)
        except (SlotConflict, InvalidReplacement, ReplacementPending, TooManyConflicts,
                IdempotencyKeyReused, InvalidDuration, InvalidPolicy, ValueError):
            return ConversationOutcome(
                f"Sorry, {when_text(offer.proposed_start, zone)} is no longer open, so nothing "
                "was requested. Tell me what day works and I'll send the current open times.")
        self._store.accept(offer, hold.hold_id)
        return self._requested(offer, hold, zone)

    def _requested(self, offer: Counteroffer, hold: PendingHold,
                   zone: ZoneInfo) -> "ConversationOutcome":
        from scheduling.domain.conversation import ConversationOutcome

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
            if existing is not None:
                return False
            self._offers[key] = offer
            self._active[(offer.business_id, offer.owner)] = offer.offer_id
            return True

    def discard(self, offer: Counteroffer) -> None:
        with self._lock:
            key = (offer.business_id, offer.offer_id)
            current = self._offers.get(key)
            if (current is not None and current.state == OfferState.PROPOSED
                    and self._active.get((offer.business_id, offer.owner)) == offer.offer_id
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
                    or self._active.get((offer.business_id, offer.owner)) != offer.offer_id
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
            if (current is not None
                    and current.state in (OfferState.CONFIRMED, OfferState.ACCEPTED)
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
