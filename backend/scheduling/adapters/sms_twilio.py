"""Twilio delivery from committed outbox intents and trusted business records."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment, ReplacementGuard
from scheduling.domain.availability import AvailabilityPolicy
from scheduling.domain.booking_invitations import (
    INVITATION_TEMPLATE,
    InvitationIntent,
    StoredInvitation,
    invitation_outbox_id,
    invitation_problem,
)
from scheduling.domain.booking_outreach import (
    OutreachRecord,
    approved_invitation_message,
    invitation_message,
)
from scheduling.domain.calendar import CalendarSnapshot, CalendarStatus
from scheduling.domain.client_records import ClientProfile, RecordConflict, RecordNotFound
from scheduling.domain.holds import COUNTEROFFER_REQUEST_TEMPLATE
from scheduling.domain.lifecycle import (
    COUNTEROFFER_APPROVED_TEMPLATE,
    COUNTEROFFER_DECLINED_TEMPLATE,
    EXPIRED_REPLACEMENT_WAITING_TEMPLATE,
)
from scheduling.domain.outbox import DeliveryFailure, OutboxRecord, PermanentDeliveryFailure
from scheduling.domain.owner_calendar import UnavailableBlock
from scheduling.domain.owner_counteroffer import (
    COUNTEROFFER_FAILED_TEMPLATE,
    COUNTEROFFER_TEMPLATE,
    PROBLEM_TEXT,
    CounterofferStore,
    OfferProblem,
    OfferState,
    check_offer,
    counteroffer_failure_outbox,
    failure_outbox_id_for,
    outbox_id_for,
)
from scheduling.domain.sms_ingress import (
    WELCOME_TEMPLATE,
    WELCOME_TEXT,
    Keyword,
    SenderRole,
    SmsIngressStore,
    normalize_phone,
    welcome_outbox_id,
)

FICTIONAL_NUMBER = re.compile(r"\+1[2-9][0-9]{2}55501[0-9]{2}")
BLOCK_TEMPLATES = frozenset({"block_time", "edit_block", "remove_block"})


class SchedulingRecords(Protocol):
    def read_appointment(self, appointment_id: str) -> Appointment | None: ...
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...
    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...
    def read_outreach(self, business_id: str) -> OutreachRecord: ...
    def read_confirmed_for_client(self, business_id: str, client_id: str,
                                  run_at: datetime, end_at: datetime) -> tuple[int, tuple[Appointment, ...]]: ...
    def read_last_invitation_sent(self, business_id: str, client_id: str) -> datetime | None: ...
    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...
    def read_block(self, business_id: str, block_id: str) -> UnavailableBlock | None: ...
    def read_replacement_guard(
        self, business_id: str, original_id: str) -> ReplacementGuard | None: ...
    def acquire_client_send(self, business_id: str, client_id: str) -> str: ...
    def release_client_send(self, business_id: str, client_id: str, token: str) -> None: ...
    def read_invitation(self, business_id: str, intent_id: str) -> StoredInvitation | None: ...
    def claim_invitation_handoff(self, intent: InvitationIntent) -> bool: ...
    def complete_invitation(self, intent: InvitationIntent,
                            handed_off_at: datetime | None,
                            provider_id: str | None = None) -> bool: ...


class TwilioMessages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class TwilioSmsSender:
    """No caller or model supplied phone number or free-form SMS body is accepted."""

    def __init__(self, messages: TwilioMessages, records: SchedulingRecords,
                 consent: SmsIngressStore, business_id: str, from_number: str,
                 owner_number: str, *, timezone: str = "America/Los_Angeles",
                 status_callback: str | None = None,
                 clock: Callable[[], datetime] | None = None,
                 counteroffers: CounterofferStore | None = None,
                 invitation_send_enabled: Callable[[], bool] | None = None,
                 manual_invitation_send_enabled: Callable[[], bool] | None = None) -> None:
        if not business_id:
            raise ValueError("Business ID is required")
        self._messages = messages
        self._records = records
        self._consent = consent
        self._business_id = business_id
        self._from = normalize_phone(from_number)
        self._owner = normalize_phone(owner_number)
        self._timezone = ZoneInfo(timezone)
        if status_callback is not None:
            parsed = urlsplit(status_callback)
            if (parsed.scheme != "https" or not parsed.netloc
                    or parsed.path != "/webhooks/sms/status" or parsed.query or parsed.fragment):
                raise ValueError("Status callback needs an exact HTTPS URL")
        self._status_callback = status_callback
        self._clock = clock or (lambda: datetime.now(UTC))
        self._counteroffers = counteroffers
        self._invitation_send_enabled = invitation_send_enabled or (lambda: False)
        self._manual_invitation_send_enabled = manual_invitation_send_enabled or (lambda: False)

    def deliver(self, record: OutboxRecord) -> str:
        if record.business_id != self._business_id:
            raise PermanentDeliveryFailure("BUSINESS_MISMATCH")
        if record.recipient not in {"owner", "client"}:
            raise PermanentDeliveryFailure("RECIPIENT_UNKNOWN")
        if record.template == "conversation-reply":
            return self._deliver_reply(record)
        if record.template == WELCOME_TEMPLATE:
            return self._deliver_welcome(record)
        if record.template == INVITATION_TEMPLATE:
            return self._deliver_invitation(record)
        if record.template == COUNTEROFFER_TEMPLATE:
            return self._deliver_counteroffer(record)
        if record.template == COUNTEROFFER_FAILED_TEMPLATE:
            return self._deliver_counteroffer_failed(record)
        appointment = self._records.read_appointment(record.entity_id)
        if appointment is not None and appointment.business_id != record.business_id:
            raise PermanentDeliveryFailure("ENTITY_MISMATCH")
        profile = (self._records.read_profile(record.business_id, appointment.client_id)
                   if appointment is not None else None)
        if record.recipient == "client":
            if appointment is None or profile is None or not profile.active:
                raise PermanentDeliveryFailure("CLIENT_UNAVAILABLE")
            to = normalize_phone(profile.phone_e164)
            evidence = self._consent.read_consent(record.business_id, to)
            if (profile.phone_verified_at is None or evidence is None
                    or evidence.client_id != profile.client_id):
                raise PermanentDeliveryFailure("CONSENT_REQUIRED")
        else:
            to = self._owner
        if self._consent.is_opted_out(record.business_id, to):
            raise PermanentDeliveryFailure("OPTED_OUT")
        body = self._render(record, appointment, profile)
        return self._send(record, to, body,
                          appointment.client_id if record.recipient == "client"
                          and appointment is not None else None)

    def _deliver_invitation(self, record: OutboxRecord) -> str:
        stored = self._records.read_invitation(record.business_id, record.entity_id)
        if (stored is None or record.recipient != "client" or record.event_version != 0
                or record.outbox_id != invitation_outbox_id(record.entity_id)):
            raise PermanentDeliveryFailure("INVITATION_UNAVAILABLE")
        intent = stored.intent
        if stored.state == "SENT" and stored.provider_id:
            return stored.provider_id  # Outbox completion retry after successful handoff.
        if stored.state != "OUTBOX":
            raise PermanentDeliveryFailure("INVITATION_NOT_SENDABLE")

        def check() -> None:
            enabled = (self._manual_invitation_send_enabled() if intent.manual_message is not None
                       else self._invitation_send_enabled())
            if not enabled:
                raise PermanentDeliveryFailure("INVITATION_SEND_DISABLED")
            reason = invitation_problem(intent, self._records, self._consent, self._clock())
            if reason is not None:
                raise PermanentDeliveryFailure(f"INVITATION_{reason}")

        try:
            check()
        except PermanentDeliveryFailure:
            self._records.complete_invitation(intent, None)
            raise
        if not self._records.claim_invitation_handoff(intent):
            raise PermanentDeliveryFailure("INVITATION_NOT_SENDABLE")
        profile = self._records.read_profile(intent.business_id, intent.client_id)
        if profile is None:
            self._records.complete_invitation(intent, None)
            raise PermanentDeliveryFailure("INVITATION_CLIENT_UNAVAILABLE")
        to = normalize_phone(profile.phone_e164)
        if intent.manual_message is not None:
            body = intent.manual_message
        elif intent.outreach_version is not None:
            body = invitation_message(self._records.read_outreach(intent.business_id).settings)
        else:
            body = approved_invitation_message(intent.lookahead_weeks)
        try:
            provider_id = self._send(record, to, body, intent.client_id, pre_send=check)
        except PermanentDeliveryFailure:
            self._records.complete_invitation(intent, None)
            raise
        except DeliveryFailure as exc:
            if exc.code == "CLIENT_SEND_BUSY":
                # The client lock was held before any provider call. Suppress this
                # run and release its invitation guard; a later scheduled run may
                # select the client again when the other send has finished.
                self._records.complete_invitation(intent, None)
                raise PermanentDeliveryFailure("INVITATION_CLIENT_SEND_BUSY") from exc
            # Provider acceptance is uncertain. Keep the durable SENDING claim and
            # per-client guard for manual reconciliation; never retry this text.
            raise PermanentDeliveryFailure("INVITATION_HANDOFF_UNCERTAIN") from exc
        if not self._records.complete_invitation(intent, self._clock(), provider_id):
            raise PermanentDeliveryFailure("INVITATION_HANDOFF_RECONCILE")
        return provider_id

    def _deliver_welcome(self, record: OutboxRecord) -> str:
        """First enrollment text: only to the phone verified by the consent that queued it."""
        if (record.recipient != "client" or record.outbox_id != welcome_outbox_id(record.entity_id)):
            raise PermanentDeliveryFailure("WELCOME_UNAVAILABLE")
        profile = self._records.read_profile(record.business_id, record.entity_id)
        if profile is None or not profile.active:
            raise PermanentDeliveryFailure("CLIENT_UNAVAILABLE")
        to = normalize_phone(profile.phone_e164)
        evidence = self._consent.read_consent(record.business_id, to)
        if (profile.phone_verified_at is None or evidence is None
                or evidence.client_id != profile.client_id):
            raise PermanentDeliveryFailure("CONSENT_REQUIRED")
        # The welcome is stamped with the consent time, which is also the verification
        # time. A phone change clears verification and new consent resets it, so a
        # mismatch means the number consented to is no longer the profile's number.
        # Other profile edits (such as an address fix) do not block it.
        if profile.phone_verified_at != record.created_at:
            raise PermanentDeliveryFailure("EVENT_SUPERSEDED")
        if self._consent.is_opted_out(record.business_id, to):
            raise PermanentDeliveryFailure("OPTED_OUT")
        return self._send(record, to, WELCOME_TEXT, profile.client_id)

    def _deliver_counteroffer(self, record: OutboxRecord) -> str:
        """Owner-confirmed offer text, sent only if everything still holds at send time."""
        offer = (self._counteroffers.read(record.business_id, record.entity_id)
                 if self._counteroffers is not None else None)
        if (offer is None or record.recipient != "client"
                or record.outbox_id != outbox_id_for(offer.offer_id)
                or offer.state != OfferState.CONFIRMED or record.event_version != offer.version):
            raise PermanentDeliveryFailure("OFFER_UNAVAILABLE")
        now = self._clock()
        problem = check_offer(offer, self._records, self._consent, now)
        if problem is not None:
            # No client text. Tell the owner once (insert-if-absent), then fail permanently.
            assert self._counteroffers is not None
            self._counteroffers.record_failure(
                offer, problem.value, now, counteroffer_failure_outbox(offer, now))
            raise PermanentDeliveryFailure(f"OFFER_{problem.value}")
        to = normalize_phone(offer.client_phone)
        if self._consent.is_opted_out(record.business_id, to):
            raise PermanentDeliveryFailure("OPTED_OUT")
        return self._send(record, to, offer.text, offer.client_id)

    def _deliver_counteroffer_failed(self, record: OutboxRecord) -> str:
        """Owner text explaining why a confirmed offer could not be sent."""
        offer = (self._counteroffers.read(record.business_id, record.entity_id)
                 if self._counteroffers is not None else None)
        if (offer is None or record.recipient != "owner" or offer.failure is None
                or record.outbox_id != failure_outbox_id_for(offer.offer_id)):
            raise PermanentDeliveryFailure("OFFER_UNAVAILABLE")
        if self._consent.is_opted_out(record.business_id, self._owner):
            raise PermanentDeliveryFailure("OPTED_OUT")
        profile = self._records.read_profile(record.business_id, offer.client_id)
        name = profile.name if profile is not None else "the client"
        try:
            reason = PROBLEM_TEXT[OfferProblem(offer.failure)]
        except ValueError as exc:
            raise PermanentDeliveryFailure("OFFER_UNAVAILABLE") from exc
        return self._send(record, self._owner,
                          f"I couldn't send the offer to {name}: {reason} "
                          "Tell me another time to offer.")

    def _deliver_reply(self, record: OutboxRecord) -> str:
        receipt = self._consent.read_received(record.business_id, record.entity_id)
        body = self._consent.read_reply_text(record.business_id, record.entity_id)
        if (receipt is None or body is None or not receipt.authorized_for_commands
                or receipt.body is None or receipt.keyword != Keyword.OTHER
                or record.event_version != 0 or record.outbox_id != f"sms-reply#{record.entity_id}"
                or receipt.role.value != record.recipient):
            raise PermanentDeliveryFailure("REPLY_UNAVAILABLE")
        to = normalize_phone(receipt.sender)
        if receipt.role == SenderRole.OWNER:
            if to != self._owner:
                raise PermanentDeliveryFailure("OWNER_MISMATCH")
        elif receipt.role == SenderRole.CLIENT:
            profile = (self._records.read_profile(record.business_id, receipt.client_id)
                       if receipt.client_id is not None else None)
            evidence = self._consent.read_consent(record.business_id, to)
            if (profile is None or not profile.active or profile.phone_verified_at is None
                    or profile.phone_e164 != to or evidence is None
                    or evidence.client_id != receipt.client_id):
                raise PermanentDeliveryFailure("CONSENT_REQUIRED")
        else:
            raise PermanentDeliveryFailure("RECIPIENT_UNKNOWN")
        if self._consent.is_opted_out(record.business_id, to):
            raise PermanentDeliveryFailure("OPTED_OUT")
        return self._send(record, to, body,
                          receipt.client_id if receipt.role == SenderRole.CLIENT else None)

    def _send(self, record: OutboxRecord, to: str, body: str,
              client_id: str | None = None,
              pre_send: Callable[[], None] | None = None) -> str:
        # Synthetic test numbers (555-0100 to 555-0199 in any area code) never reach Twilio.
        if FICTIONAL_NUMBER.fullmatch(to):
            raise PermanentDeliveryFailure("FICTIONAL_NUMBER")
        kwargs = {"from_": self._from, "to": to, "body": body}
        if self._status_callback is not None:
            kwargs["status_callback"] = (self._status_callback + "?" + urlencode({
                "outbox_id": record.outbox_id,
            }))
        token: str | None = None
        if client_id is not None:
            try:
                token = self._records.acquire_client_send(record.business_id, client_id)
            except RecordNotFound as exc:
                raise PermanentDeliveryFailure("CLIENT_UNAVAILABLE") from exc
            except RecordConflict as exc:
                raise DeliveryFailure("CLIENT_SEND_BUSY") from exc
        evidence_written = False
        provider_started = False
        try:
            if pre_send is not None:
                pre_send()
            try:
                provider_started = True
                result = self._messages.create(**kwargs)
            except Exception as exc:
                # Never leak provider diagnostics, body, or destination to the outbox.
                raise DeliveryFailure("PROVIDER_SEND_ERROR") from exc
            provider_id = getattr(result, "sid", None)
            if not isinstance(provider_id, str) or not provider_id:
                raise DeliveryFailure("PROVIDER_ID_MISSING")
            self._consent.record_outbound(record.business_id, to, provider_id, self._clock(),
                                          body, client_id, record.template)
            evidence_written = True
            return provider_id
        finally:
            # A timeout or evidence-write failure may follow provider acceptance.
            # Keep the claim until an operator reconciles that uncertain send.
            if (not provider_started or evidence_written) and token is not None and client_id is not None:
                self._records.release_client_send(record.business_id, client_id, token)

    def _render(self, record: OutboxRecord, appointment: Appointment | None,
                profile: ClientProfile | None) -> str:
        if record.template in BLOCK_TEMPLATES:
            if record.recipient != "owner":
                raise PermanentDeliveryFailure("TEMPLATE_RECIPIENT_MISMATCH")
            if record.block_start_at is None or record.block_end_at is None:
                raise PermanentDeliveryFailure("ENTITY_UNAVAILABLE")
            current = self._records.read_block(record.business_id, record.entity_id)
            if record.template == "remove_block":
                if current is not None:
                    raise PermanentDeliveryFailure("EVENT_SUPERSEDED")
            elif (current is None or current.version != record.event_version
                  or current.start_at != record.block_start_at
                  or current.end_at != record.block_end_at):
                raise PermanentDeliveryFailure("EVENT_SUPERSEDED")
            start = record.block_start_at.astimezone(self._timezone)
            end = record.block_end_at.astimezone(self._timezone)
            action = {"block_time": "blocked", "edit_block": "updated",
                      "remove_block": "unblocked"}[record.template]
            return (f"Owner calendar {action}: {start.strftime('%a %b')} {start.day}, "
                    f"{start.year} at {start.strftime('%I:%M %p').lstrip('0')} "
                    f"{start.strftime('%Z')} to {end.strftime('%a %b')} {end.day}, "
                    f"{end.year} at {end.strftime('%I:%M %p').lstrip('0')} "
                    f"{end.strftime('%Z')}. Check the current calendar.")
        if (record.template not in BLOCK_TEMPLATES and appointment is not None
                and record.event_version != appointment.version):
            raise PermanentDeliveryFailure("EVENT_SUPERSEDED")
        if record.reply_provider_id is not None and record.recipient == "client":
            drafted = self._consent.read_reply_text(record.business_id,
                                                    record.reply_provider_id)
            if drafted is not None:
                return drafted
        original = (self._records.read_appointment(appointment.replaces_appointment_id)
                    if appointment is not None and appointment.replaces_appointment_id
                    and record.template in COUNTEROFFER_TEMPLATES else None)
        if appointment is not None and record.template == EXPIRED_REPLACEMENT_WAITING_TEMPLATE:
            guard = self._records.read_replacement_guard(
                record.business_id, appointment.appointment_id)
            original = (self._records.read_appointment(guard.replacement_id)
                        if guard is not None else None)
        return render_notification(record.template, record.recipient, appointment, profile,
                                   self._timezone, original, self._clock())


COUNTEROFFER_TEMPLATES = frozenset({
    COUNTEROFFER_REQUEST_TEMPLATE, COUNTEROFFER_APPROVED_TEMPLATE,
    COUNTEROFFER_DECLINED_TEMPLATE})


def _counteroffer_text(template: str, recipient: str, appointment: Appointment,
                       profile: ClientProfile | None, timezone: ZoneInfo,
                       original: Appointment | None, now: datetime | None) -> str:
    """Wording for a request created by accepting an owner counteroffer."""
    when = _when(appointment.start_at.astimezone(timezone))
    reference = appointment.appointment_id[:8]
    name = profile.name if profile else "client"
    earlier = (_when(original.start_at.astimezone(timezone)) if original is not None
               else "the earlier time")
    earlier_ref = f" (ref {original.appointment_id[:8]})" if original is not None else ""
    if template == COUNTEROFFER_REQUEST_TEMPLATE:
        if recipient != "owner":
            raise PermanentDeliveryFailure("TEMPLATE_RECIPIENT_MISMATCH")
        return (f"{name} accepted your counteroffer: new request for {when}, "
                f"{appointment.duration_minutes} min. Ref {reference}. It replaces their "
                f"request for {earlier}{earlier_ref}, which stays pending until you decide. "
                "Approving this one resolves both. Review before approval.")
    if template == COUNTEROFFER_APPROVED_TEMPLATE:
        if recipient == "owner":
            return (f"Accepted counteroffer approved: {name}, {when}. Ref {reference}. "
                    f"Both requests are resolved: their request for {earlier}{earlier_ref} "
                    "is closed.")
        return f"Cleaning visit confirmed: {when}. Ref {reference}. Reply to the business number for help."
    live = (original is not None and original.status == CalendarStatus.PENDING_APPROVAL
            and original.hold_expires_at is not None and now is not None
            and original.hold_expires_at > now)
    tail = (f" The earlier request for {earlier}{earlier_ref} is still pending owner approval."
            if live else "")
    return f"Cleaning request declined: {when}. Ref {reference}.{tail}"


def render_notification(template: str, recipient: str, appointment: Appointment | None,
                        profile: ClientProfile | None, timezone: ZoneInfo,
                        original: Appointment | None = None,
                        now: datetime | None = None) -> str:
    """Render a notification body from trusted records; callers check event freshness."""
    if template in BLOCK_TEMPLATES:
        raise PermanentDeliveryFailure("ENTITY_UNAVAILABLE")
    if appointment is None:
        if recipient != "owner":
            raise PermanentDeliveryFailure("ENTITY_UNAVAILABLE")
        if template in {"seed_policy", "edit_business_calendar"}:
            return "Scheduling policy updated. Check the current owner calendar."
        raise PermanentDeliveryFailure("TEMPLATE_UNKNOWN")
    if template == EXPIRED_REPLACEMENT_WAITING_TEMPLATE:
        # The normal expiry text, plus the accepted counteroffer request still waiting
        # (``original`` carries that replacement here).
        text = render_notification("expire", recipient, appointment, profile, timezone)
        if (original is not None and original.status == CalendarStatus.PENDING_APPROVAL
                and original.hold_expires_at is not None and now is not None
                and original.hold_expires_at > now):
            text += (f" Your request for {_when(original.start_at.astimezone(timezone))} "
                     f"(ref {original.appointment_id[:8]}) is still pending owner approval.")
        return text
    if template not in {
        "hold-request", "hold-pending", "approve", "decline", "expire", "cancel",
        "edit_appointment", "replacement-approved", "replacement-original-retained",
        "create_owner_appointment", *COUNTEROFFER_TEMPLATES,
    }:
        raise PermanentDeliveryFailure("TEMPLATE_UNKNOWN")
    if template in COUNTEROFFER_TEMPLATES:
        return _counteroffer_text(
            template, recipient, appointment, profile, timezone, original, now)
    if template == "hold-request" and recipient != "owner":
        raise PermanentDeliveryFailure("TEMPLATE_RECIPIENT_MISMATCH")
    if template == "hold-pending" and recipient != "client":
        raise PermanentDeliveryFailure("TEMPLATE_RECIPIENT_MISMATCH")
    local = appointment.start_at.astimezone(timezone)
    when = _when(local)
    reference = appointment.appointment_id[:8]
    if template == "hold-request":
        name = profile.name if profile else "client"
        return (f"New cleaning request from {name}: {when}, "
                f"{appointment.duration_minutes} min. Ref {reference}. Review before approval.")
    if template == "hold-pending":
        return f"Your cleaning request for {when} is pending owner approval. Ref {reference}."
    # A later state may have superseded this intent before delivery. The
    # event-version check in the sender prevents a stale status announcement.
    status = {
        "approve": "confirmed", "decline": "declined", "expire": "expired",
        "cancel": "cancelled", "edit_appointment": "updated",
        "replacement-approved": "confirmed", "replacement-original-retained": "not changed",
        "create_owner_appointment": "confirmed",
    }[template]
    return (f"Cleaning visit {status}: {when}. Ref {reference}. "
            "Reply to the business number for help.")

def _when(local: datetime) -> str:
    return f"{local.strftime('%a %b')} {local.day} at {local.strftime('%I:%M %p').lstrip('0')}"
