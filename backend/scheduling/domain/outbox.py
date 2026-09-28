"""Provider-independent dispatch and delivery of committed notification intents."""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4


class DeliveryState(StrEnum):
    PENDING = "PENDING"
    RETRYABLE = "RETRYABLE"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class OutboxConflict(Exception):
    """Another worker changed the authoritative outbox record."""


class DeliveryFailure(Exception):
    """Expected provider rejection, with a safe error code and no message content."""

    def __init__(self, code: str) -> None:
        if not code or not code.replace("_", "").isalnum():
            raise ValueError("Delivery failure code must be a safe identifier")
        self.code = code
        super().__init__(code)


class PermanentDeliveryFailure(DeliveryFailure):
    """A policy or data gate that another provider attempt cannot repair."""


@dataclass(frozen=True)
class OutboxRecord:
    business_id: str
    outbox_id: str
    entity_id: str
    recipient: str
    template: str
    event_version: int
    state: DeliveryState
    created_at: datetime
    next_attempt_at: datetime | None
    dispatch_after: datetime | None
    attempts: int = 0
    lease_token: str | None = None
    provider_id: str | None = None


class OutboxStore(Protocol):
    def due(self, now: datetime, limit: int) -> Iterable[tuple[str, str]]: ...

    def get(self, business_id: str, outbox_id: str) -> OutboxRecord | None: ...

    def mark_enqueued(
        self, record: OutboxRecord, now: datetime, next_dispatch_at: datetime
    ) -> None: ...

    def claim(self, record: OutboxRecord, now: datetime, lease_until: datetime) -> OutboxRecord: ...

    def mark_sent(self, record: OutboxRecord, provider_id: str, now: datetime) -> None: ...

    def mark_failure(
        self, record: OutboxRecord, state: DeliveryState, due_at: datetime | None,
        error_code: str, now: datetime
    ) -> None: ...


class IntentQueue(Protocol):
    def enqueue(self, business_id: str, outbox_id: str) -> None: ...


class NotificationSender(Protocol):
    def deliver(self, record: OutboxRecord) -> str: ...


@dataclass(frozen=True)
class DispatchReport:
    examined: int
    enqueued: int
    stale: int
    oldest_due_age_seconds: float


class DispatchService:
    def __init__(
        self, store: OutboxStore, queue: IntentQueue, clock: Callable[[], datetime],
        *, batch_size: int = 100, redispatch_delay: timedelta = timedelta(minutes=5),
    ) -> None:
        if batch_size <= 0 or redispatch_delay <= timedelta(0):
            raise ValueError("Dispatch bounds must be positive")
        self._store = store
        self._queue = queue
        self._clock = clock
        self._batch_size = batch_size
        self._redispatch_delay = redispatch_delay

    def run_once(self) -> DispatchReport:
        now = _utc(self._clock())
        examined = enqueued = stale = 0
        oldest_age = 0.0
        for business_id, outbox_id in self._store.due(now, self._batch_size):
            examined += 1
            record = self._store.get(business_id, outbox_id)
            if record is None or not _is_dispatch_due(record, now):
                stale += 1
                continue
            oldest_age = max(oldest_age, (now - record.created_at).total_seconds())
            # A crash here can create another queue message. The consumer's
            # conditional claim makes duplicate messages harmless to state.
            self._queue.enqueue(record.business_id, record.outbox_id)
            enqueued += 1
            try:
                self._store.mark_enqueued(record, now, now + self._redispatch_delay)
            except OutboxConflict:
                # The consumer may already have claimed or completed this intent.
                pass
        return DispatchReport(examined, enqueued, stale, oldest_age)


class ConsumeOutcome(StrEnum):
    SKIPPED = "SKIPPED"
    SENT = "SENT"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED = "FAILED"


