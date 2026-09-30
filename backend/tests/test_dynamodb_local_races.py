"""Opt-in transaction proof against DynamoDB Local or the deployed synthetic dev table."""

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
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

DEV_TABLE = "scheduling-dev"
DEV_REGION = "us-west-1"
START = datetime(2026, 10, 1, 16, tzinfo=UTC)
EXPIRY = datetime(2026, 9, 30, 16, tzinfo=UTC)


@dataclass
class RaceEnv:
    """One table plus the synthetic identifiers owned by a single test run."""

    client: Any
    table: str
    business: str
    run: str
    appointment_ids: list[str] = field(default_factory=list)

    def appointment_id(self, name: str) -> str:
        # Appointment keys are not business-scoped, so the run id keeps them unique.
        identifier = f"{self.run}-{name}"
        self.appointment_ids.append(identifier)
        return identifier

    def scrub(self) -> None:
        """Delete only the items this run created (its business and appointment keys)."""
        partitions = [f"BUSINESS#{self.business}"]
        partitions += [f"APPOINTMENT#{identifier}" for identifier in self.appointment_ids]
        for pk in partitions:
            last_key: dict[str, Any] | None = None
            while True:
                arguments: dict[str, Any] = {
                    "TableName": self.table, "KeyConditionExpression": "PK = :pk",
                    "ExpressionAttributeValues": {":pk": {"S": pk}},
                    "ProjectionExpression": "PK, SK", "ConsistentRead": True,
                }
                if last_key:
                    arguments["ExclusiveStartKey"] = last_key
                page = self.client.query(**arguments)
                for item in page["Items"]:
                    self.client.delete_item(
                        TableName=self.table, Key={"PK": item["PK"], "SK": item["SK"]},
                    )
                last_key = page.get("LastEvaluatedKey")
                if not last_key:
                    break


def _dev_env() -> Iterator[RaceEnv]:
    table = os.environ["SCHEDULING_DEV_TABLE"]
    region = os.environ.get("SCHEDULING_DEV_REGION", DEV_REGION)
    if table != DEV_TABLE:
        raise ValueError(f"SCHEDULING_DEV_TABLE must be {DEV_TABLE!r}, never another table")
    if region != DEV_REGION:
        raise ValueError(f"SCHEDULING_DEV_REGION must be {DEV_REGION!r}")
    if os.environ.get("DYNAMODB_LOCAL_URL") or os.environ.get("AWS_ENDPOINT_URL"):
        raise ValueError("Dev-table mode must not be combined with a custom endpoint")
    if os.environ.get("CI"):
        pytest.skip("The deployed dev table mode never runs in CI")
    # Standard credential chain (for example AWS_PROFILE=scheduling-dev-deployer).
    client = boto3.client("dynamodb", region_name=DEV_REGION)
    run = f"run-{uuid4().hex[:12]}"
    env = RaceEnv(client, table, f"synthetic-{run}", run)  # never "dev-synthetic"
    try:
        yield env
    finally:
        env.scrub()


def _local_env() -> Iterator[RaceEnv]:
    url = os.environ.get("DYNAMODB_LOCAL_URL")
    if not url:
        pytest.skip(
            "Set DYNAMODB_LOCAL_URL to an isolated DynamoDB Local endpoint "
            "or SCHEDULING_DEV_TABLE=scheduling-dev to use the deployed dev table"
        )
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
    run = f"run-{uuid4().hex[:12]}"
    try:
        yield RaceEnv(client, name, "synthetic-business", run)
    finally:
        client.delete_table(TableName=name)


@pytest.fixture
def race_env() -> Iterator[RaceEnv]:
    """DynamoDB Local by default; the deployed synthetic dev table when opted in."""
    if os.environ.get("SCHEDULING_DEV_TABLE"):
        yield from _dev_env()
    else:
        yield from _local_env()


def _appointment(env: RaceEnv, name: str, status: CalendarStatus,
                 *, replacement_of: str | None = None, version: int = 1) -> Appointment:
    return Appointment(
        env.appointment_id(name), env.business, "synthetic-client", START,
        START + timedelta(hours=1),
        status, EXPIRY if status == CalendarStatus.PENDING_APPROVAL else None,
        60, 30, version, replacement_of,
    )


def _seed(env: RaceEnv, appointments: tuple[Appointment, ...],
          *, guard_original: str | None = None) -> None:
    client, table = env.client, env.table
    repo = DynamoDBCalendarRepository(client, table)
    client.put_item(
        TableName=table,
        Item={**repo._business_key(env.business, "CALENDAR#REVISION"), "revision": {"N": "7"}},
    )
    for appointment in appointments:
        client.put_item(TableName=table, Item=repo._appointment_item(appointment))
        client.put_item(TableName=table, Item=repo._event_item(appointment))
    if guard_original:
        client.put_item(
            TableName=table,
            Item={**repo._business_key(env.business, f"REPLACEMENT#{guard_original}"),
                  "replacement_id": {"S": "replacement"},
                  "expires_at": {"S": EXPIRY.isoformat(timespec="microseconds")}},
        )


