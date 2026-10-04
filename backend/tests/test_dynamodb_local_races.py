"""Opt-in transaction proof against DynamoDB Local or the deployed synthetic dev table."""

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError

from scheduling.adapters.conversation_state_dynamodb import DynamoConversationStates
from scheduling.adapters.dynamodb import DynamoDBCalendarRepository, _command_sort_key
from scheduling.adapters.outbox_aws import DynamoOutboxStore, due_keys
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.appointments import Appointment
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientRecordService, HomeSize, RecordConflict
from scheduling.domain.conversation_state import ConversationState, PromptKind
from scheduling.domain.holds import OutboxIntent, RevisionConflict
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    TransitionCommit,
    TransitionResult,
)
from scheduling.domain.outbox import (
    ConsumeOutcome,
    ConsumeService,
    DeliveryState,
    DispatchService,
    OutboxRecord,
)
from scheduling.domain.sms_ingress import ConsentEvidence, record_in_person_consent
from scheduling.domain.sms_status import SmsDeliveryStatus

DEV_TABLE = "scheduling-dev"
DEV_REGION = "us-west-1"
DEV_ENDPOINT = "https://dynamodb.us-west-1.amazonaws.com"
# Far in the past so this run's keys sort first in the table-wide due indexes and
# other synthetic records on a shared table cannot crowd them out of bounded queries.
START = datetime(2001, 10, 1, 16, tzinfo=UTC)
EXPIRY = datetime(2001, 9, 30, 16, tzinfo=UTC)


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
        partitions = [f"BUSINESS#{self.business}",
                      DynamoDBCalendarRepository._visit_partition(
                          self.business, "synthetic-client")]
        partitions += [f"APPOINTMENT#{identifier}" for identifier in self.appointment_ids]
        errors: list[str] = []
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
                try:
                    page = self.client.query(**arguments)
                except (BotoCoreError, ClientError) as exc:
                    errors.append(f"query {pk}: {exc}")
                    break
                for item in page["Items"]:
                    try:
                        self.client.delete_item(
                            TableName=self.table, Key={"PK": item["PK"], "SK": item["SK"]},
                        )
                    except (BotoCoreError, ClientError) as exc:
                        errors.append(f"delete {pk} {item['SK']['S']}: {exc}")
                last_key = page.get("LastEvaluatedKey")
                if not last_key:
                    break
        if errors:
            raise RuntimeError(
                f"Cleanup for run {self.run} left items behind; sweep them by prefix: "
                + "; ".join(errors)
            )


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
    # Also catches AWS_ENDPOINT_URL_DYNAMODB and a profile-level endpoint_url.
    if client.meta.endpoint_url != DEV_ENDPOINT:
        raise ValueError(f"Unexpected DynamoDB endpoint {client.meta.endpoint_url!r}")
    run = f"run-{uuid4().hex[:12]}"
    print(f"\nSynthetic dev race run id: {run} "
          f"(keys BUSINESS#synthetic-{run}, APPOINTMENT#{run}-*)", flush=True)
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
        if appointment.status == CalendarStatus.CONFIRMED:
            client.put_item(TableName=table, Item=repo._visit_item(appointment))
    if guard_original:
        client.put_item(
            TableName=table,
            Item={**repo._business_key(env.business, f"REPLACEMENT#{guard_original}"),
                  "replacement_id": {"S": next(
                      (a.appointment_id for a in appointments
                       if a.replaces_appointment_id == guard_original), "replacement")},
                  "expires_at": {"S": EXPIRY.isoformat(timespec="microseconds")}},
        )


