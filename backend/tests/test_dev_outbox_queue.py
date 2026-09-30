"""Opt-in proof of the outbox SQS path with a fake sender, before outbox dispatch is enabled.

Dev mode (``SCHEDULING_DEV_TABLE=scheduling-dev`` plus ``SCHEDULING_DEV_OUTBOX_QUEUE=
scheduling-outbox-dev``) runs the real ``DispatchService`` and ``SQSIntentQueue`` against the
deployed outbox queue, consumes with the real ``ConsumeService`` and a fake sender, and drives
one poison message to the dead-letter queue. Nothing here invokes a Lambda, enables an event
source mapping, or builds a Twilio client. It reuses the guards and run-scoped cleanup of the
#80 harness in ``test_dynamodb_local_races``.

Without the opt-in, the same scenario always runs in memory against a fake SQS client, and runs
against DynamoDB Local when ``DYNAMODB_LOCAL_URL`` is set.
"""

import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from test_dynamodb_local_races import (
    DEV_REGION,
    EXPIRY,
    RaceEnv,
    _appointment,
    _commit,
    _dev_env,
    _local_env,
    _RunScopedStore,
    _seed,
    _wait_until,
)
from test_outbox import MemoryStore

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.outbox_aws import DynamoOutboxStore, SQSIntentQueue
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.lifecycle import Action
from scheduling.domain.outbox import (
    ConsumeOutcome,
    ConsumeService,
    DeliveryFailure,
    DeliveryState,
    DispatchService,
    OutboxRecord,
    consume_sqs_batch,
    decode_queue_message,
)

OUTBOX_QUEUE = "scheduling-outbox-dev"
DLQ_QUEUE = "scheduling-outbox-dlq-dev"
SQS_ENDPOINT = "https://sqs.us-west-1.amazonaws.com"
VISIBILITY = 3  # per-receive override, so no test waits out the queue's 180 second default
MAX_RECEIVE_LIMIT = 10
STACK = "scheduling-dev"
SENDER_MAPPING_LOGICAL_ID = "SmsSenderFunctionOutbox"
DISPATCH_RULE_LOGICAL_ID = "OutboxDispatchFunctionSweep"  # SAM: <function><event name>


def _queue_url_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"^https://sqs\.us-west-1\.amazonaws\.com/\d{{12}}/{re.escape(name)}$")


def require_queue_url(name: str, url: str) -> str:
    if not _queue_url_pattern(name).fullmatch(url):
        raise ValueError(f"{url!r} is not the {name!r} queue in us-west-1")
    return url


def dev_queue_name() -> str:
    name = os.environ.get("SCHEDULING_DEV_OUTBOX_QUEUE", "")
    if name != OUTBOX_QUEUE:
        raise ValueError(f"SCHEDULING_DEV_OUTBOX_QUEUE must be exactly {OUTBOX_QUEUE!r}")
    return name


def _physical_id(cloudformation: Any, logical_id: str) -> str:
    detail = cloudformation.describe_stack_resource(
        StackName=STACK, LogicalResourceId=logical_id)["StackResourceDetail"]
    return str(detail["PhysicalResourceId"])


def require_consumers_off(cloudformation: Any, lambda_client: Any, events: Any) -> None:
    """Fail closed unless the real sender's queue mapping and the dispatcher rule are disabled.

    Otherwise the real SmsSenderFunction could consume this run's messages (and build a Twilio
    client), or the table-wide dispatcher could put other intents on the queue mid-run.
    Any lookup error also stops the run.
    """
    try:
        mapping_id = _physical_id(cloudformation, SENDER_MAPPING_LOGICAL_ID)
        mapping = lambda_client.get_event_source_mapping(UUID=mapping_id)
        rule_name = _physical_id(cloudformation, DISPATCH_RULE_LOGICAL_ID)
        rule = events.describe_rule(Name=rule_name.rsplit("/", 1)[-1])
    except Exception as exc:  # any failure to prove the state must stop the run
        raise ValueError(f"Could not confirm the sender mapping and dispatcher are off: {exc}") \
            from exc
    if not str(mapping.get("EventSourceArn", "")).endswith(f":{OUTBOX_QUEUE}"):
        raise ValueError("The sender mapping does not read the outbox queue; refusing to run")
    if mapping.get("State") != "Disabled":
        raise ValueError(
            f"SmsSenderFunctionOutbox mapping state is {mapping.get('State')!r}, not 'Disabled'"
        )
    if rule.get("State") != "DISABLED":
        raise ValueError(f"Outbox dispatch rule state is {rule.get('State')!r}, not 'DISABLED'")


