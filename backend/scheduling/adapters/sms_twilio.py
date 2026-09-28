"""Twilio delivery from committed outbox intents and trusted business records."""

from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.outbox import DeliveryFailure, OutboxRecord, PermanentDeliveryFailure
from scheduling.domain.sms_ingress import SmsIngressStore, normalize_phone


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
                 authorized_recipients: frozenset[str]) -> None:
        if not business_id:
            raise ValueError("Business ID is required")
        self._messages = messages
        self._records = records
        self._consent = consent
        self._business_id = business_id
        self._from = normalize_phone(from_number)
        self._owner = normalize_phone(owner_number)
        self._authorized_recipients = frozenset(
            normalize_phone(number) for number in authorized_recipients
        )
        if not self._authorized_recipients:
            raise ValueError("Explicitly authorized SMS recipients are required")
        self._timezone = ZoneInfo(timezone)
        if status_callback is not None:
            parsed = urlsplit(status_callback)
            if (parsed.scheme != "https" or not parsed.netloc
                    or parsed.path != "/webhooks/sms/status" or parsed.query or parsed.fragment):
                raise ValueError("Status callback needs an exact HTTPS URL")
        self._status_callback = status_callback

    def deliver(self, record: OutboxRecord) -> str:
        if record.business_id != self._business_id:
            raise PermanentDeliveryFailure("BUSINESS_MISMATCH")
        if record.recipient not in {"owner", "client"}:
            raise PermanentDeliveryFailure("RECIPIENT_UNKNOWN")
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
                    or evidence.client_id != profile.client_id
                    or self._consent.is_opted_out(record.business_id, to)):
                raise PermanentDeliveryFailure("CONSENT_REQUIRED")
        else:
            to = self._owner
        if to not in self._authorized_recipients:
            raise PermanentDeliveryFailure("RECIPIENT_NOT_AUTHORIZED")
        body = self._render(record, appointment, profile)
        kwargs = {"from_": self._from, "to": to, "body": body}
        if self._status_callback is not None:
            kwargs["status_callback"] = self._status_callback
        try:
            result = self._messages.create(**kwargs)
        except Exception as exc:
            # Never leak provider diagnostics, body, or destination to the outbox.
            raise DeliveryFailure("PROVIDER_SEND_ERROR") from exc
        provider_id = getattr(result, "sid", None)
        if not isinstance(provider_id, str) or not provider_id:
            raise DeliveryFailure("PROVIDER_ID_MISSING")
        return provider_id

    def _render(self, record: OutboxRecord, appointment: Appointment | None,
                profile: ClientProfile | None) -> str:
        template = record.template
        if template in {"block_time", "edit_block", "remove_block"}:
            if record.recipient != "owner":
                raise PermanentDeliveryFailure("TEMPLATE_RECIPIENT_MISMATCH")
            return f"Owner calendar updated ({template}). Check the current calendar."
        if appointment is None:
            if record.recipient != "owner":
                raise PermanentDeliveryFailure("ENTITY_UNAVAILABLE")
            if template in {"seed_policy", "edit_business_calendar"}:
                return "Scheduling policy updated. Check the current owner calendar."
            raise PermanentDeliveryFailure("TEMPLATE_UNKNOWN")
        if record.event_version != appointment.version:
            raise PermanentDeliveryFailure("EVENT_SUPERSEDED")
        if template not in {
            "hold-request", "hold-pending", "approve", "decline", "expire", "cancel",
            "edit_appointment", "replacement-approved", "replacement-original-retained",
            "create_owner_appointment",
        }:
            raise PermanentDeliveryFailure("TEMPLATE_UNKNOWN")
        if template == "hold-request" and record.recipient != "owner":
            raise PermanentDeliveryFailure("TEMPLATE_RECIPIENT_MISMATCH")
        if template == "hold-pending" and record.recipient != "client":
            raise PermanentDeliveryFailure("TEMPLATE_RECIPIENT_MISMATCH")
        local = appointment.start_at.astimezone(self._timezone)
        when = _when(local)
        reference = appointment.appointment_id[:8]
        if template == "hold-request":
            name = profile.name if profile else "client"
            return (f"New cleaning request from {name}: {when}, "
                    f"{appointment.duration_minutes} min. Ref {reference}. Review before approval.")
        if template == "hold-pending":
            return f"Your cleaning request for {when} is pending owner approval. Ref {reference}."
        # A later state may have superseded this intent before delivery. The
        # event-version check above prevents a stale status announcement.
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