def test_client_erasure_releases_reservations_and_removes_linked_records(
    race_env: RaceEnv,
) -> None:
    env = race_env
    repo = DynamoDBCalendarRepository(env.client, env.table)
    ClientRecordService(repo).save_profile(
        env.business, "synthetic-client", "Synthetic Client", "+14155550101",
        "123 Test Street", HomeSize.SMALL, 60, True, 0, 180, START,
    )
    confirmed = _appointment(env, "erasure-confirmed", CalendarStatus.CONFIRMED)
    pending = _appointment(env, "erasure-pending", CalendarStatus.PENDING_APPROVAL)
    _seed(env, (confirmed, pending))
    env.client.put_item(TableName=env.table, Item={
        **repo._business_key(env.business, "NOTE#CLIENT#" +
                             sha256(b"synthetic-client").hexdigest() +
                             "#synthetic-note"),
        "client_id": {"S": "synthetic-client"}, "body": {"S": "Synthetic note"},
    })
    env.client.put_item(TableName=env.table, Item={
        **repo._business_key(env.business, "SMS#synthetic-receipt"),
        "client_id": {"S": "synthetic-client"}, "sender": {"S": "+14155550101"},
        "body": {"S": "Synthetic request"},
    })
    env.client.put_item(TableName=env.table, Item={
        **repo._business_key(env.business, "OUTBOX#synthetic-notice"),
        "appointment_id": {"S": confirmed.appointment_id},
    })
    env.client.put_item(TableName=env.table, Item={
        **repo._business_key(env.business, "OUTBOX#welcome#synthetic-client"),
        "entity_id": {"S": "synthetic-client"},
        "recipient": {"S": "client"},
        "template": {"S": "welcome"},
    })
    command_key = _command_sort_key("synthetic-client", "cancel", "synthetic-receipt")
    env.client.put_item(TableName=env.table, Item={
        **repo._business_key(env.business, command_key),
        "request_hash": {"S": "synthetic-hash"},
    })

    from scheduling.adapters.counteroffer_dynamodb import DynamoCounterofferStore
    from scheduling.domain.owner_counteroffer import (
        Counteroffer,
        OfferState,
        counteroffer_failure_outbox,
        counteroffer_outbox,
    )

    offers = DynamoCounterofferStore(env.client, env.table)
    offer = Counteroffer(
        env.business, "co-erase", "+15005550009", pending.appointment_id, 1,
        "synthetic-client", "+14155550101", START, 60, "Synthetic offer text.",
        OfferState.PROPOSED, 1, START, START + timedelta(minutes=30))
    assert offers.put_draft(offer)
    confirmed_offer = offers.confirm(offer, "SM-erase", START, counteroffer_outbox(
        replace(offer, version=2), START))
    assert confirmed_offer is not None
    offers.record_failure(confirmed_offer, "SLOT_UNAVAILABLE", START,
                          counteroffer_failure_outbox(confirmed_offer, START))
    assert offers.read(env.business, "co-erase") is not None

    lease = repo.acquire_client_send(env.business, "synthetic-client")
    with pytest.raises(RecordConflict):
        repo.erase_client(env.business, "synthetic-client")
    repo.release_client_send(env.business, "synthetic-client", lease)

    repo.erase_client(env.business, "synthetic-client")
    repo.erase_client(env.business, "synthetic-client")  # safe replay
    assert repo.read_profile(env.business, "synthetic-client") is None
    assert repo.read_appointment(confirmed.appointment_id) is None
    assert repo.read_appointment(pending.appointment_id) is None
    assert repo.read_calendar(env.business).events == ()
    remaining = repo._scan_items()
    assert not any(item["SK"]["S"].startswith(("NOTE#", "SMS#", "OUTBOX#"))
                   for item in remaining if item["PK"]["S"] == f"BUSINESS#{env.business}")
    assert not any(item["SK"]["S"].startswith("COUNTEROFFER")
                   for item in remaining if item["PK"]["S"] == f"BUSINESS#{env.business}")
    assert offers.read(env.business, "co-erase") is None
    assert offers.read_confirmed_for_client(env.business, "synthetic-client") is None
    assert not offers.put_draft(offer)  # The erased client can no longer receive offers.
    assert not any(item["PK"]["S"].startswith(f"VISITS#{env.business}#")
                   for item in remaining)
    assert not any(item["SK"]["S"] == command_key for item in remaining)
    DynamoSmsIngressStore(env.client, env.table).put_status(SmsDeliveryStatus(
        env.business, "synthetic-notice", "SM-synthetic-late", "undelivered",
        "+14155550101", START, "30007"))
    assert repo._get(repo._business_key(env.business, "SMS_STATUS#SM-synthetic-late")) is None
    state = ConversationState(env.business, "+14155550101", "synthetic-receipt",
                              PromptKind.OFFER, START, START + timedelta(minutes=30),
                              (START,), client_id="synthetic-client")
    with pytest.raises(ClientError):
        DynamoConversationStates(env.client, env.table).put_state(state)
    assert repo._get(repo._business_key(env.business, "SMS_STATE#+14155550101")) is None


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