class FakeSqs:
    """In-memory SQS with visibility timeouts, receive counts and redrive to a DLQ.

    Models only what the harness uses. ``advance`` replaces sleeping.
    """

    def __init__(self, max_receive: int = 5, per_call: int = 1) -> None:
        self.per_call = per_call  # like long polling, one message per poll exercises multi-poll
        self.now = 0.0
        self.max_receive = max_receive
        self._queues: dict[str, list[dict[str, Any]]] = {OUTBOX_QUEUE: [], DLQ_QUEUE: []}
        self.calls: list[str] = []

    def advance(self, seconds: float) -> None:
        self.now += seconds

    @staticmethod
    def _name(url: str) -> str:
        return url.rsplit("/", 1)[-1]

    def get_queue_url(self, QueueName: str) -> dict[str, str]:
        self.calls.append("get_queue_url")
        if QueueName not in self._queues:
            raise KeyError(QueueName)
        return {"QueueUrl": f"{SQS_ENDPOINT}/123456789012/{QueueName}"}

    def get_queue_attributes(self, QueueUrl: str, **_: Any) -> dict[str, Any]:
        self.calls.append("get_queue_attributes")
        name = self._name(QueueUrl)
        messages = self._queues[name]
        visible = sum(1 for m in messages if m["visible_at"] <= self.now)
        attributes = {
            "ApproximateNumberOfMessages": str(visible),
            "ApproximateNumberOfMessagesNotVisible": str(len(messages) - visible),
            "ApproximateNumberOfMessagesDelayed": "0",
        }
        if name == OUTBOX_QUEUE:
            attributes["RedrivePolicy"] = json.dumps({
                "deadLetterTargetArn": f"arn:aws:sqs:us-west-1:123456789012:{DLQ_QUEUE}",
                "maxReceiveCount": self.max_receive,
            })
        return {"Attributes": attributes}

    def send_message(self, QueueUrl: str, MessageBody: str, **_: Any) -> dict[str, str]:
        self.calls.append("send_message")
        message_id = uuid4().hex
        self._queues[self._name(QueueUrl)].append({
            "id": message_id, "body": MessageBody, "count": 0, "visible_at": self.now,
            "handle": "",
        })
        return {"MessageId": message_id}

    def receive_message(
        self, QueueUrl: str, MaxNumberOfMessages: int = 1, VisibilityTimeout: int = 180,
        **_: Any,
    ) -> dict[str, Any]:
        self.calls.append("receive_message")
        name = self._name(QueueUrl)
        received: list[dict[str, Any]] = []
        for message in list(self._queues[name]):
            if message["visible_at"] > self.now or len(received) >= min(MaxNumberOfMessages, self.per_call):
                continue
            if name == OUTBOX_QUEUE and message["count"] >= self.max_receive:
                self._queues[name].remove(message)  # redrive instead of another delivery
                self._queues[DLQ_QUEUE].append({**message, "visible_at": self.now, "handle": ""})
                continue
            message["count"] += 1
            message["visible_at"] = self.now + VisibilityTimeout
            message["handle"] = uuid4().hex
            received.append({
                "MessageId": message["id"], "Body": message["body"],
                "ReceiptHandle": message["handle"],
                "Attributes": {"ApproximateReceiveCount": str(message["count"])},
            })
        return {"Messages": received} if received else {}

    def delete_message(self, QueueUrl: str, ReceiptHandle: str) -> dict[str, Any]:
        self.calls.append("delete_message")
        queue = self._queues[self._name(QueueUrl)]
        queue[:] = [m for m in queue if m["handle"] != ReceiptHandle]
        return {}


@dataclass
class QueueEnv:
    sqs: Any
    outbox_url: str
    dlq_url: str
    max_receive: int
    business: str
    sleep: Callable[[float], None]
    monotonic: Callable[[], float]
    guard: Callable[[], None] = field(default=lambda: None)


