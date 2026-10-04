"""In-person consent also verifies the profile phone, atomically, and nothing else does."""

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.domain.appointments import Appointment
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import (
    ClientProfile,
    ClientRecordService,
    HomeSize,
    RecordConflict,
)
from scheduling.domain.outbox import DeliveryState, OutboxRecord, PermanentDeliveryFailure
from scheduling.domain.sms_ingress import (
    ConsentEvidence,
    InboundReceipt,
    SenderRole,
    SmsIngressService,
    record_in_person_consent,
)
from scheduling.twilio_webhooks import create_twilio_ingress_app

NOW = datetime(2026, 9, 28, 17, tzinfo=UTC)
# Twilio magic test number: belongs to no person and is not in the refused fictional range.
PHONE = "+15005550006"
URL = "https://sms.example.test/webhooks/sms/inbound"
TOKEN = "synthetic-auth-token"


class Store:
    """Memory ingress store with the same all-or-nothing contract as the DynamoDB one."""

    def __init__(self, repository: InMemoryCalendarRepository) -> None:
        self.repository = repository
        self.consent: dict[str, ConsentEvidence] = {}
        self.receipts: dict[str, InboundReceipt] = {}

    def put_consent(self, evidence: ConsentEvidence) -> None:
        self.consent[evidence.phone_e164] = evidence

    def put_consent_verifying_phone(self, evidence: ConsentEvidence,
                                    verified: ClientProfile,
                                    welcome: OutboxRecord | None = None) -> None:
        # The profile write is conditional on the version; if it raises, no consent is kept.
        self.repository.save_profile(verified, verified.version - 1, verified.phone_e164)
        self.put_consent(evidence)

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return self.consent.get(phone_e164)

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def put_received(self, receipt: InboundReceipt) -> bool:
        self.receipts[receipt.provider_id] = receipt
        return True

    def record_outbound(self, *args: object) -> None:
        pass


def world() -> tuple[InMemoryCalendarRepository, ClientRecordService, Store]:
    repository = InMemoryCalendarRepository()
    clients = ClientRecordService(repository)
    clients.save_profile("pilot", "client-1", "Synthetic Client", PHONE, "123 Test Street",
                         HomeSize.SMALL, 60, True, 0, 180, NOW)
    return repository, clients, Store(repository)


def consent(repository: InMemoryCalendarRepository, store: Store,
            phone: str = PHONE) -> ConsentEvidence:
    return record_in_person_consent(store, repository, "pilot", "client-1", phone,  # type: ignore[arg-type]
                                    "Synthetic Client", "pilot-v1", NOW)


def signed(values: dict[str, str]) -> dict[str, str]:
    return {"X-Twilio-Signature": RequestValidator(TOKEN).compute_signature(URL, values)}


def test_consent_marks_phone_verified_and_client_can_text() -> None:
    repository, _, store = world()
    service = SmsIngressService(store, repository, "pilot", "+14155550000",  # type: ignore[arg-type]
                                "+15005550009")
    api = TestClient(create_twilio_ingress_app(service, TOKEN, URL, lambda: NOW))
    values = {"MessageSid": "SM-1", "From": PHONE, "To": "+14155550000", "Body": "Tuesday at 9"}

    before = repository.read_profile("pilot", "client-1")
    assert before is not None and before.phone_verified_at is None
    assert api.post("/webhooks/sms/inbound", data=values,
                    headers=signed(values)).status_code == 204
    assert "SM-1" not in store.receipts

    consent(repository, store)
    profile = repository.read_profile("pilot", "client-1")
    assert profile is not None and profile.version == 2
    assert profile.phone_verified_at == NOW
    values["MessageSid"] = "SM-2"
    assert api.post("/webhooks/sms/inbound", data=values,
                    headers=signed(values)).status_code == 204
    assert store.receipts["SM-2"].role == SenderRole.CLIENT
    assert store.receipts["SM-2"].authorized_for_commands is True


class Messages:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create(self, **kwargs: str) -> Any:
        self.calls.append(kwargs)
        return type("Sent", (), {"sid": "SM-out"})()


class Records:
    def __init__(self, repository: InMemoryCalendarRepository) -> None:
        self.repository = repository
        self.appointment = Appointment("visit-12345678", "pilot", "client-1", NOW,
                                       NOW + timedelta(hours=1), CalendarStatus.PENDING_APPROVAL,
                                       NOW + timedelta(hours=2), 60, 0, 1)

    def read_appointment(self, appointment_id: str) -> Appointment | None:
        return self.appointment

    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None:
        return self.repository.read_profile(business_id, client_id)

    def acquire_client_send(self, business_id: str, client_id: str) -> str:
        return "synthetic-token"

    def release_client_send(self, business_id: str, client_id: str, token: str) -> None:
        assert token == "synthetic-token"