class ConsumeService:
    def __init__(
        self, store: OutboxStore, sender: NotificationSender, clock: Callable[[], datetime],
        *, lease: timedelta = timedelta(minutes=2), max_attempts: int = 5,
        base_backoff: timedelta = timedelta(seconds=30),
        max_backoff: timedelta = timedelta(hours=1),
    ) -> None:
        if (
            lease <= timedelta(0) or max_attempts <= 0 or base_backoff <= timedelta(0)
            or max_backoff < base_backoff
        ):
            raise ValueError("Delivery retry bounds are invalid")
        self._store = store
        self._sender = sender
        self._clock = clock
        self._lease = lease
        self._max_attempts = max_attempts
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff

    def consume(self, business_id: str, outbox_id: str) -> ConsumeOutcome:
        now = _utc(self._clock())
        record = self._store.get(business_id, outbox_id)
        if record is None or not _is_claimable(record, now):
            return ConsumeOutcome.SKIPPED
        try:
            claimed = self._store.claim(record, now, now + self._lease)
        except OutboxConflict:
            return ConsumeOutcome.SKIPPED

        if claimed.attempts > self._max_attempts:
            self._store.mark_failure(
                claimed, DeliveryState.FAILED, None, "LEASE_EXHAUSTED", now
            )
            return ConsumeOutcome.FAILED

        try:
            provider_id = self._sender.deliver(claimed)
            if not provider_id:
                raise ValueError("Sender must return a provider message ID")
            self._store.mark_sent(claimed, provider_id, _utc(self._clock()))
            return ConsumeOutcome.SENT
        except PermanentDeliveryFailure as exc:
            self._store.mark_failure(
                claimed, DeliveryState.FAILED, None, exc.code, _utc(self._clock())
            )
            return ConsumeOutcome.FAILED
        except DeliveryFailure as exc:
            failed_at = _utc(self._clock())
            if claimed.attempts >= self._max_attempts:
                self._store.mark_failure(
                    claimed, DeliveryState.FAILED, None, exc.code, failed_at
                )
                return ConsumeOutcome.FAILED
            delay = min(
                self._base_backoff * (2 ** (claimed.attempts - 1)), self._max_backoff
            )
            self._store.mark_failure(
                claimed, DeliveryState.RETRYABLE, failed_at + delay, exc.code, failed_at
            )
            return ConsumeOutcome.RETRY_SCHEDULED


def decode_queue_message(body: str) -> tuple[str, str]:
    """Invalid SQS messages raise so redrive can move them to the DLQ."""
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise TypeError("Queue body must be an object")
    business_id, outbox_id = payload.get("business_id"), payload.get("outbox_id")
    if not isinstance(business_id, str) or not business_id:
        raise ValueError("Queue body needs business_id")
    if not isinstance(outbox_id, str) or not outbox_id:
        raise ValueError("Queue body needs outbox_id")
    return business_id, outbox_id


def consume_sqs_batch(
    records: Iterable[dict[str, str]], consumer: ConsumeService
) -> dict[str, list[dict[str, str]]]:
    """Return Lambda partial failures; repeated poison messages reach the SQS DLQ."""
    failures: list[dict[str, str]] = []
    for message in records:
        message_id = message["messageId"]
        try:
            business_id, outbox_id = decode_queue_message(message["body"])
            consumer.consume(business_id, outbox_id)
        except Exception:  # noqa: BLE001 - every failed record must be returned for SQS redrive
            # The caller logs the safe message ID, not the body or recipient.
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def new_lease_token() -> str:
    return uuid4().hex


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Worker clock must return a timezone-aware instant")
    return value.astimezone(UTC)


def _is_claimable(record: OutboxRecord, now: datetime) -> bool:
    return (
        record.state in (DeliveryState.PENDING, DeliveryState.RETRYABLE, DeliveryState.SENDING)
        and record.next_attempt_at is not None and record.next_attempt_at <= now
    )


def _is_dispatch_due(record: OutboxRecord, now: datetime) -> bool:
    return (
        _is_claimable(record, now)
        and record.dispatch_after is not None and record.dispatch_after <= now
    )