class Clock:
    """The domain clock, independent of the queue's real time."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeSender:
    """Records deliveries and never contacts a provider."""

    def __init__(self) -> None:
        self.deliveries: list[str] = []
        self.fail_next = 0

    def deliver(self, record: OutboxRecord) -> str:
        if self.fail_next:
            self.fail_next -= 1
            raise DeliveryFailure("SYNTHETIC_UNAVAILABLE")
        self.deliveries.append(record.outbox_id)
        return f"synthetic-provider-{len(self.deliveries)}"


class RecordingConsumer(ConsumeService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.outcomes: list[ConsumeOutcome] = []

    def consume(self, business_id: str, outbox_id: str) -> ConsumeOutcome:
        outcome = super().consume(business_id, outbox_id)
        self.outcomes.append(outcome)
        return outcome


class RunQueue:
    """The real SQS adapter, refusing any intent outside this run's business."""

    def __init__(self, inner: SQSIntentQueue, business: str) -> None:
        self._inner = inner
        self._business = business

    def enqueue(self, business_id: str, outbox_id: str) -> None:
        if business_id != self._business:
            raise AssertionError(f"Refusing to enqueue for {business_id!r}")
        self._inner.enqueue(business_id, outbox_id)


def _pending_counts(q: QueueEnv, url: str) -> int:
    attributes = q.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    return sum(int(attributes[key]) for key in (
        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
        "ApproximateNumberOfMessagesDelayed",
    ))


def _receive(q: QueueEnv, url: str, visibility: int = VISIBILITY) -> list[dict[str, Any]]:
    response = q.sqs.receive_message(
        QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=visibility,
        WaitTimeSeconds=2, MessageSystemAttributeNames=["ApproximateReceiveCount"],
    )
    messages: list[dict[str, Any]] = response.get("Messages", [])
    for message in messages:
        # Fail closed: the queue was empty at the start, so anything else is not ours to touch.
        if json.loads(message["Body"]).get("business_id") != q.business:
            pytest.fail(f"Received a message that is not run {q.business}'s; stopping")
    return messages


def _receive_count(q: QueueEnv, url: str, wanted: int, seconds: float = 30) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    deadline = q.monotonic() + seconds
    while len(found) < wanted:
        for message in _receive(q, url):
            found[message["MessageId"]] = message
        if len(found) >= wanted:
            break
        if q.monotonic() >= deadline:
            pytest.fail(f"Expected {wanted} messages on {url}, got {len(found)}")
        q.sleep(0.5)
    return list(found.values())


def _consume(q: QueueEnv, consumer: ConsumeService, messages: list[dict[str, Any]],
             *, delete: bool) -> set[str]:
    """One SQS batch through the production batch handler; delete only what succeeded."""
    records = [{"messageId": m["MessageId"], "body": m["Body"]} for m in messages]
    failed = {f["itemIdentifier"] for f in consume_sqs_batch(records, consumer)["batchItemFailures"]}
    if delete:
        for message in messages:
            if message["MessageId"] not in failed:
                q.sqs.delete_message(QueueUrl=q.outbox_url, ReceiptHandle=message["ReceiptHandle"])
    return failed


