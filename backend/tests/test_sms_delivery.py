"""Outbound SMS uses committed intents, trusted destinations and consent."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.domain.appointments import Appointment
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.outbox import (
    DeliveryFailure,
    DeliveryState,
    OutboxRecord,
    PermanentDeliveryFailure,
)
from scheduling.domain.sms_ingress import ConsentEvidence

NOW = datetime(2026, 9, 28, 17, tzinfo=UTC)


class Records:
    def __init__(self) -> None:
        self.appointment = Appointment("visit-12345678", "pilot", "client-1", NOW, NOW.replace(hour=18),
                                       CalendarStatus.PENDING_APPROVAL, NOW.replace(hour=19), 60, 0, 1)
        self.profile = ClientProfile("pilot", "client-1", "Synthetic Client", "+14155550101",
                                     "123 Test Street", HomeSize.SMALL, 60, True, 1,
                                     NOW, NOW, NOW)

    def read_appointment(self, appointment_id: str) -> Appointment | None:
        return self.appointment if appointment_id == self.appointment.appointment_id else None

    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None:
        return self.profile if business_id == "pilot" and client_id == "client-1" else None


class Consent:
    def __init__(self) -> None:
        self.approved = True
        self.opted_out = False

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        if not self.approved:
            return None
        return ConsentEvidence("pilot", "client-1", "Synthetic Client", phone_e164,
                               NOW, "pilot-v1")

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return self.opted_out


class Messages:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.fail = False

    def create(self, **kwargs: str) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("private provider diagnostic")
        return SimpleNamespace(sid="SM-synthetic")


def record(recipient: str = "client", template: str = "hold-pending",
           version: int = 1) -> OutboxRecord:
    return OutboxRecord("pilot", "outbox-1", "visit-12345678", recipient, template,
                        version, DeliveryState.SENDING, NOW, NOW, NOW)


def setup() -> tuple[TwilioSmsSender, Messages, Consent, Records]:
    messages, consent, records = Messages(), Consent(), Records()
    sender = TwilioSmsSender(messages, records, consent, "pilot", "+14155550000",
                             "+14155559999", authorized_recipients=frozenset({
                                 "+14155550101", "+14155559999"}))
    return sender, messages, consent, records


def test_client_destination_is_from_verified_profile_with_consent() -> None:
    sender, messages, consent, records = setup()
    assert sender.deliver(record()) == "SM-synthetic"
    assert messages.calls[0]["to"] == records.profile.phone_e164
    assert "pending owner approval" in messages.calls[0]["body"]

    consent.opted_out = True
    with pytest.raises(PermanentDeliveryFailure, match="CONSENT_REQUIRED"):
        sender.deliver(record())
    assert len(messages.calls) == 1
    consent.opted_out = False
    consent.approved = False
    with pytest.raises(PermanentDeliveryFailure, match="CONSENT_REQUIRED"):
        sender.deliver(record())


def test_unknown_or_stale_intent_never_sends() -> None:
    sender, messages, _, _ = setup()
    for intent in (record("+14155550102"), record(template="arbitrary-text"),
                   record(version=2), record(template="hold-request")):
        with pytest.raises(PermanentDeliveryFailure):
            sender.deliver(intent)
    assert messages.calls == []
    assert sender.deliver(record("owner", "hold-request")) == "SM-synthetic"
    assert messages.calls[0]["to"] == "+14155559999"


def test_unapproved_test_number_is_never_sent() -> None:
    _, messages, consent, records = setup()
    sender = TwilioSmsSender(messages, records, consent, "pilot", "+14155550000",
                             "+14155559999", authorized_recipients=frozenset({
                                 "+14155559999"}))
    with pytest.raises(PermanentDeliveryFailure, match="RECIPIENT_NOT_AUTHORIZED"):
        sender.deliver(record())
    assert messages.calls == []


def test_provider_failure_is_safe_and_retryable() -> None:
    sender, messages, _, _ = setup()
    messages.fail = True
    with pytest.raises(DeliveryFailure, match="PROVIDER_SEND_ERROR"):
        sender.deliver(record())
