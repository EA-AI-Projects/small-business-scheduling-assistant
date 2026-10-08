"""Outbound SMS uses committed intents, trusted destinations and consent."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scheduling.adapters.sms_twilio import FICTIONAL_NUMBER, TwilioSmsSender
from scheduling.domain.appointments import Appointment
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize, RecordConflict, RecordNotFound
from scheduling.domain.outbox import (
    DeliveryFailure,
    DeliveryState,
    OutboxRecord,
    PermanentDeliveryFailure,
)
from scheduling.domain.owner_calendar import UnavailableBlock
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

# Sendable numbers are Twilio magic test numbers (+1500555xxxx): they belong to no person and are not
# in the fictional 555-0100 to 555-0199 range the sender refuses.
NOW = datetime(2026, 9, 28, 17, tzinfo=UTC)


class Records:
    def __init__(self) -> None:
        self.appointment = Appointment("visit-12345678", "pilot", "client-1", NOW, NOW.replace(hour=18),
                                       CalendarStatus.PENDING_APPROVAL, NOW.replace(hour=19), 60, 0, 1)
        self.profile = ClientProfile("pilot", "client-1", "Synthetic Client", "+15005550006",
                                     "123 Test Street", HomeSize.SMALL, 60, True, 1,
                                     NOW, NOW, NOW)
        self.send_claimed = False
        self.block: UnavailableBlock | None = None

    def read_block(self, business_id: str, block_id: str) -> UnavailableBlock | None:
        return self.block if (business_id == "pilot" and self.block is not None
                              and self.block.block_id == block_id) else None

    def read_appointment(self, appointment_id: str) -> Appointment | None:
        return self.appointment if appointment_id == self.appointment.appointment_id else None

    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None:
        return self.profile if business_id == "pilot" and client_id == "client-1" else None

    def acquire_client_send(self, business_id: str, client_id: str) -> str:
        assert business_id == "pilot" and client_id == "client-1"
        assert not self.send_claimed
        self.send_claimed = True
        return "synthetic-token"

    def release_client_send(self, business_id: str, client_id: str, token: str) -> None:
        assert business_id == "pilot" and client_id == "client-1" and token == "synthetic-token"
        self.send_claimed = False


class Consent:
    def __init__(self) -> None:
        self.approved = True
        self.opted_out = False
        self.outbound: list[tuple[str, str, str, datetime]] = []
        self.reply_receipt: InboundReceipt | None = None
        self.reply_text: str | None = None

    def record_outbound(self, business_id: str, phone_e164: str,
                        provider_id: str, sent_at: datetime, body: str,
                        client_id: str | None = None, template: str = "") -> None:
        self.outbound.append((business_id, phone_e164, provider_id, sent_at))

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        if not self.approved:
            return None
        return ConsentEvidence("pilot", "client-1", "Synthetic Client", phone_e164,
                               NOW, "pilot-v1")

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return self.opted_out

    def read_received(self, business_id: str, provider_id: str) -> InboundReceipt | None:
        if business_id == "pilot" and provider_id == "SM-in":
            return self.reply_receipt
        return None

    def read_reply_text(self, business_id: str, provider_id: str) -> str | None:
        return self.reply_text if business_id == "pilot" and provider_id == "SM-in" else None


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
                             "+15005550009")
    return sender, messages, consent, records


def test_client_destination_is_from_verified_profile_with_consent() -> None:
    sender, messages, consent, records = setup()
    assert sender.deliver(record()) == "SM-synthetic"
    assert messages.calls[0]["to"] == records.profile.phone_e164
    assert "pending owner approval" in messages.calls[0]["body"]
    assert consent.outbound[0][1:3] == (records.profile.phone_e164, "SM-synthetic")
    consent.opted_out = True
    with pytest.raises(PermanentDeliveryFailure, match="OPTED_OUT"):
        sender.deliver(record())
    assert len(messages.calls) == 1
    consent.opted_out = False
    consent.approved = False
    with pytest.raises(PermanentDeliveryFailure, match="CONSENT_REQUIRED"):
        sender.deliver(record())


def test_committed_notification_uses_attached_draft_once_or_safe_template() -> None:
    sender, messages, consent, _ = setup()
    consent.reply_text = "Your request is pending owner approval. Ref a101a101."
    attached = replace(record(), reply_provider_id="SM-in")
    assert sender.deliver(attached) == "SM-synthetic"
    assert messages.calls[-1]["body"] == consent.reply_text
    consent.reply_text = None  # Draft vanished; the original intent still has safe wording.
    assert sender.deliver(attached) == "SM-synthetic"
    assert "pending owner approval" in messages.calls[-1]["body"]
    assert len(messages.calls) == 2


def test_client_send_claim_blocks_deletion_race_and_survives_uncertain_failure() -> None:
    sender, messages, _, records = setup()
    def erasing(_business_id: str, _client_id: str) -> str:
        raise RecordConflict("Erasing")
    records.acquire_client_send = erasing  # type: ignore[method-assign]
    with pytest.raises(DeliveryFailure, match="CLIENT_SEND_BUSY"):
        sender.deliver(record())
    assert messages.calls == []

    def deleted(_business_id: str, _client_id: str) -> str:
        raise RecordNotFound("Deleted")
    records.acquire_client_send = deleted  # type: ignore[method-assign]
    with pytest.raises(PermanentDeliveryFailure, match="CLIENT_UNAVAILABLE"):
        sender.deliver(record())
    assert messages.calls == []

    sender, messages, _, records = setup()
    messages.fail = True
    with pytest.raises(DeliveryFailure, match="PROVIDER_SEND_ERROR"):
        sender.deliver(record())
    assert records.send_claimed is True

    sender, messages, consent, records = setup()
    def evidence_failed(_business_id: str, _phone_e164: str,
                        _provider_id: str, _sent_at: datetime, _body: str,
                        _client_id: str | None, _template: str) -> None:
        raise RuntimeError("evidence unavailable")
    consent.record_outbound = evidence_failed  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="evidence unavailable"):
        sender.deliver(record())
    assert len(messages.calls) == 1
    assert records.send_claimed is True


def test_callback_identifies_committed_outbox_intent() -> None:
    messages, consent, records = Messages(), Consent(), Records()
    sender = TwilioSmsSender(
        messages, records, consent, "pilot", "+14155550000", "+15005550009",
        status_callback="https://sms.example.test/webhooks/sms/status",
    )
    sender.deliver(record())
    assert messages.calls[0]["status_callback"] == (
        "https://sms.example.test/webhooks/sms/status?outbox_id=outbox-1"
    )

def test_unknown_or_stale_intent_never_sends() -> None:
    sender, messages, _, _ = setup()
    for intent in (record("+14155550102"), record(template="arbitrary-text"),
                   record(version=2), record(template="hold-request")):
        with pytest.raises(PermanentDeliveryFailure):
            sender.deliver(intent)
    assert messages.calls == []
    assert sender.deliver(record("owner", "hold-request")) == "SM-synthetic"
    assert messages.calls[0]["to"] == "+15005550009"


def test_consented_verified_client_is_sent_to_without_any_allowlist() -> None:
    sender, messages, _, records = setup()
    assert sender.deliver(record()) == "SM-synthetic"
    assert messages.calls[0]["to"] == records.profile.phone_e164


def test_unverified_client_is_refused() -> None:
    sender, messages, _, records = setup()
    records.profile = replace(records.profile, phone_verified_at=None)
    with pytest.raises(PermanentDeliveryFailure, match="CONSENT_REQUIRED"):
        sender.deliver(record())
    assert messages.calls == []


@pytest.mark.parametrize("number", ["+14155550100", "+14155550150", "+12125550199", "+16505550100"])
def test_fictional_numbers_never_reach_twilio(number: str) -> None:
    sender, messages, _, records = setup()
    records.profile = replace(records.profile, phone_e164=number)
    with pytest.raises(PermanentDeliveryFailure, match="FICTIONAL_NUMBER"):
        sender.deliver(record())
    assert messages.calls == []


def test_fictional_client_number_on_conversation_reply_is_refused() -> None:
    sender, messages, consent, records = setup()
    records.profile = replace(records.profile, phone_e164="+14155550150")
    consent.reply_receipt = InboundReceipt(
        "pilot", "SM-in", "+14155550150", "+14155550000",
        "Book 2026-10-01", NOW, SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    consent.reply_text = "Please send one exact date and time."
    reply = OutboxRecord("pilot", "sms-reply#SM-in", "SM-in", "client",
                         "conversation-reply", 0, DeliveryState.SENDING, NOW, NOW, NOW)
    with pytest.raises(PermanentDeliveryFailure, match="FICTIONAL_NUMBER"):
        sender.deliver(reply)
    assert messages.calls == []


def test_fictional_owner_number_is_refused() -> None:
    messages, consent, records = Messages(), Consent(), Records()
    sender = TwilioSmsSender(messages, records, consent, "pilot", "+14155550000", "+14155550123")
    with pytest.raises(PermanentDeliveryFailure, match="FICTIONAL_NUMBER"):
        sender.deliver(record("owner", "hold-request"))
    assert messages.calls == []


def test_provider_failure_is_safe_and_retryable() -> None:
    sender, messages, _, _ = setup()
    messages.fail = True
    with pytest.raises(DeliveryFailure, match="PROVIDER_SEND_ERROR"):
        sender.deliver(record())


def test_owner_stop_blocks_owner_notifications() -> None:
    sender, messages, consent, _ = setup()
    consent.opted_out = True
    with pytest.raises(PermanentDeliveryFailure, match="OPTED_OUT"):
        sender.deliver(record("owner", "hold-request"))
    assert messages.calls == []


def test_block_create_and_remove_render_committed_local_interval_at_adapter_boundary() -> None:
    sender, messages, _, records = setup()
    start = datetime(2026, 11, 2, 18, tzinfo=UTC)
    end = datetime(2026, 11, 2, 20, tzinfo=UTC)
    records.block = UnavailableBlock("block-1", "pilot", start, end, 1)
    created = replace(record("owner", "block_time"), entity_id="block-1",
                      block_start_at=start, block_end_at=end)
    assert sender.deliver(created) == "SM-synthetic"
    assert messages.calls[-1]["to"] == "+15005550009"
    assert "Mon Nov 2, 2026 at 10:00 AM PST" in messages.calls[-1]["body"]
    assert "Mon Nov 2, 2026 at 12:00 PM PST" in messages.calls[-1]["body"]
    assert "block_time" not in messages.calls[-1]["body"]

    records.block = None
    removed = replace(created, template="remove_block")
    assert sender.deliver(removed) == "SM-synthetic"
    assert "unblocked" in messages.calls[-1]["body"]
    assert "Mon Nov 2, 2026 at 10:00 AM PST" in messages.calls[-1]["body"]
    assert "remove_block" not in messages.calls[-1]["body"]

    with pytest.raises(PermanentDeliveryFailure, match="CLIENT_UNAVAILABLE"):
        sender.deliver(replace(created, recipient="client"))
    with pytest.raises(PermanentDeliveryFailure, match="EVENT_SUPERSEDED"):
        sender.deliver(created)
    records.block = UnavailableBlock("block-1", "pilot", start, end, 2)
    with pytest.raises(PermanentDeliveryFailure, match="EVENT_SUPERSEDED"):
        sender.deliver(removed)
    assert len(messages.calls) == 2


def test_conversation_reply_uses_persisted_verified_receipt_and_current_consent() -> None:
    sender, messages, consent, _ = setup()
    consent.reply_receipt = InboundReceipt(
        "pilot", "SM-in", "+15005550006", "+14155550000",
        "Book 2026-10-01", NOW, SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    consent.reply_text = "Please send one exact date and time."
    reply = OutboxRecord("pilot", "sms-reply#SM-in", "SM-in", "client",
                         "conversation-reply", 0, DeliveryState.SENDING, NOW, NOW, NOW)
    assert sender.deliver(reply) == "SM-synthetic"
    assert messages.calls[0]["to"] == "+15005550006"
    assert messages.calls[0]["body"] == consent.reply_text
    consent.opted_out = True
    with pytest.raises(PermanentDeliveryFailure, match="OPTED_OUT"):
        sender.deliver(reply)
    consent.opted_out = False
    consent.approved = False
    with pytest.raises(PermanentDeliveryFailure, match="CONSENT_REQUIRED"):
        sender.deliver(reply)
    assert len(messages.calls) == 1


def test_conversation_reply_rejects_missing_body_or_forged_destination() -> None:
    sender, messages, consent, _ = setup()
    consent.reply_receipt = InboundReceipt(
        "pilot", "SM-in", "+15005550006", "+14155550000",
        "Book 2026-10-01", NOW, SenderRole.CLIENT, "client-1", Keyword.OTHER, True)
    consent.reply_text = "Safe clarification"
    reply = OutboxRecord("pilot", "sms-reply#SM-in", "SM-in", "owner",
                         "conversation-reply", 0, DeliveryState.SENDING, NOW, NOW, NOW)
    with pytest.raises(PermanentDeliveryFailure, match="REPLY_UNAVAILABLE"):
        sender.deliver(reply)
    assert messages.calls == []


def test_template_phone_defaults_cannot_be_texted() -> None:
    # OwnerNumber is a recipient, so its default must be in the refused fictional range.
    # TwilioBusinessNumber is a From number, not a recipient; its default is a 555-0000 placeholder.
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "template.yaml").read_text()

    def default(name: str) -> str:
        match = re.search(rf"^  {name}:\n(?:    .*\n)*?    Default: '([^']*)'", text, re.MULTILINE)
        assert match is not None
        return match.group(1)

    assert FICTIONAL_NUMBER.fullmatch(default("OwnerNumber"))
    assert default("TwilioBusinessNumber") == "+14155550000"
    assert set(re.findall(r"'(\+\d{8,15})'", text)) == {"+14155550199", "+14155550000"}