def prove_outbox_queue_path(
    q: QueueEnv, store: Any, dispatch_store: Any, outbox_id: str, clock: Clock,
    await_due: Callable[[], None],
) -> None:
    business = q.business
    sender = FakeSender()
    consumer = RecordingConsumer(store, sender, clock)
    queue = RunQueue(SQSIntentQueue(q.sqs, q.outbox_url), business)
    expected_body = json.dumps({"business_id": business, "outbox_id": outbox_id},
                               sort_keys=True, separators=(",", ":"))

    # 1. Dispatch reaches the real queue; a transient provider failure schedules a retry.
    await_due()
    q.guard()
    report = DispatchService(dispatch_store, queue, clock).run_once()
    assert (report.examined, report.enqueued) == (1, 1)
    (first,) = _receive_count(q, q.outbox_url, 1)
    assert first["Body"] == expected_body
    assert first["Attributes"]["ApproximateReceiveCount"] == "1"
    sender.fail_next = 1
    assert _consume(q, consumer, [first], delete=True) == set()
    assert consumer.outcomes == [ConsumeOutcome.RETRY_SCHEDULED]
    record = store.get(business, outbox_id)
    assert record is not None and record.state == DeliveryState.RETRYABLE
    assert record.attempts == 1 and record.provider_id is None and not sender.deliveries

    # 2. The retry is dispatched again, plus a duplicate (a dispatcher crash after enqueue),
    # and one delivery is consumed but not deleted (a consumer crash before delete).
    clock.advance(60)
    await_due()
    q.guard()
    assert DispatchService(dispatch_store, queue, clock).run_once().enqueued == 1
    queue.enqueue(business, outbox_id)
    a, b = _receive_count(q, q.outbox_url, 2)
    assert a["Body"] == b["Body"] == expected_body and a["MessageId"] != b["MessageId"]
    assert _consume(q, consumer, [a], delete=False) == set()  # sent, then "crash"
    assert _consume(q, consumer, [b], delete=True) == set()   # duplicate message: skipped
    assert consumer.outcomes[-2:] == [ConsumeOutcome.SENT, ConsumeOutcome.SKIPPED]
    q.sleep(VISIBILITY + 1)
    (again,) = _receive_count(q, q.outbox_url, 1)
    assert again["MessageId"] == a["MessageId"]
    # The gather may itself outlast a visibility timeout, so compare with a's latest receive.
    assert int(again["Attributes"]["ApproximateReceiveCount"]) == int(
        a["Attributes"]["ApproximateReceiveCount"]) + 1
    assert _consume(q, consumer, [again], delete=True) == set()  # redelivery: skipped
    assert consumer.outcomes[-1] == ConsumeOutcome.SKIPPED
    assert sender.deliveries == [outbox_id]  # exactly one logical delivery
    record = store.get(business, outbox_id)  # the outbox record, not the queue, is authoritative
    assert record is not None and record.state == DeliveryState.SENT
    assert record.provider_id == "synthetic-provider-1" and record.attempts == 2

    # 3. A poison message is received and never deleted until redrive moves it to the DLQ.
    poison = json.dumps({"business_id": business, "outbox_id": ""},
                        sort_keys=True, separators=(",", ":"))
    q.guard()
    q.sqs.send_message(QueueUrl=q.outbox_url, MessageBody=poison)
    for count in range(1, q.max_receive + 1):
        (message,) = _receive_count(q, q.outbox_url, 1)
        assert message["Body"] == poison
        assert message["Attributes"]["ApproximateReceiveCount"] == str(count)
        assert _consume(q, consumer, [message], delete=True) == {message["MessageId"]}
        q.sleep(VISIBILITY + 1)
    deadline = q.monotonic() + 120
    dead: list[dict[str, Any]] = []
    while not dead:
        # The move happens on the receive attempt after maxReceiveCount, so keep polling both.
        assert not _receive(q, q.outbox_url)
        dead = q.sqs.receive_message(
            QueueUrl=q.dlq_url, MaxNumberOfMessages=1, VisibilityTimeout=60, WaitTimeSeconds=2,
        ).get("Messages", [])
        if not dead and q.monotonic() >= deadline:
            pytest.fail("The poison message did not reach the DLQ")
        if not dead:
            q.sleep(1)
    assert len(dead) == 1 and dead[0]["Body"] == poison
    with pytest.raises(ValueError, match="outbox_id"):
        decode_queue_message(dead[0]["Body"])  # inspected: why it can never be consumed
    # Inspected, not replayed: the record and deliveries are untouched, nothing re-entered
    # the main queue, and the record is disposed of by deleting it, never by redrive.
    assert sender.deliveries == [outbox_id]
    assert store.get(business, outbox_id) == record
    assert not _receive(q, q.outbox_url)
    q.sqs.delete_message(QueueUrl=q.dlq_url, ReceiptHandle=dead[0]["ReceiptHandle"])


def cleanup_run_messages(q: QueueEnv) -> None:
    """Delete only this run's messages, matched by business id; never purge."""
    leftovers: list[str] = []
    for url in (q.outbox_url, q.dlq_url):
        deadline = q.monotonic() + 120
        while True:
            if _pending_counts(q, url) == 0:  # never receive from a queue that is already empty
                break
            response = q.sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=5, WaitTimeSeconds=2,
            )
            for message in response.get("Messages", []):
                try:
                    mine = json.loads(message["Body"]).get("business_id") == q.business
                except ValueError:
                    mine = False
                if mine:
                    q.sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
                else:
                    leftovers.append(f"{url}: message {message['MessageId']} is not this run's")
            if leftovers or _pending_counts(q, url) == 0:
                break
            if q.monotonic() >= deadline:
                leftovers.append(f"{url}: messages still present after 120 seconds")
                break
            q.sleep(1)
    if leftovers:
        raise RuntimeError(
            f"Cleanup for run {q.business} left queue messages; inspect by hand, never purge: "
            + "; ".join(leftovers)
        )


