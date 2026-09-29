"""Opt-in transaction proof against an actual DynamoDB Local endpoint."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
import pytest

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.outbox_aws import DynamoOutboxStore
from scheduling.domain.appointments import Appointment
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.holds import OutboxIntent, RevisionConflict
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    TransitionCommit,
    TransitionResult,
)
from scheduling.domain.outbox import ConsumeOutcome, ConsumeService, DispatchService, OutboxRecord

BUSINESS = "synthetic-business"
START = datetime(2026, 10, 1, 16, tzinfo=UTC)
EXPIRY = datetime(2026, 9, 30, 16, tzinfo=UTC)


@pytest.fixture
def local_table() -> tuple[Any, str]:
    url = os.environ.get("DYNAMODB_LOCAL_URL")
    if not url:
        pytest.skip("Set DYNAMODB_LOCAL_URL to an isolated DynamoDB Local endpoint")
    if urlsplit(url).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("DYNAMODB_LOCAL_URL must use loopback, never an AWS endpoint")
    client = boto3.client(
        "dynamodb", endpoint_url=url, region_name="us-west-1",
        aws_access_key_id="synthetic", aws_secret_access_key="synthetic",
    )
    name = f"scheduling-race-{uuid4().hex[:12]}"
    client.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "hold_due_pk", "AttributeType": "S"},
            {"AttributeName": "hold_due_sk", "AttributeType": "S"},
            {"AttributeName": "outbox_due_pk", "AttributeType": "S"},
            {"AttributeName": "outbox_due_sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "HoldDueIndex",
                "KeySchema": [
                    {"AttributeName": "hold_due_pk", "KeyType": "HASH"},
                    {"AttributeName": "hold_due_sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            },
            {
                "IndexName": "OutboxDueIndex",
                "KeySchema": [
                    {"AttributeName": "outbox_due_pk", "KeyType": "HASH"},
                    {"AttributeName": "outbox_due_sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            },
        ],
    )
    client.get_waiter("table_exists").wait(TableName=name)
    try:
        yield client, name
    finally:
        client.delete_table(TableName=name)


def _appointment(appointment_id: str, status: CalendarStatus,
                 *, replacement_of: str | None = None, version: int = 1) -> Appointment:
    return Appointment(
        appointment_id, BUSINESS, "synthetic-client", START, START + timedelta(hours=1),
        status, EXPIRY if status == CalendarStatus.PENDING_APPROVAL else None,
        60, 30, version, replacement_of,
    )


def _seed(client: Any, table: str, appointments: tuple[Appointment, ...],
          *, guard_original: str | None = None) -> None:
    repo = DynamoDBCalendarRepository(client, table)
    client.put_item(
        TableName=table,
        Item={**repo._business_key(BUSINESS, "CALENDAR#REVISION"), "revision": {"N": "7"}},
    )
    for appointment in appointments:
        client.put_item(TableName=table, Item=repo._appointment_item(appointment))
        client.put_item(TableName=table, Item=repo._event_item(appointment))
    if guard_original:
        client.put_item(
            TableName=table,
            Item={**repo._business_key(BUSINESS, f"REPLACEMENT#{guard_original}"),
                  "replacement_id": {"S": "replacement"},
                  "expires_at": {"S": EXPIRY.isoformat(timespec="microseconds")}},
        )


def _commit(before: Appointment, after: Appointment, action: Action,
            decision_at: datetime, *, replaced: Appointment | None = None,
            clear_guard: bool = False) -> TransitionCommit:
    command = AppointmentCommand(
        BUSINESS, before.appointment_id, "owner" if action != Action.EXPIRE else "system",
        ActorRole.SYSTEM if action == Action.EXPIRE else ActorRole.OWNER,
        action, f"{action.value}-{before.appointment_id}", before.version,
    )
    return TransitionCommit(
        command, command.request_hash(), before, TransitionResult(after, 8, replaced),
        f"audit-{action.value}-{before.appointment_id}",
        (OutboxIntent(f"notice-{action.value}-{before.appointment_id}",
                      before.appointment_id, "client", action.value),),
        decision_at, clear_guard,
    )


def _run_race(repository: DynamoDBCalendarRepository,
              first: TransitionCommit, second: TransitionCommit) -> None:
    barrier = Barrier(2)

    def attempt(commit: TransitionCommit) -> None:
        barrier.wait(timeout=5)
        repository.commit_transition(7, commit)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, commit) for commit in (first, second)]
        failures = 0
        for future in futures:
            try:
                future.result()
            except RevisionConflict:
                failures += 1
    assert failures == 1


def _get(client: Any, table: str, pk: str, sk: str) -> dict[str, Any] | None:
    response = client.get_item(
        TableName=table, Key={"PK": {"S": pk}, "SK": {"S": sk}}, ConsistentRead=True,
    )
    return response.get("Item")


def _business_items(client: Any, table: str) -> list[dict[str, Any]]:
    page = client.query(
        TableName=table, KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"BUSINESS#{BUSINESS}"}},
        ConsistentRead=True,
    )
    return page["Items"]


def test_approval_and_expiry_commit_only_one_atomic_result(
    local_table: tuple[Any, str],
) -> None:
    client, table = local_table
    before = _appointment("request", CalendarStatus.PENDING_APPROVAL)
    _seed(client, table, (before,))
    repo = DynamoDBCalendarRepository(client, table)
    approve = _commit(before, replace(before, status=CalendarStatus.CONFIRMED, version=2),
                      Action.APPROVE, EXPIRY - timedelta(seconds=1))
    expire = _commit(before, replace(before, status=CalendarStatus.EXPIRED, version=2),
                     Action.EXPIRE, EXPIRY + timedelta(seconds=1))
    _run_race(repo, approve, expire)

    metadata = _get(client, table, "APPOINTMENT#request", "META")
    assert metadata is not None
    assert metadata["status"]["S"] in {"CONFIRMED", "EXPIRED"}
    items = _business_items(client, table)
    assert len([item for item in items if item["SK"]["S"].startswith("AUDIT#")]) == 1
    assert len([item for item in items if item["SK"]["S"].startswith("OUTBOX#")]) == 1
    event = next((item for item in items if item["SK"]["S"].startswith("EVENT#")), None)
    assert (event is not None) == (metadata["status"]["S"] == "CONFIRMED")
    assert _get(client, table, f"BUSINESS#{BUSINESS}", "CALENDAR#REVISION")["revision"]["N"] == "8"


def test_replacement_swap_rejects_adversarial_original_cancellation_atomically(
    local_table: tuple[Any, str],
) -> None:
    client, table = local_table
    original = _appointment("original", CalendarStatus.CONFIRMED, version=2)
    replacement = _appointment("replacement", CalendarStatus.PENDING_APPROVAL,
                               replacement_of="original")
    _seed(client, table, (original, replacement), guard_original="original")
    repo = DynamoDBCalendarRepository(client, table)
    swapped_original = replace(original, status=CalendarStatus.CANCELLED, version=3)
    approval = _commit(
        replacement, replace(replacement, status=CalendarStatus.CONFIRMED, version=2),
        Action.APPROVE, EXPIRY - timedelta(seconds=1),
        replaced=swapped_original, clear_guard=True,
    )
    cancellation = _commit(original, swapped_original, Action.CANCEL,
                           EXPIRY - timedelta(seconds=1))
    _run_race(repo, approval, cancellation)

    original_item = _get(client, table, "APPOINTMENT#original", "META")
    replacement_item = _get(client, table, "APPOINTMENT#replacement", "META")
    assert original_item is not None and original_item["status"]["S"] == "CANCELLED"
    assert replacement_item is not None
    assert replacement_item["status"]["S"] in {"CONFIRMED", "PENDING_APPROVAL"}
    items = _business_items(client, table)
    assert len([item for item in items if item["SK"]["S"].startswith("AUDIT#")]) == 1
    assert len([item for item in items if item["SK"]["S"].startswith("OUTBOX#")]) == 1
    events = [item["event_id"]["S"] for item in items
              if item["SK"]["S"].startswith("EVENT#")]
    assert "original" not in events
    assert "replacement" in events
    guard = _get(client, table, f"BUSINESS#{BUSINESS}", "REPLACEMENT#original")
    assert (guard is None) == (replacement_item["status"]["S"] == "CONFIRMED")


def test_committed_outbox_dispatches_and_fake_consumer_claims_once(
    local_table: tuple[Any, str],
) -> None:
    client, table = local_table
    before = _appointment("request", CalendarStatus.PENDING_APPROVAL)
    _seed(client, table, (before,))
    decision_at = EXPIRY - timedelta(seconds=1)
    commit = _commit(
        before, replace(before, status=CalendarStatus.CONFIRMED, version=2),
        Action.APPROVE, decision_at,
    )
    DynamoDBCalendarRepository(client, table).commit_transition(7, commit)

    class FakeQueue:
        def __init__(self) -> None:
            self.messages: list[tuple[str, str]] = []

        def enqueue(self, business_id: str, outbox_id: str) -> None:
            self.messages.append((business_id, outbox_id))

    class FakeSender:
        def __init__(self) -> None:
            self.deliveries: list[str] = []

        def deliver(self, record: OutboxRecord) -> str:
            self.deliveries.append(record.outbox_id)
            return "synthetic-provider-id"

    store = DynamoOutboxStore(client, table)
    deadline = monotonic() + 5
    while not list(store.due(decision_at, 100)):
        if monotonic() >= deadline:
            pytest.fail("Committed outbox item did not appear in the local due index")
        sleep(0.05)
    queue = FakeQueue()
    report = DispatchService(store, queue, lambda: decision_at).run_once()
    outbox_id = commit.outbox[0].outbox_id
    assert report.enqueued == 1
    assert queue.messages == [(BUSINESS, outbox_id)]
    sender = FakeSender()
    consumer = ConsumeService(store, sender, lambda: decision_at)
    assert consumer.consume(*queue.messages[0]) == ConsumeOutcome.SENT
    assert consumer.consume(*queue.messages[0]) == ConsumeOutcome.SKIPPED
    assert sender.deliveries == [outbox_id]
    item = _get(client, table, f"BUSINESS#{BUSINESS}", f"OUTBOX#{outbox_id}")
    assert item is not None
    assert item["delivery_state"]["S"] == "SENT"
    assert item["provider_id"]["S"] == "synthetic-provider-id"
    assert "outbox_due_pk" not in item