def _run_race(env: RaceEnv, repository: DynamoDBCalendarRepository,
              first: TransitionCommit, second: TransitionCommit) -> bool:
    """Return True when exactly one transaction committed."""
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
    # One loser is the usual outcome. On a real table both transactions can be
    # cancelled by an overlapping-transaction conflict; then nothing may commit.
    assert failures in {1, 2}
    if failures == 2:
        items = _business_items(env)
        assert not [item for item in items
                    if item["SK"]["S"].startswith(("AUDIT#", "OUTBOX#"))]
        revision = _get(env, f"BUSINESS#{env.business}", "CALENDAR#REVISION")
        assert revision is not None and revision["revision"]["N"] == "7"
    return failures == 1


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
    repo = DynamoDBCalendarRepository(env.client, env.table)
    if not _run_race(env, repo, approvals[0], approvals[1]):
        return  # both cancelled: the helper already proved nothing committed

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
    if not _run_race(env, repo, approve, expire):
        return  # both cancelled: the helper already proved nothing committed

    metadata = _get(env, f"APPOINTMENT#{before.appointment_id}", "META")
    assert metadata is not None
    assert metadata["status"]["S"] in {"CONFIRMED", "EXPIRED"}
    items = _business_items(env)
    assert len([item for item in items if item["SK"]["S"].startswith("AUDIT#")]) == 1
    assert len([item for item in items if item["SK"]["S"].startswith("OUTBOX#")]) == 1
    event = next((item for item in items if item["SK"]["S"].startswith("EVENT#")), None)
    assert (event is not None) == (metadata["status"]["S"] == "CONFIRMED")
    assert repo.last_visit_end(env.business, before.client_id, START + timedelta(hours=2)) == (
        before.end_at if metadata["status"]["S"] == "CONFIRMED" else None)
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
    if not _run_race(env, repo, approval, cancellation):
        return  # both cancelled: the helper already proved nothing committed

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
    assert repo.last_visit_end(env.business, original.client_id, START + timedelta(hours=2)) == (
        replacement.end_at if replacement_item["status"]["S"] == "CONFIRMED" else None)
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


def _consent_world(env: RaceEnv) -> tuple[DynamoDBCalendarRepository, DynamoSmsIngressStore,
                                           ClientRecordService]:
    repo = DynamoDBCalendarRepository(env.client, env.table)
    clients = ClientRecordService(repo)
    clients.save_profile(env.business, "consent-client", "Synthetic Client", "+14155550101",
                         "123 Test Street", HomeSize.SMALL, 60, True, 0, 180, START)
    return repo, DynamoSmsIngressStore(env.client, env.table), clients


def _consent_items(env: RaceEnv) -> list[str]:
    return [item["SK"]["S"] for item in _business_items(env)
            if item["SK"]["S"].startswith(("SMS_CONSENT#", "SMS_CONSENT_CURRENT#"))]


def _welcome_items(env: RaceEnv) -> list[str]:
    return [item["SK"]["S"] for item in _business_items(env)
            if item["SK"]["S"].startswith("OUTBOX#welcome#")]


def _consent(env: RaceEnv, repo: DynamoDBCalendarRepository,
             store: DynamoSmsIngressStore, agreed_at: datetime = START) -> None:
    record_in_person_consent(store, repo, env.business, "consent-client", "+14155550101",
                             "Synthetic Client", "1", agreed_at)


def test_in_person_consent_verifies_phone_in_one_transaction(race_env: RaceEnv) -> None:
    env = race_env
    repo, store, _ = _consent_world(env)
    assert repo.read_verified_phone(env.business, "+14155550101") is None
    _consent(env, repo, store)
    profile = repo.read_verified_phone(env.business, "+14155550101")
    assert profile is not None and profile.version == 2
    assert len(_consent_items(env)) == 2


def test_profile_change_between_read_and_consent_writes_nothing(race_env: RaceEnv) -> None:
    env = race_env
    repo, store, clients = _consent_world(env)
    original = repo.read_profile

    def stale_read(business_id: str, client_id: str) -> Any:
        repo.read_profile = original  # type: ignore[method-assign]  # the save below reads too
        profile = original(business_id, client_id)
        clients.save_profile(env.business, "consent-client", "Synthetic Client Two",
                             "+14155550101", "123 Test Street", HomeSize.SMALL, 60, True, 1,
                             180, START)
        return profile

    repo.read_profile = stale_read  # type: ignore[method-assign]
    with pytest.raises(RecordConflict):
        _consent(env, repo, store)
    assert _consent_items(env) == []
    assert original(env.business, "consent-client").phone_verified_at is None  # type: ignore[union-attr]