def _dev_queues(business: str) -> QueueEnv:
    sqs = boto3.client("sqs", region_name=DEV_REGION)
    if sqs.meta.endpoint_url != SQS_ENDPOINT:
        raise ValueError(f"Unexpected SQS endpoint {sqs.meta.endpoint_url!r}")
    outbox = require_queue_url(OUTBOX_QUEUE, sqs.get_queue_url(QueueName=OUTBOX_QUEUE)["QueueUrl"])
    dlq = require_queue_url(DLQ_QUEUE, sqs.get_queue_url(QueueName=DLQ_QUEUE)["QueueUrl"])
    if outbox.rsplit("/", 2)[-2] != dlq.rsplit("/", 2)[-2]:
        raise ValueError("The outbox queue and DLQ belong to different accounts")
    cloudformation = boto3.client("cloudformation", region_name=DEV_REGION)
    lambda_client = boto3.client("lambda", region_name=DEV_REGION)
    events = boto3.client("events", region_name=DEV_REGION)

    def guard() -> None:
        require_consumers_off(cloudformation, lambda_client, events)

    guard()  # before anything is sent or received
    q = _checked_queues(QueueEnv(sqs, outbox, dlq, 0, business, sleep, monotonic, guard))
    print(f"Synthetic outbox queue run: business {business}; queues {OUTBOX_QUEUE}, {DLQ_QUEUE}; "
          f"max receive count {q.max_receive}", flush=True)
    return q


def _checked_queues(q: QueueEnv) -> QueueEnv:
    attributes = q.sqs.get_queue_attributes(
        QueueUrl=q.outbox_url, AttributeNames=["All"])["Attributes"]
    redrive = json.loads(attributes["RedrivePolicy"])
    maximum = int(redrive["maxReceiveCount"])
    if not redrive["deadLetterTargetArn"].endswith(f":{DLQ_QUEUE}"):
        raise ValueError(f"Outbox queue redrives to {redrive['deadLetterTargetArn']!r}")
    if not 1 <= maximum <= MAX_RECEIVE_LIMIT:
        raise ValueError(f"Unexpected maxReceiveCount {maximum}")
    # An empty start keeps every receive inside this run; another producer would be left alone.
    for url in (q.outbox_url, q.dlq_url):
        if _pending_counts(q, url):
            raise ValueError(f"{url} is not empty; wait for it to drain and inspect it first")
    return replace(q, max_receive=maximum)


@pytest.fixture
def dynamo_queue_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[RaceEnv, QueueEnv]]:
    if os.environ.get("SCHEDULING_DEV_TABLE"):
        if not os.environ.get("SCHEDULING_DEV_OUTBOX_QUEUE"):
            pytest.skip("Set SCHEDULING_DEV_OUTBOX_QUEUE=scheduling-outbox-dev to use real SQS")
        dev_queue_name()
        for env in _dev_env():  # table, region, endpoint guards, CI skip, run id, cleanup
            q = _dev_queues(env.business)
            try:
                yield env, q
            finally:
                cleanup_run_messages(q)
        return
    for env in _local_env():
        q = _fake_queue_env(env.business)
        try:
            yield env, q
        finally:
            cleanup_run_messages(q)


def _fake_queue_env(business: str) -> QueueEnv:
    fake = FakeSqs()
    return _checked_queues(QueueEnv(
        fake, fake.get_queue_url(QueueName=OUTBOX_QUEUE)["QueueUrl"],
        fake.get_queue_url(QueueName=DLQ_QUEUE)["QueueUrl"], 0, business,
        fake.advance, lambda: fake.now,
    ))


def test_outbox_dispatch_fake_consumer_retry_duplicates_and_dlq(
    dynamo_queue_env: tuple[RaceEnv, QueueEnv],
) -> None:
    env, q = dynamo_queue_env
    before = _appointment(env, "request", CalendarStatus.PENDING_APPROVAL)
    _seed(env, (before,))
    decision_at = EXPIRY - timedelta(seconds=1)
    commit = _commit(
        env, before, replace(before, status=CalendarStatus.CONFIRMED, version=2),
        Action.APPROVE, decision_at,
    )
    DynamoDBCalendarRepository(env.client, env.table).commit_transition(7, commit)
    store = DynamoOutboxStore(env.client, env.table)
    scoped = _RunScopedStore(store, env.business)
    clock = Clock(decision_at)

    def await_due() -> None:
        try:
            _wait_until(lambda: bool(list(scoped.due(clock.now, 100))),
                        "Outbox item did not appear in the due index")
        except (BotoCoreError, ClientError) as exc:  # pragma: no cover - deployed only
            pytest.fail(str(exc))

    prove_outbox_queue_path(q, store, scoped, commit.outbox[0].outbox_id, clock, await_due)