def _commit(env: RaceEnv, before: Appointment, after: Appointment, action: Action,
            decision_at: datetime, *, replaced: Appointment | None = None,
            clear_guard: bool = False) -> TransitionCommit:
    command = AppointmentCommand(
        env.business, before.appointment_id, "owner" if action != Action.EXPIRE else "system",
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


def _get(env: RaceEnv, pk: str, sk: str) -> dict[str, Any] | None:
    response = env.client.get_item(
        TableName=env.table, Key={"PK": {"S": pk}, "SK": {"S": sk}}, ConsistentRead=True,
    )
    return response.get("Item")


def _business_items(env: RaceEnv) -> list[dict[str, Any]]:
    page = env.client.query(
        TableName=env.table, KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"BUSINESS#{env.business}"}},
        ConsistentRead=True,
    )
    return list(page["Items"])


def _wait_until(condition: Callable[[], bool], message: str, seconds: float = 30) -> None:
    """Bounded polling for eventually consistent GSI visibility."""
    deadline = monotonic() + seconds
    while not condition():
        if monotonic() >= deadline:
            pytest.fail(message)
        sleep(0.25)


def test_two_approvals_for_one_slot_commit_at_most_one(race_env: RaceEnv) -> None:
    env = race_env
    first = _appointment(env, "first", CalendarStatus.PENDING_APPROVAL)
    second = _appointment(env, "second", CalendarStatus.PENDING_APPROVAL)
    _seed(env, (first, second))
    decision_at = EXPIRY - timedelta(seconds=1)
    approvals = [
        _commit(env, request, replace(request, status=CalendarStatus.CONFIRMED, version=2),
                Action.APPROVE, decision_at)
        for request in (first, second)
    ]
    _run_race(DynamoDBCalendarRepository(env.client, env.table), approvals[0], approvals[1])

    statuses = {}
    for request in (first, second):
        metadata = _get(env, f"APPOINTMENT#{request.appointment_id}", "META")
        assert metadata is not None
        statuses[request.appointment_id] = metadata["status"]["S"]
    assert sorted(statuses.values()) == ["CONFIRMED", "PENDING_APPROVAL"]
    winner = next(key for key, value in statuses.items() if value == "CONFIRMED")
    items = _business_items(env)
    assert len([item for item in items if item["SK"]["S"].startswith("AUDIT#")]) == 1
    outbox = [item["SK"]["S"] for item in items if item["SK"]["S"].startswith("OUTBOX#")]
    assert len(outbox) == 1 and winner in outbox[0]
    revision = _get(env, f"BUSINESS#{env.business}", "CALENDAR#REVISION")
    assert revision is not None and revision["revision"]["N"] == "8"


def test_approval_and_expiry_commit_only_one_atomic_result(race_env: RaceEnv) -> None:
    env = race_env
    before = _appointment(env, "request", CalendarStatus.PENDING_APPROVAL)
    _seed(env, (before,))
    repo = DynamoDBCalendarRepository(env.client, env.table)
    approve = _commit(env, before, replace(before, status=CalendarStatus.CONFIRMED, version=2),
                      Action.APPROVE, EXPIRY - timedelta(seconds=1))
    expire = _commit(env, before, replace(before, status=CalendarStatus.EXPIRED, version=2),
                     Action.EXPIRE, EXPIRY + timedelta(seconds=1))
    _run_race(repo, approve, expire)

    metadata = _get(env, f"APPOINTMENT#{before.appointment_id}", "META")
    assert metadata is not None
    assert metadata["status"]["S"] in {"CONFIRMED", "EXPIRED"}
    items = _business_items(env)
    assert len([item for item in items if item["SK"]["S"].startswith("AUDIT#")]) == 1
    assert len([item for item in items if item["SK"]["S"].startswith("OUTBOX#")]) == 1
    event = next((item for item in items if item["SK"]["S"].startswith("EVENT#")), None)
    assert (event is not None) == (metadata["status"]["S"] == "CONFIRMED")
    revision = _get(env, f"BUSINESS#{env.business}", "CALENDAR#REVISION")
    assert revision is not None and revision["revision"]["N"] == "8"


