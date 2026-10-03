"""One welcome SMS after a new client's first in-person consent, and never otherwise."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.adapters.sms_twilio import TwilioSmsSender
from scheduling.domain.client_records import (
    ClientProfile,
    ClientRecordService,
    HomeSize,
    RecordConflict,
)
from scheduling.domain.outbox import OutboxRecord, PermanentDeliveryFailure
from scheduling.domain.sms_ingress import (
    WELCOME_TEXT,
    ConsentEvidence,
    record_in_person_consent,
)

NOW = datetime(2026, 10, 3, 17, tzinfo=UTC)
PHONE = "+15005550006"
FROM = "+14155550000"
OWNER = "+15005550009"


class Store:
    """Memory store with the all-or-nothing contract of the DynamoDB one."""

    def __init__(self, repository: InMemoryCalendarRepository) -> None:
        self.repository = repository
        self.consent: dict[str, ConsentEvidence] = {}
        self.outbox: dict[str, OutboxRecord] = {}
        self.opted_out = False
        self.fail = False

    def put_consent_verifying_phone(self, evidence: ConsentEvidence, verified: ClientProfile,
                                    welcome: OutboxRecord | None = None) -> None:
        if self.fail:
            raise RecordConflict("changed")
        self.repository.save_profile(verified, verified.version - 1, verified.phone_e164)
        self.consent[evidence.phone_e164] = evidence
        if welcome is not None:
            self.outbox.setdefault(welcome.outbox_id, welcome)

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return self.consent.get(phone_e164)

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return self.opted_out

    def record_outbound(self, *args: object) -> None:
        pass


class Messages:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create(self, **kwargs: str) -> Any:
        self.calls.append(kwargs)
        return type("Sent", (), {"sid": f"SM-{len(self.calls)}"})()


def world() -> tuple[InMemoryCalendarRepository, ClientRecordService, Store, Messages,
                     TwilioSmsSender]:
    repository = InMemoryCalendarRepository()
    clients = ClientRecordService(repository)
    clients.save_profile("pilot", "client-1", "Synthetic Client", PHONE, "123 Test Street",
                         HomeSize.SMALL, 60, True, 0, 180, NOW)
    store = Store(repository)
    messages = Messages()
    sender = TwilioSmsSender(messages, repository, store,  # type: ignore[arg-type]
                             "pilot", FROM, OWNER)
    return repository, clients, store, messages, sender


def consent(repository: InMemoryCalendarRepository, store: Store, phone: str = PHONE,
            at: datetime = NOW) -> None:
    record_in_person_consent(store, repository, "pilot", "client-1", phone,  # type: ignore[arg-type]
                             "Synthetic Client", "1", at)


def test_new_client_consent_queues_one_welcome_that_sends_the_documented_text() -> None:
    repository, _, store, messages, sender = world()
    consent(repository, store)
    assert list(store.outbox) == ["welcome#client-1"]
    assert sender.deliver(store.outbox["welcome#client-1"]) == "SM-1"
    assert messages.calls[0]["to"] == PHONE
    assert messages.calls[0]["body"] == WELCOME_TEXT
    assert WELCOME_TEXT.startswith("Smart Scheduling Assistant: You're enrolled")


def test_failed_capture_queues_nothing() -> None:
    repository, _, store, _, _ = world()
    store.fail = True
    with pytest.raises(RecordConflict):
        consent(repository, store)
    with pytest.raises(ValueError, match="match"):
        consent(repository, store, "+14155550199")
    assert store.outbox == {} and store.consent == {}


def test_repeat_consent_and_start_after_stop_send_no_second_welcome() -> None:
    repository, _, store, _, _ = world()
    consent(repository, store)
    consent(repository, store, at=NOW + timedelta(days=30))  # repeat or re-subscribe
    assert len(store.outbox) == 1


def test_existing_client_with_verified_phone_gets_no_welcome() -> None:
    repository, _, store, _, _ = world()
    store.consent[PHONE] = ConsentEvidence("pilot", "client-1", "Synthetic Client", PHONE,
                                           NOW, "1")
    profile = repository.read_profile("pilot", "client-1")
    assert profile is not None
    from dataclasses import replace
    repository.save_profile(replace(profile, version=2, phone_verified_at=NOW), 1, PHONE)
    consent(repository, store)
    assert store.outbox == {}


def test_welcome_is_refused_for_opt_out_changed_phone_and_missing_consent() -> None:
    repository, clients, store, messages, sender = world()
    consent(repository, store)
    welcome = store.outbox["welcome#client-1"]
    store.opted_out = True
    with pytest.raises(PermanentDeliveryFailure, match="OPTED_OUT"):
        sender.deliver(welcome)
    store.opted_out = False
    clients.save_profile("pilot", "client-1", "Synthetic Client", "+14155550102",
                         "123 Test Street", HomeSize.SMALL, 60, True, 2, 180, NOW)
    with pytest.raises(PermanentDeliveryFailure, match="CONSENT_REQUIRED"):
        sender.deliver(welcome)
    store.consent.clear()
    with pytest.raises(PermanentDeliveryFailure, match="CONSENT_REQUIRED"):
        sender.deliver(welcome)
    assert messages.calls == []


def test_welcome_for_an_old_number_is_superseded_after_phone_change_and_reconsent() -> None:
    repository, clients, store, messages, sender = world()
    consent(repository, store)
    welcome = store.outbox["welcome#client-1"]
    clients.save_profile("pilot", "client-1", "Synthetic Client", "+14155550102",
                         "123 Test Street", HomeSize.SMALL, 60, True, 2, 180, NOW)
    consent(repository, store, "+14155550102")
    assert len(store.outbox) == 1  # the same client is never welcomed twice
    with pytest.raises(PermanentDeliveryFailure, match="EVENT_SUPERSEDED"):
        sender.deliver(welcome)
    assert messages.calls == []


class Dynamo:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.transactions: list[list[dict[str, Any]]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return {"Item": {"PK": {"S": "x"}}} if kwargs["Key"]["SK"]["S"] in self.existing else {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transactions.append(kwargs["TransactItems"])
        return {}


def _pair() -> tuple[ConsentEvidence, ClientProfile, OutboxRecord]:
    evidence = ConsentEvidence("pilot", "client-1", "Synthetic Client", PHONE, NOW, "1")
    profile = ClientProfile("pilot", "client-1", "Synthetic Client", PHONE, "123 Test Street",
                            HomeSize.SMALL, 60, True, 2, NOW, NOW, NOW)
    welcome = OutboxRecord("pilot", "welcome#client-1", "client-1", "client", "welcome", 2,
                           __import__("scheduling.domain.outbox", fromlist=["x"]).DeliveryState.PENDING,
                           NOW, NOW, NOW)
    return evidence, profile, welcome


def test_dynamodb_welcome_is_in_the_same_transaction_and_conditional() -> None:
    dynamo = Dynamo()
    evidence, profile, welcome = _pair()
    DynamoSmsIngressStore(dynamo, "t").put_consent_verifying_phone(  # type: ignore[arg-type]
        evidence, profile, welcome)
    assert len(dynamo.transactions) == 1
    put = dynamo.transactions[0][-1]["Put"]
    assert put["Item"]["SK"]["S"] == "OUTBOX#welcome#client-1"
    assert put["Item"]["template"]["S"] == "welcome"
    assert put["ConditionExpression"] == "attribute_not_exists(PK)"


def test_dynamodb_skips_welcome_that_already_exists() -> None:
    dynamo = Dynamo({"OUTBOX#welcome#client-1"})
    evidence, profile, welcome = _pair()
    DynamoSmsIngressStore(dynamo, "t").put_consent_verifying_phone(  # type: ignore[arg-type]
        evidence, profile, welcome)
    assert all("Put" not in item or "OUTBOX" not in item["Put"]["Item"]["SK"]["S"]
               for item in dynamo.transactions[0])