def test_two_concurrent_consents_commit_exactly_one(race_env: RaceEnv) -> None:
    env = race_env
    repo, store, _ = _consent_world(env)
    barrier = Barrier(2)

    def attempt() -> bool:
        barrier.wait(timeout=5)
        try:
            _consent(env, repo, store)
        except RecordConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(attempt), pool.submit(attempt)]]
    # Both read version 1; only one conditional profile write can win.
    assert results.count(True) == 1
    profile = repo.read_verified_phone(env.business, "+14155550101")
    assert profile is not None and profile.version == 2
    assert len(_consent_items(env)) == 2
    # Exactly one welcome text was queued, and the outbox store can read and dispatch it.
    assert _welcome_items(env) == ["OUTBOX#welcome#consent-client"]
    outbox = DynamoOutboxStore(env.client, env.table)
    record = outbox.get(env.business, "welcome#consent-client")
    assert record is not None and record.template == "welcome" and record.recipient == "client"
    assert record.state == DeliveryState.PENDING and record.created_at == START
    assert (env.business, "welcome#consent-client") in list(outbox.due(START, 10))


def test_repeat_consent_and_pre_welcome_client_queue_no_second_welcome(
        race_env: RaceEnv) -> None:
    env = race_env
    repo, store, clients = _consent_world(env)
    _consent(env, repo, store)
    # A later repeat consent, as after STOP/START re-subscription.
    _consent(env, repo, store, START + timedelta(days=1))
    assert _welcome_items(env) == ["OUTBOX#welcome#consent-client"]
    # A client enrolled before welcome texts existed: consent records, no outbox record.
    clients.save_profile(env.business, "old-client", "Synthetic Old", "+14155550103",
                         "123 Test Street", HomeSize.SMALL, 60, True, 0, 180, START)
    old = ConsentEvidence(env.business, "old-client", "Synthetic Old", "+14155550103", START, "1")
    profile = repo.read_profile(env.business, "old-client")
    assert profile is not None
    store.put_consent_verifying_phone(old, replace(
        profile, version=2, updated_at=START, phone_verified_at=START))
    clients.save_profile(env.business, "old-client", "Synthetic Old", "+14155550104",
                         "123 Test Street", HomeSize.SMALL, 60, True, 2, 180, START)
    # A new client then consents on the old client's former (shared) number, which
    # overwrites that number's current-consent record but not the old client's history.
    clients.save_profile(env.business, "shared-client", "Synthetic Shared", "+14155550103",
                         "123 Test Street", HomeSize.SMALL, 60, True, 0, 180, START)
    record_in_person_consent(store, repo, env.business, "shared-client", "+14155550103",
                             "Synthetic Shared", "1", START + timedelta(days=3))
    record_in_person_consent(store, repo, env.business, "old-client", "+14155550104",
                             "Synthetic Old", "1", START + timedelta(days=5))
    assert sorted(_welcome_items(env)) == ["OUTBOX#welcome#consent-client",
                                           "OUTBOX#welcome#shared-client"]