def test_replacement_swap_rejects_adversarial_original_cancellation_atomically(
    race_env: RaceEnv,
) -> None:
    env = race_env
    original = _appointment(env, "original", CalendarStatus.CONFIRMED, version=2)
    replacement = _appointment(env, "replacement", CalendarStatus.PENDING_APPROVAL,
                               replacement_of=original.appointment_id)
    _seed(env, (original, replacement), guard_original=original.appointment_id)
    repo = DynamoDBCalendarRepository(env.client, env.table)
    swapped_original = replace(original, status=CalendarStatus.CANCELLED, version=3)
    approval = _commit(
        env, replacement, replace(replacement, status=CalendarStatus.CONFIRMED, version=2),
        Action.APPROVE, EXPIRY - timedelta(seconds=1),
        replaced=swapped_original, clear_guard=True,
    )
    cancellation = _commit(env, original, swapped_original, Action.CANCEL,
                           EXPIRY - timedelta(seconds=1))
    _run_race(repo, approval, cancellation)

    original_item = _get(env, f"APPOINTMENT#{original.appointment_id}", "META")
    replacement_item = _get(env, f"APPOINTMENT#{replacement.appointment_id}", "META")
    assert original_item is not None and original_item["status"]["S"] == "CANCELLED"
    assert replacement_item is not None
    assert replacement_item["status"]["S"] in {"CONFIRMED", "PENDING_APPROVAL"}
    items = _business_items(env)
    assert len([item for item in items if item["SK"]["S"].startswith("AUDIT#")]) == 1
    assert len([item for item in items if item["SK"]["S"].startswith("OUTBOX#")]) == 1
    events = [item["event_id"]["S"] for item in items
              if item["SK"]["S"].startswith("EVENT#")]
    assert original.appointment_id not in events
    assert replacement.appointment_id in events
    guard = _get(env, f"BUSINESS#{env.business}", f"REPLACEMENT#{original.appointment_id}")
    assert (guard is None) == (replacement_item["status"]["S"] == "CONFIRMED")


def test_hold_due_index_shows_pending_hold_then_drops_it_after_approval(
    race_env: RaceEnv,
) -> None:
    env = race_env
    before = _appointment(env, "request", CalendarStatus.PENDING_APPROVAL)
    _seed(env, (before,))
    repo = DynamoDBCalendarRepository(env.client, env.table)
    # Mirror the hold-creation item: the due keys wake the expiry worker.
    env.client.update_item(
        TableName=env.table,
        Key={"PK": {"S": f"APPOINTMENT#{before.appointment_id}"}, "SK": {"S": "META"}},
        UpdateExpression="SET hold_due_pk = :pk, hold_due_sk = :sk",
        ExpressionAttributeValues={
            ":pk": {"S": "HOLD#PENDING"},
            ":sk": {"S": f"{EXPIRY.isoformat(timespec='microseconds')}#{before.appointment_id}"},
        },
    )
    now = EXPIRY + timedelta(seconds=1)
    _wait_until(lambda: before.appointment_id in repo.due_hold_ids(now, 100),
                "Pending hold did not appear in HoldDueIndex")
    commit = _commit(env, before, replace(before, status=CalendarStatus.CONFIRMED, version=2),
                     Action.APPROVE, EXPIRY - timedelta(seconds=1))
    repo.commit_transition(7, commit)
    _wait_until(lambda: before.appointment_id not in repo.due_hold_ids(now, 100),
                "Approved hold stayed in HoldDueIndex")


class _RunScopedStore:
    """Limits dispatch to this run's business so a shared table is never touched elsewhere."""

    def __init__(self, inner: DynamoOutboxStore, business: str) -> None:
        self._inner = inner
        self._business = business

    def due(self, now: datetime, limit: int) -> Iterator[tuple[str, str]]:
        return ((b, o) for b, o in self._inner.due(now, limit) if b == self._business)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_committed_outbox_dispatches_and_fake_consumer_claims_once(race_env: RaceEnv) -> None:
    env = race_env
    before = _appointment(env, "request", CalendarStatus.PENDING_APPROVAL)
    _seed(env, (before,))
    decision_at = EXPIRY - timedelta(seconds=1)
    commit = _commit(
        env, before, replace(before, status=CalendarStatus.CONFIRMED, version=2),
        Action.APPROVE, decision_at,
    )
    DynamoDBCalendarRepository(env.client, env.table).commit_transition(7, commit)

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

    store = DynamoOutboxStore(env.client, env.table)
    scoped = _RunScopedStore(store, env.business)
    _wait_until(lambda: bool(list(scoped.due(decision_at, 100))),
                "Committed outbox item did not appear in the due index")
    queue = FakeQueue()
    report = DispatchService(scoped, queue, lambda: decision_at).run_once()  # type: ignore[arg-type]
    outbox_id = commit.outbox[0].outbox_id
    assert report.enqueued == 1
    assert queue.messages == [(env.business, outbox_id)]
    sender = FakeSender()
    consumer = ConsumeService(store, sender, lambda: decision_at)
    assert consumer.consume(*queue.messages[0]) == ConsumeOutcome.SENT
    assert consumer.consume(*queue.messages[0]) == ConsumeOutcome.SKIPPED
    assert sender.deliveries == [outbox_id]
    item = _get(env, f"BUSINESS#{env.business}", f"OUTBOX#{outbox_id}")
    assert item is not None
    assert item["delivery_state"]["S"] == "SENT"
    assert item["provider_id"]["S"] == "synthetic-provider-id"
    assert "outbox_due_pk" not in item