def test_fake_queue_path_runs_in_memory_without_dynamodb() -> None:
    business = f"synthetic-run-{uuid4().hex[:12]}"
    q = _fake_queue_env(business)
    fake = q.sqs
    store = MemoryStore()
    store.record = replace(store.record, business_id=business)
    now = store.record.next_attempt_at
    assert now is not None and now.tzinfo == UTC
    prove_outbox_queue_path(
        q, store, store, store.record.outbox_id, Clock(now), lambda: None,
    )
    cleanup_run_messages(q)
    assert _pending_counts(q, q.outbox_url) == _pending_counts(q, q.dlq_url) == 0
    # The only queue calls are the harness's; nothing purges or replays.
    assert set(fake.calls) <= {
        "get_queue_url", "get_queue_attributes", "send_message", "receive_message",
        "delete_message",
    }


def test_cleanup_deletes_only_the_run_and_refuses_foreign_messages() -> None:
    fake = FakeSqs()
    url = fake.get_queue_url(QueueName=OUTBOX_QUEUE)["QueueUrl"]
    dlq = fake.get_queue_url(QueueName=DLQ_QUEUE)["QueueUrl"]
    mine = json.dumps({"business_id": "synthetic-run-a", "outbox_id": "x"})
    other = json.dumps({"business_id": "dev-synthetic", "outbox_id": "y"})
    fake.send_message(QueueUrl=url, MessageBody=mine)
    fake.send_message(QueueUrl=url, MessageBody=other)
    q = QueueEnv(fake, url, dlq, 5, "synthetic-run-a", fake.advance, lambda: fake.now)
    with pytest.raises(RuntimeError, match="not this run's"):
        cleanup_run_messages(q)
    fake.advance(10)
    remaining = fake.receive_message(QueueUrl=url, MaxNumberOfMessages=10)["Messages"]
    assert [m["Body"] for m in remaining] == [other]


def test_queue_guards_accept_only_the_two_dev_queues() -> None:
    url = f"{SQS_ENDPOINT}/123456789012/{OUTBOX_QUEUE}"
    assert require_queue_url(OUTBOX_QUEUE, url) == url
    for bad in (
        url.replace("us-west-1", "us-east-1"),
        f"{SQS_ENDPOINT}/123456789012/scheduling-sms-conversation-dev",
        f"{url}-extra",
        "http://127.0.0.1:9324/000000000000/" + OUTBOX_QUEUE,
    ):
        with pytest.raises(ValueError, match="not the"):
            require_queue_url(OUTBOX_QUEUE, bad)
    with pytest.raises(ValueError, match="not the"):
        require_queue_url(DLQ_QUEUE, url)


def test_queue_name_guard_and_nonempty_queue_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEDULING_DEV_OUTBOX_QUEUE", OUTBOX_QUEUE)
    assert dev_queue_name() == OUTBOX_QUEUE
    for bad in ("", "scheduling-outbox-dlq-dev", "scheduling-outbox-pilot"):
        monkeypatch.setenv("SCHEDULING_DEV_OUTBOX_QUEUE", bad)
        with pytest.raises(ValueError, match="SCHEDULING_DEV_OUTBOX_QUEUE"):
            dev_queue_name()
    fake = FakeSqs()
    url = fake.get_queue_url(QueueName=OUTBOX_QUEUE)["QueueUrl"]
    dlq = fake.get_queue_url(QueueName=DLQ_QUEUE)["QueueUrl"]
    fake.send_message(QueueUrl=url, MessageBody="{}")
    with pytest.raises(ValueError, match="not empty"):
        _checked_queues(QueueEnv(fake, url, dlq, 0, "synthetic-run-a", fake.advance,
                                 lambda: fake.now))
    wrong = FakeSqs(max_receive=50)
    with pytest.raises(ValueError, match="maxReceiveCount"):
        _checked_queues(QueueEnv(wrong, url, dlq, 0, "synthetic-run-a", wrong.advance,
                                 lambda: wrong.now))