def test_retire_pending_outbox_before_cutoff_leaves_the_due_index(race_env: RaceEnv) -> None:
    env = race_env
    store = DynamoOutboxStore(env.client, env.table)
    cutoff = EXPIRY + timedelta(days=1)

    def put(outbox_id: str, state: DeliveryState, created: datetime,
            lease: str | None = None) -> None:
        stamp = created.isoformat(timespec="microseconds")
        item: dict[str, Any] = {
            "PK": {"S": f"BUSINESS#{env.business}"}, "SK": {"S": f"OUTBOX#{outbox_id}"},
            "outbox_id": {"S": outbox_id}, "entity_id": {"S": "synthetic-entity"},
            "recipient": {"S": "owner"}, "template": {"S": "block_time"},
            "event_version": {"N": "1"}, "delivery_state": {"S": state.value},
            "created_at": {"S": stamp}, "next_attempt_at": {"S": stamp},
            "dispatch_after": {"S": stamp}, **due_keys(state, created, outbox_id),
        }
        if lease:
            item["lease_token"] = {"S": lease}
        env.client.put_item(TableName=env.table, Item=item)

    old = f"{env.run}-old"
    new = f"{env.run}-new"
    leased = f"{env.run}-leased"
    put(old, DeliveryState.PENDING, EXPIRY)
    put(new, DeliveryState.PENDING, cutoff + timedelta(hours=1))
    put(leased, DeliveryState.SENDING, EXPIRY, lease="synthetic-token")
    _wait_until(lambda: len(store.pending_before(env.business, cutoff)) == 2,
                "Seeded outbox records did not appear in the due index")

    dry = store.retire_pending_before(
        env.business, cutoff, "RETIRED_BEFORE_LIVE_SMS", cutoff, execute=False)
    assert {o.outbox_id: o.result.value for o in dry} == {
        old: "WOULD_RETIRE", leased: "SKIPPED_LEASED"}
    before = _get(env, f"BUSINESS#{env.business}", f"OUTBOX#{old}")
    assert before is not None and before["delivery_state"]["S"] == "PENDING"

    done = store.retire_pending_before(
        env.business, cutoff, "RETIRED_BEFORE_LIVE_SMS", cutoff, execute=True)
    assert {o.outbox_id: o.result.value for o in done} == {
        old: "RETIRED", leased: "SKIPPED_LEASED"}
    item = _get(env, f"BUSINESS#{env.business}", f"OUTBOX#{old}")
    assert item is not None
    assert item["delivery_state"]["S"] == "FAILED"
    assert item["last_error_code"]["S"] == "RETIRED_BEFORE_LIVE_SMS"
    assert "retired_at" in item
    for gone in ("outbox_due_pk", "outbox_due_sk", "dispatch_after", "next_attempt_at"):
        assert gone not in item
    _wait_until(
        lambda: [r.outbox_id for r in store.pending_before(env.business, cutoff)] == [leased],
        "Retired record stayed in the due index")
    kept = _get(env, f"BUSINESS#{env.business}", f"OUTBOX#{new}")
    assert kept is not None and kept["delivery_state"]["S"] == "PENDING"


def test_two_concurrent_counteroffer_confirmations_queue_exactly_one_client_text(
        race_env: RaceEnv) -> None:
    from scheduling.adapters.counteroffer_dynamodb import DynamoCounterofferStore
    from scheduling.domain.owner_counteroffer import (
        Counteroffer,
        OfferState,
        counteroffer_outbox,
    )

    env = race_env
    store = DynamoCounterofferStore(env.client, env.table)
    offer = Counteroffer(
        env.business, "co-race", "+15005550009", env.appointment_id("request"), 1,
        "synthetic-client", "+15005550006", START, 60, "Synthetic offer text.",
        OfferState.PROPOSED, 1, START, START + timedelta(minutes=30))
    store.put_draft(offer)
    store.put_draft(offer)  # A redelivered owner message rewrites the same proposal.
    barrier = Barrier(2)

    def attempt(message: str) -> bool:
        barrier.wait(timeout=5)
        confirmed = store.confirm(
            offer, message, START,
            counteroffer_outbox(replace(offer, version=2), START))
        return confirmed is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [
            pool.submit(attempt, "SM-1"), pool.submit(attempt, "SM-2")]]
    assert results.count(True) == 1
    stored = store.read(env.business, "co-race")
    assert stored is not None and stored.state == OfferState.CONFIRMED and stored.version == 2
    assert store.read_active(env.business, "+15005550009") == stored
    assert store.read_confirmed_for_client(env.business, "synthetic-client") == stored
    outbox = DynamoOutboxStore(env.client, env.table)
    record = outbox.get(env.business, "counteroffer#co-race")
    assert record is not None and record.template == "owner-counteroffer"
    assert record.recipient == "client" and record.event_version == 2
    assert not store.put_draft(offer)  # A finished offer cannot be reopened.


def _countered_pair(env: RaceEnv) -> tuple[Appointment, Appointment]:
    """A still-pending original and the pending request an accepted counteroffer created."""
    original = _appointment(env, "countered", CalendarStatus.PENDING_APPROVAL)
    replacement = _appointment(env, "accepted", CalendarStatus.PENDING_APPROVAL,
                               replacement_of=original.appointment_id)
    _seed(env, (original, replacement), guard_original=original.appointment_id)
    return original, replacement