def test_sender_refuses_until_consent_then_sends() -> None:
    repository, _, store = world()
    messages = Messages()
    sender = TwilioSmsSender(messages, Records(repository), store,  # type: ignore[arg-type]
                             "pilot", "+14155550000", "+15005550009")
    intent = OutboxRecord("pilot", "outbox-1", "visit-12345678", "client", "hold-pending", 1,
                          DeliveryState.SENDING, NOW, NOW, NOW)
    with pytest.raises(PermanentDeliveryFailure, match="CONSENT_REQUIRED"):
        sender.deliver(intent)
    consent(repository, store)
    assert sender.deliver(intent) == "SM-out"
    assert messages.calls[0]["to"] == PHONE


def test_phone_change_clears_verification_and_needs_new_consent() -> None:
    repository, clients, store = world()
    consent(repository, store)
    changed = clients.save_profile("pilot", "client-1", "Synthetic Client", "+14155550102",
                                   "123 Test Street", HomeSize.SMALL, 60, True, 2, 180, NOW)
    assert changed.phone_verified_at is None
    assert repository.read_verified_phone("pilot", "+14155550102") is None
    # The old consent is for the old number; the new number has none until recorded.
    assert store.read_consent("pilot", "+14155550102") is None
    consent(repository, store, "+14155550102")
    assert repository.read_verified_phone("pilot", "+14155550102") is not None


def test_mismatched_phone_records_nothing() -> None:
    repository, _, store = world()
    with pytest.raises(ValueError, match="match"):
        consent(repository, store, "+14155550199")
    assert store.consent == {}
    profile = repository.read_profile("pilot", "client-1")
    assert profile is not None and profile.phone_verified_at is None and profile.version == 1


def test_concurrent_profile_change_fails_without_consent() -> None:
    repository, clients, store = world()
    original = repository.read_profile

    def stale_read(business_id: str, client_id: str) -> ClientProfile | None:
        repository.read_profile = original  # type: ignore[method-assign]
        profile = original(business_id, client_id)
        # The owner edits the profile after the consent check read it.
        clients.save_profile("pilot", "client-1", "Synthetic Client Two", PHONE,
                             "123 Test Street", HomeSize.SMALL, 60, True, 1, 180, NOW)
        return profile

    repository.read_profile = stale_read  # type: ignore[method-assign]
    with pytest.raises(RecordConflict):
        consent(repository, store)
    assert store.consent == {}
    current = original("pilot", "client-1")
    assert current is not None and current.phone_verified_at is None


class FakeDynamo:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.transactions: list[list[dict[str, Any]]] = []

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        self.transactions.append(kwargs["TransactItems"])
        return {}


def verified_pair() -> tuple[ConsentEvidence, ClientProfile]:
    evidence = ConsentEvidence("pilot", "client-1", "Synthetic Client", PHONE, NOW, "pilot-v1")
    profile = ClientProfile("pilot", "client-1", "Synthetic Client", PHONE, "123 Test Street",
                            HomeSize.SMALL, 60, True, 3, NOW, NOW, NOW)
    return evidence, profile


def test_dynamodb_writes_consent_and_verification_in_one_transaction() -> None:
    dynamo = FakeDynamo()
    evidence, profile = verified_pair()
    DynamoSmsIngressStore(dynamo, "scheduling").put_consent_verifying_phone(  # type: ignore[arg-type]
        evidence, profile)
    assert len(dynamo.transactions) == 1
    items = dynamo.transactions[0]
    assert [next(iter(item)) for item in items] == [
        "Put", "Update", "Update", "ConditionCheck", "ConditionCheck"]
    assert items[3]["ConditionCheck"]["Key"]["SK"]["S"].startswith("ERASURE#")
    assert items[4]["ConditionCheck"]["Key"]["SK"]["S"].startswith("ERASURE_PHONE#")
    update = items[2]["Update"]
    assert update["Key"]["SK"]["S"] == "CLIENT#client-1"
    assert "version = :old_version" in update["ConditionExpression"]
    assert "phone_e164 = :phone" in update["ConditionExpression"]
    values = update["ExpressionAttributeValues"]
    assert values[":old_version"] == {"N": "2"} and values[":new_version"] == {"N": "3"}


def test_dynamodb_version_change_is_a_clean_conflict() -> None:
    class Cancelled(Exception):
        response: ClassVar[dict[str, Any]] = {"Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": "None"}, {"Code": "None"},
                                            {"Code": "ConditionalCheckFailed"}]}

    evidence, profile = verified_pair()
    store = DynamoSmsIngressStore(FakeDynamo(Cancelled()), "scheduling")  # type: ignore[arg-type]
    with pytest.raises(RecordConflict):
        store.put_consent_verifying_phone(evidence, profile)
