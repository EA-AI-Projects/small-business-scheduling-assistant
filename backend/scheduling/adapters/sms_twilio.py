"""Twilio delivery from committed outbox intents and trusted business records."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.outbox import DeliveryFailure, OutboxRecord, PermanentDeliveryFailure
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


class TwilioMessages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class TwilioSmsSender:
    """No caller or model supplied phone number or free-form SMS body is accepted."""

    def __init__(self, messages: TwilioMessages, records: SchedulingRecords,
                 consent: SmsIngressStore, business_id: str, from_number: str,
                 owner_number: str, *, timezone: str = "America/Los_Angeles",
                 status_callback: str | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
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

    def deliver(self, record: OutboxRecord) -> str:
        if record.business_id != self._business_id:
            raise PermanentDeliveryFailure("BUSINESS_MISMATCH")
        if record.recipient not in {"owner", "client"}:
            raise PermanentDeliveryFailure("RECIPIENT_UNKNOWN")
        if record.template == "conversation-reply":
            return self._deliver_reply(record)
        if record.template == WELCOME_TEMPLATE:
            return self._deliver_welcome(record)
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
        return self._send(record, to, body)

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
        # Any profile change after the consent write (such as a new phone) supersedes it.
        if record.event_version != profile.version:
            raise PermanentDeliveryFailure("EVENT_SUPERSEDED")
        if self._consent.is_opted_out(record.business_id, to):
            raise PermanentDeliveryFailure("OPTED_OUT")
        return self._send(record, to, WELCOME_TEXT)

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
        return self._send(record, to, body)

    def _send(self, record: OutboxRecord, to: str, body: str) -> str:
        # Synthetic test numbers (555-0100 to 555-0199 in any area code) never reach Twilio.
        if FICTIONAL_NUMBER.fullmatch(to):
            raise PermanentDeliveryFailure("FICTIONAL_NUMBER")
        kwargs = {"from_": self._from, "to": to, "body": body}
        if self._status_callback is not None:
            kwargs["status_callback"] = (self._status_callback + "?" + urlencode({
                "outbox_id": record.outbox_id,
            }))
        try:
            result = self._messages.create(**kwargs)
        except Exception as exc:
            # Never leak provider diagnostics, body, or destination to the outbox.
            raise DeliveryFailure("PROVIDER_SEND_ERROR") from exc
        provider_id = getattr(result, "sid", None)
        if not isinstance(provider_id, str) or not provider_id:
            raise DeliveryFailure("PROVIDER_ID_MISSING")
        self._consent.record_outbound(record.business_id, to, provider_id, self._clock())
        return provider_id

    def _render(self, record: OutboxRecord, appointment: Appointment | None,
                profile: ClientProfile | None) -> str:
        if (record.template not in BLOCK_TEMPLATES and appointment is not None
                and record.event_version != appointment.version):
            raise PermanentDeliveryFailure("EVENT_SUPERSEDED")
        return render_notification(record.template, record.recipient, appointment, profile,
                                   self._timezone)


def render_notification(template: str, recipient: str, appointment: Appointment | None,
                        profile: ClientProfile | None, timezone: ZoneInfo) -> str:
    """Render a notification body from trusted records; callers check event freshness."""
    if template in BLOCK_TEMPLATES:
        if recipient != "owner":
            raise PermanentDeliveryFailure("TEMPLATE_RECIPIENT_MISMATCH")
        return f"Owner calendar updated ({template}). Check the current calendar."
    if appointment is None:
        if recipient != "owner":
            raise PermanentDeliveryFailure("ENTITY_UNAVAILABLE")
        if template in {"seed_policy", "edit_business_calendar"}:
            return "Scheduling policy updated. Check the current owner calendar."
        raise PermanentDeliveryFailure("TEMPLATE_UNKNOWN")
    if template not in {
        "hold-request", "hold-pending", "approve", "decline", "expire", "cancel",
        "edit_appointment", "replacement-approved", "replacement-original-retained",
        "create_owner_appointment",
    }:
        raise PermanentDeliveryFailure("TEMPLATE_UNKNOWN")
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