def test_approving_an_accepted_counteroffer_resolves_both_requests_in_one_transaction(
    race_env: RaceEnv,
) -> None:
    env = race_env
    original, replacement = _countered_pair(env)
    repo = DynamoDBCalendarRepository(env.client, env.table)
    approval = _commit(
        env, replacement, replace(replacement, status=CalendarStatus.CONFIRMED, version=2),
        Action.APPROVE, EXPIRY - timedelta(seconds=1),
        replaced=replace(original, status=CalendarStatus.DECLINED, version=2), clear_guard=True)
    repo.commit_transition(7, approval)

    original_item = _get(env, f"APPOINTMENT#{original.appointment_id}", "META")
    replacement_item = _get(env, f"APPOINTMENT#{replacement.appointment_id}", "META")
    assert original_item is not None and original_item["status"]["S"] == "DECLINED"
    assert replacement_item is not None and replacement_item["status"]["S"] == "CONFIRMED"
    items = _business_items(env)
    assert [item["event_id"]["S"] for item in items if item["SK"]["S"].startswith("EVENT#")] == [
        replacement.appointment_id]
    assert len([item for item in items if item["SK"]["S"].startswith("AUDIT#")]) == 1
    assert _get(env, f"BUSINESS#{env.business}", f"REPLACEMENT#{original.appointment_id}") is None
    revision = _get(env, f"BUSINESS#{env.business}", "CALENDAR#REVISION")
    assert revision is not None and revision["revision"]["N"] == "8"


def test_accepted_counteroffer_approval_races_a_decision_on_the_original_atomically(
    race_env: RaceEnv,
) -> None:
    env = race_env
    original, replacement = _countered_pair(env)
    repo = DynamoDBCalendarRepository(env.client, env.table)
    declined_original = replace(original, status=CalendarStatus.DECLINED, version=2)
    approval = _commit(
        env, replacement, replace(replacement, status=CalendarStatus.CONFIRMED, version=2),
        Action.APPROVE, EXPIRY - timedelta(seconds=1),
        replaced=declined_original, clear_guard=True)
    decline = _commit(env, original, declined_original, Action.DECLINE,
                      EXPIRY - timedelta(seconds=1))
    if not _run_race(env, repo, approval, decline):
        return  # both cancelled: the helper already proved nothing committed

    original_item = _get(env, f"APPOINTMENT#{original.appointment_id}", "META")
    replacement_item = _get(env, f"APPOINTMENT#{replacement.appointment_id}", "META")
    assert original_item is not None and original_item["status"]["S"] == "DECLINED"
    assert replacement_item is not None
    assert replacement_item["status"]["S"] in {"CONFIRMED", "PENDING_APPROVAL"}
    items = _business_items(env)
    assert len([item for item in items if item["SK"]["S"].startswith("AUDIT#")]) == 1
    events = [item["event_id"]["S"] for item in items if item["SK"]["S"].startswith("EVENT#")]
    assert events == [replacement.appointment_id]
    guard = _get(env, f"BUSINESS#{env.business}", f"REPLACEMENT#{original.appointment_id}")
    assert (guard is None) == (replacement_item["status"]["S"] == "CONFIRMED")


def test_accepted_counteroffer_approval_after_the_original_changed_writes_nothing(
    race_env: RaceEnv,
) -> None:
    env = race_env
    original, replacement = _countered_pair(env)
    repo = DynamoDBCalendarRepository(env.client, env.table)
    declined_original = replace(original, status=CalendarStatus.DECLINED, version=2)
    repo.commit_transition(7, _commit(env, original, declined_original, Action.DECLINE,
                                      EXPIRY - timedelta(seconds=1)))
    stale_approval = _commit(
        env, replacement, replace(replacement, status=CalendarStatus.CONFIRMED, version=2),
        Action.APPROVE, EXPIRY - timedelta(seconds=1),
        replaced=replace(declined_original, version=3), clear_guard=True)
    with pytest.raises(RevisionConflict):
        repo.commit_transition(8, stale_approval)
    replacement_item = _get(env, f"APPOINTMENT#{replacement.appointment_id}", "META")
    assert replacement_item is not None and replacement_item["status"]["S"] == "PENDING_APPROVAL"
    assert len([item for item in _business_items(env)
                if item["SK"]["S"].startswith("AUDIT#")]) == 1


