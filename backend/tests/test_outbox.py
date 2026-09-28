"""Outbox recovery and duplicate delivery use synthetic notification references."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock

import pytest

from scheduling.domain.outbox import (
    ConsumeOutcome,
    ConsumeService,
    DeliveryFailure,
    DeliveryState,
    DispatchService,
    OutboxConflict,
    OutboxRecord,
    PermanentDeliveryFailure,
    consume_sqs_batch,
    decode_queue_message,
)

NOW = datetime(2026, 9, 28, 16, tzinfo=UTC)


def pending() -> OutboxRecord:
    return OutboxRecord(
        "business-1", "notice-1", "appointment-1", "client", "hold-pending", 1,
        DeliveryState.PENDING, NOW, NOW, NOW,
    )


class MemoryStore:
    def __init__(self) -> None:
        self.lock = RLock()
        self.record = pending()
        self.enqueues = 0

    def due(self, now: datetime, limit: int) -> list[tuple[str, str]]:
        with self.lock:
            record = self.record
            if record.dispatch_after is not None and record.dispatch_after <= now:
                return [(record.business_id, record.outbox_id)][:limit]
            return []

    def get(self, business_id: str, outbox_id: str) -> OutboxRecord | None:
        with self.lock:
            if (business_id, outbox_id) == (self.record.business_id, self.record.outbox_id):
                return self.record
            return None

    def mark_enqueued(
        self, record: OutboxRecord, now: datetime, next_dispatch_at: datetime
    ) -> None:
        with self.lock:
            if self.record != record:
                raise OutboxConflict("Stale dispatch")
            self.record = replace(record, dispatch_after=next_dispatch_at)
            self.enqueues += 1

    def claim(self, record: OutboxRecord, now: datetime, lease_until: datetime) -> OutboxRecord:
        with self.lock:
            if (
                self.record != record or record.next_attempt_at is None
                or record.next_attempt_at > now
                or record.state not in (
                    DeliveryState.PENDING, DeliveryState.RETRYABLE, DeliveryState.SENDING
                )
            ):
                raise OutboxConflict("Stale claim")
            self.record = replace(
                record, state=DeliveryState.SENDING, next_attempt_at=lease_until,
                dispatch_after=lease_until, attempts=record.attempts + 1,
                lease_token=f"lease-{record.attempts + 1}",
            )
            return self.record

    def mark_sent(self, record: OutboxRecord, provider_id: str, now: datetime) -> None:
        with self.lock:
            if self.record != record:
                raise OutboxConflict("Stale completion")
            self.record = replace(
                record, state=DeliveryState.SENT, next_attempt_at=None,
                dispatch_after=None, lease_token=None, provider_id=provider_id,
            )

    def mark_failure(
        self, record: OutboxRecord, state: DeliveryState, due_at: datetime | None,
        error_code: str, now: datetime
    ) -> None:
        with self.lock:
            if self.record != record:
                raise OutboxConflict("Stale completion")
            self.record = replace(
                record, state=state, next_attempt_at=due_at, dispatch_after=due_at,
                lease_token=None,
            )


class MemoryQueue:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def enqueue(self, business_id: str, outbox_id: str) -> None:
        self.messages.append((business_id, outbox_id))


class Sender:
    def __init__(self, fail_count: int = 0) -> None:
        self.calls = 0
        self.fail_count = fail_count

    def deliver(self, record: OutboxRecord) -> str:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise DeliveryFailure("PROVIDER_UNAVAILABLE")
        return f"provider-{self.calls}"


def test_crash_after_enqueue_requeues_but_only_one_logical_delivery() -> None:
    class CrashAfterEnqueue(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.crash = True

        def mark_enqueued(
            self, record: OutboxRecord, now: datetime, next_dispatch_at: datetime
        ) -> None:
            if self.crash:
                self.crash = False
                raise RuntimeError("dispatcher stopped")
            super().mark_enqueued(record, now, next_dispatch_at)

    store = CrashAfterEnqueue()
    queue = MemoryQueue()
    dispatcher = DispatchService(store, queue, lambda: NOW)

    with pytest.raises(RuntimeError):
        dispatcher.run_once()
    assert dispatcher.run_once().enqueued == 1
    assert queue.messages == [("business-1", "notice-1")] * 2

    sender = Sender()
    consumer = ConsumeService(store, sender, lambda: NOW)
    assert [consumer.consume(*message) for message in queue.messages] == [
        ConsumeOutcome.SENT, ConsumeOutcome.SKIPPED,
    ]
    assert sender.calls == 1
    assert store.record.state == DeliveryState.SENT


def test_queue_failure_keeps_intent_due_and_stale_index_result_is_skipped() -> None:
    class FailingQueue(MemoryQueue):
        def enqueue(self, business_id: str, outbox_id: str) -> None:
            raise RuntimeError("queue unavailable")

    store = MemoryStore()
    with pytest.raises(RuntimeError):
        DispatchService(store, FailingQueue(), lambda: NOW).run_once()
    assert store.record.dispatch_after == NOW
    assert store.enqueues == 0

    # The GSI may still show a due key after the base item was sent.
    class StaleIndexStore(MemoryStore):
        def due(self, now: datetime, limit: int) -> list[tuple[str, str]]:
            return [("business-1", "notice-1")]

    stale = StaleIndexStore()
    stale.record = replace(
        stale.record, state=DeliveryState.SENT, next_attempt_at=None,
        dispatch_after=None,
    )
    queue = MemoryQueue()
    assert DispatchService(stale, queue, lambda: NOW).run_once().stale == 1
    assert queue.messages == []


def test_concurrent_duplicate_consumers_claim_once() -> None:
    store = MemoryStore()
    sender = Sender()
    consumer = ConsumeService(store, sender, lambda: NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consumer.consume("business-1", "notice-1"), range(2)))
    assert sorted(results) == [ConsumeOutcome.SENT, ConsumeOutcome.SKIPPED]
    assert sender.calls == 1


def test_permanent_consent_failure_does_not_retry() -> None:
    class NoConsentSender:
        def deliver(self, record: OutboxRecord) -> str:
            raise PermanentDeliveryFailure("CONSENT_REQUIRED")

    store = MemoryStore()
    outcome = ConsumeService(store, NoConsentSender(), lambda: NOW).consume(
        "business-1", "notice-1"
    )
    assert outcome == ConsumeOutcome.FAILED
    assert store.record.state == DeliveryState.FAILED
    assert store.record.next_attempt_at is None


def test_concurrent_dispatchers_cannot_cause_two_logical_deliveries() -> None:
    store = MemoryStore()
    queue = MemoryQueue()
    dispatcher = DispatchService(store, queue, lambda: NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: dispatcher.run_once(), range(2)))

    assert 1 <= len(queue.messages) <= 2
    sender = Sender()
    consumer = ConsumeService(store, sender, lambda: NOW)
    for message in queue.messages:
        consumer.consume(*message)
    assert store.record.state == DeliveryState.SENT
    assert sender.calls == 1


def test_expired_delivery_lease_is_requeued_and_reclaimed() -> None:
    store = MemoryStore()
    store.claim(store.record, NOW, NOW + timedelta(minutes=2))
    clock = [NOW + timedelta(minutes=3)]
    queue = MemoryQueue()
    report = DispatchService(store, queue, lambda: clock[0]).run_once()
    assert report.enqueued == 1
    assert queue.messages == [("business-1", "notice-1")]
    assert ConsumeService(store, Sender(), lambda: clock[0]).consume(*queue.messages[0]) == (
        ConsumeOutcome.SENT
    )
    assert store.record.attempts == 2


def test_expired_lease_does_not_call_provider_after_attempt_limit() -> None:
    store = MemoryStore()
    store.claim(store.record, NOW, NOW + timedelta(minutes=2))
    clock = NOW + timedelta(minutes=3)
    sender = Sender()
    consumer = ConsumeService(store, sender, lambda: clock, max_attempts=1)

    assert consumer.consume("business-1", "notice-1") == ConsumeOutcome.FAILED
    assert store.record.state == DeliveryState.FAILED
    assert store.record.dispatch_after is None
    assert sender.calls == 0


def test_provider_failure_uses_backoff_and_terminal_failure() -> None:
    store = MemoryStore()
    clock = [NOW]
    sender = Sender(fail_count=3)
    consumer = ConsumeService(
        store, sender, lambda: clock[0], max_attempts=3,
        base_backoff=timedelta(seconds=30),
    )

    assert consumer.consume("business-1", "notice-1") == ConsumeOutcome.RETRY_SCHEDULED
    assert store.record.next_attempt_at == NOW + timedelta(seconds=30)
    assert consumer.consume("business-1", "notice-1") == ConsumeOutcome.SKIPPED
    clock[0] += timedelta(seconds=30)
    assert consumer.consume("business-1", "notice-1") == ConsumeOutcome.RETRY_SCHEDULED
    assert store.record.next_attempt_at == NOW + timedelta(seconds=90)
    clock[0] += timedelta(seconds=60)
    assert consumer.consume("business-1", "notice-1") == ConsumeOutcome.FAILED
    assert store.record.state == DeliveryState.FAILED
    assert store.record.dispatch_after is None
    assert sender.calls == 3


def test_partial_batch_failure_redrives_only_invalid_record() -> None:
    store = MemoryStore()
    sender = Sender()
    consumer = ConsumeService(store, sender, lambda: NOW)
    valid = json.dumps({"business_id": "business-1", "outbox_id": "notice-1"})
    result = consume_sqs_batch(
        [
            {"messageId": "good", "body": valid},
            {"messageId": "poison", "body": "{}"},
            {"messageId": "duplicate", "body": valid},
        ],
        consumer,
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "poison"}]}
    assert sender.calls == 1
    with pytest.raises(ValueError):
        decode_queue_message("{}")