class FakeAws:
    """Stands in for the CloudFormation, Lambda and EventBridge read calls."""

    def __init__(self, mapping: str = "Disabled", rule: str = "DISABLED",
                 queue: str = OUTBOX_QUEUE) -> None:
        self.mapping, self.rule, self.queue = mapping, rule, queue
        self.missing: set[str] = set()

    def describe_stack_resource(self, StackName: str, LogicalResourceId: str) -> dict[str, Any]:
        assert StackName == STACK
        if LogicalResourceId in self.missing:
            raise KeyError(LogicalResourceId)
        return {"StackResourceDetail": {"PhysicalResourceId": f"phys-{LogicalResourceId}"}}

    def get_event_source_mapping(self, UUID: str) -> dict[str, Any]:
        assert UUID == f"phys-{SENDER_MAPPING_LOGICAL_ID}"
        return {"State": self.mapping,
                "EventSourceArn": f"arn:aws:sqs:us-west-1:123456789012:{self.queue}"}

    def describe_rule(self, Name: str) -> dict[str, Any]:
        assert Name == f"phys-{DISPATCH_RULE_LOGICAL_ID}"
        return {"State": self.rule}


def test_consumer_guard_passes_only_when_mapping_and_dispatcher_are_off() -> None:
    aws = FakeAws()
    require_consumers_off(aws, aws, aws)
    for kwargs, message in (
        ({"mapping": "Enabled"}, "not 'Disabled'"),
        ({"mapping": "Enabling"}, "not 'Disabled'"),
        ({"mapping": "Disabling"}, "not 'Disabled'"),
        ({"rule": "ENABLED"}, "not 'DISABLED'"),
        ({"queue": "scheduling-sms-conversation-dev"}, "does not read the outbox queue"),
    ):
        bad = FakeAws(**kwargs)
        with pytest.raises(ValueError, match=message):
            require_consumers_off(bad, bad, bad)
    for logical in (SENDER_MAPPING_LOGICAL_ID, DISPATCH_RULE_LOGICAL_ID):
        lost = FakeAws()
        lost.missing.add(logical)
        with pytest.raises(ValueError, match="Could not confirm"):
            require_consumers_off(lost, lost, lost)


def _guarded_run(aws: FakeAws, flip_after: int | None) -> FakeSqs:
    q = _fake_queue_env(f"synthetic-run-{uuid4().hex[:12]}")
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if flip_after is not None and calls > flip_after:
            aws.mapping = "Enabled"
        require_consumers_off(aws, aws, aws)

    q = replace(q, guard=guard)
    store = MemoryStore()
    store.record = replace(store.record, business_id=q.business)
    now = store.record.next_attempt_at
    assert now is not None
    try:
        prove_outbox_queue_path(q, store, store, store.record.outbox_id, Clock(now),
                                lambda: None)
    finally:
        cleanup_run_messages(q)
    fake: FakeSqs = q.sqs
    return fake


def test_scenario_refuses_to_send_when_the_mapping_is_enabled() -> None:
    with pytest.raises(ValueError, match="not 'Disabled'"):
        _guarded_run(FakeAws(mapping="Enabled"), None)


def test_scenario_rechecks_the_mapping_before_the_poison_step() -> None:
    # The mapping flips after the two dispatch checks; the poison step must still refuse.
    with pytest.raises(ValueError, match="not 'Disabled'"):
        _guarded_run(FakeAws(), 2)


def test_scenario_survives_gathers_that_outlast_the_visibility_timeout() -> None:
    fake = FakeSqs(per_call=1)
    q = _checked_queues(QueueEnv(
        fake, fake.get_queue_url(QueueName=OUTBOX_QUEUE)["QueueUrl"],
        fake.get_queue_url(QueueName=DLQ_QUEUE)["QueueUrl"], 0,
        f"synthetic-run-{uuid4().hex[:12]}",
        lambda s: fake.advance(2.0 if s < 1 else s), lambda: fake.now,
    ))
    store = MemoryStore()
    store.record = replace(store.record, business_id=q.business)
    now = store.record.next_attempt_at
    assert now is not None
    prove_outbox_queue_path(q, store, store, store.record.outbox_id, Clock(now), lambda: None)
    cleanup_run_messages(q)