def test_owner_thread_text_is_purged_per_message_while_client_thread_keeps_old_rule(
        race_env: RaceEnv) -> None:
    from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

    env = race_env
    store = DynamoSmsIngressStore(env.client, env.table)
    owner, client_phone = "+15005550010", "+15005550011"
    now = datetime.now(UTC)

    def receive(sid: str, sender: str, role: SenderRole, age_days: int) -> None:
        assert store.put_received(InboundReceipt(
            env.business, f"{env.run}-{sid}", sender, "+15005550000", f"Synthetic {sid}",
            now - timedelta(days=age_days), role,
            "synthetic-client" if role == SenderRole.CLIENT else None, Keyword.OTHER, True))

    receive("owner-old", owner, SenderRole.OWNER, 100)
    receive("owner-new", owner, SenderRole.OWNER, 1)  # Recent owner activity.
    receive("client-old", client_phone, SenderRole.CLIENT, 100)
    receive("client-new", client_phone, SenderRole.CLIENT, 1)

    assert store.purge_expired_bodies(env.business, now, owner) == 1
    old = _get(env, f"BUSINESS#{env.business}", f"SMS#{env.run}-owner-old")
    assert old is not None and "body" not in old and "provider_id" in old
    new = _get(env, f"BUSINESS#{env.business}", f"SMS#{env.run}-owner-new")
    assert new is not None and new["body"]["S"] == "Synthetic owner-new"
    for name in ("client-old", "client-new"):
        kept = _get(env, f"BUSINESS#{env.business}", f"SMS#{env.run}-{name}")
        assert kept is not None and "body" in kept
    _owner_edge_cases(env, store, owner, now)
    assert store.put_received(InboundReceipt(
        env.business, f"{env.run}-owner-old", owner, "+15005550000", "Synthetic redelivery",
        now - timedelta(days=100), SenderRole.OWNER, None, Keyword.OTHER, True)) is False


class _ReplyBeforePurge:
    """Client proxy that lets a reply land after the purge read but before its write."""

    def __init__(self, client: Any, on_first_write: Callable[[], None]) -> None:
        self._client, self._hook = client, on_first_write

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def transact_write_items(self, **kwargs: Any) -> Any:
        hook, self._hook = self._hook, lambda: None
        hook()
        return self._client.transact_write_items(**kwargs)


def _owner_edge_cases(env: RaceEnv, store: DynamoSmsIngressStore, owner: str,
                      now: datetime) -> None:
    from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole

    def receive(sid: str, age_days: int) -> InboundReceipt:
        receipt = InboundReceipt(
            env.business, f"{env.run}-{sid}", owner, "+15005550000", f"Synthetic {sid}",
            now - timedelta(days=age_days), SenderRole.OWNER, None, Keyword.OTHER, True)
        assert store.put_received(receipt)
        return receipt

    def answer(receipt: InboundReceipt, when: datetime) -> None:
        assert store.claim_processing(receipt, "token", when, when + timedelta(minutes=2))
        assert store.put_reply(receipt, "Synthetic reply", "token", when)

    def read(sid: str) -> dict[str, Any]:
        item = _get(env, f"BUSINESS#{env.business}", f"SMS#{env.run}-{sid}")
        assert item is not None
        return item

    # Received 100 days ago, answered 89 days ago: the reply keeps the exchange alive.
    answered = receive("owner-answered", 100)
    answer(answered, now - timedelta(days=89))
    # Under legal hold: kept even though it is old.
    receive("owner-held", 100)
    env.client.update_item(
        TableName=env.table,
        Key={"PK": {"S": f"BUSINESS#{env.business}"}, "SK": {"S": f"SMS#{env.run}-owner-held"}},
        UpdateExpression="SET legal_hold_reason = :r",
        ExpressionAttributeValues={":r": {"S": "synthetic hold"}})
    # Old and unanswered when read; a reply lands before the purge writes.
    racing = receive("owner-racing", 100)
    racer = DynamoSmsIngressStore(_ReplyBeforePurge(
        env.client, lambda: answer(racing, now)), env.table)

    assert racer.purge_expired_bodies(env.business, now, owner) == 0  # The earlier purge already took owner-old; the race cancels the rest.
    assert read("owner-answered")["body"]["S"] == "Synthetic owner-answered"
    assert read("owner-answered")["reply_text"]["S"] == "Synthetic reply"
    assert read("owner-held")["body"]["S"] == "Synthetic owner-held"
    assert read("owner-racing")["reply_text"]["S"] == "Synthetic reply"
    assert "body" in read("owner-racing")
