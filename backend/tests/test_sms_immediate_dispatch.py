"""Synthetic worker/persistence/queue boundary; no AWS, model or provider calls."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scheduling.adapters.counteroffer_dynamodb import DynamoCounterofferStore
from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.outbox_aws import DynamoOutboxStore
from scheduling.domain.conversation import ConversationOutcome
from scheduling.domain.holds import CreateHold, HoldCommit, HoldService
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    LifecycleService,
    TransitionCommit,
)
from scheduling.domain.outbox import DeliveryState, DispatchService, OutboxRecord
from scheduling.domain.owner_counteroffer import Counteroffer, OfferState
from scheduling.domain.sms_ingress import InboundReceipt, Keyword, SenderRole
from scheduling.workers import sms_conversation

NOW = datetime(2026, 10, 19, tzinfo=UTC)


class Dynamo:
    """Capture successful writes; conditional expressions are covered separately."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.failed_write = False
        self.enqueued: list[str] = []

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        if self.failed_write:
            raise RuntimeError("database unavailable")
        for write in kwargs["TransactItems"]:
            if "Put" in write:
                item = write["Put"]["Item"]
                self.items[item["SK"]["S"]] = item
            if "Update" in write:
                update = write["Update"]
                item = self.items.setdefault(update["Key"]["SK"]["S"], {})
                values = update.get("ExpressionAttributeValues", {})
                if ":text" in values:
                    item["reply_text"] = values[":text"]
                if ":provider" in values:
                    item["reply_provider_id"] = values[":provider"]
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self.items.get(kwargs["Key"]["SK"]["S"])
        return {"Item": item} if item else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        if ":next" in values:
            item = self.items[kwargs["Key"]["SK"]["S"]]
            item["dispatch_after"] = values[":next"]
        return {}


@pytest.mark.parametrize("role", [SenderRole.CLIENT, SenderRole.OWNER])
@pytest.mark.parametrize("failure", [None, "queue", "write", "after_commit"])
def test_worker_hands_off_only_saved_replies_after_processing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    role: SenderRole, failure: str | None,
) -> None:
    dynamo = Dynamo()
    finished: list[str] = []
    processed: list[str] = []
    queue_attempts: list[str] = []

    class Queue:
        def send_message(self, **kwargs: Any) -> dict[str, str]:
            payload = json.loads(kwargs["MessageBody"])
            outbox_id = payload["outbox_id"]
            provider_id = outbox_id.removeprefix("sms-reply#")
            assert provider_id in finished  # Never send before drafting finishes.
            assert f"OUTBOX#{outbox_id}" in dynamo.items  # Persist before enqueue.
            assert set(payload) == {"business_id", "outbox_id"}
            queue_attempts.append(outbox_id)
            if failure == "queue" and provider_id == "SM-first":
                raise RuntimeError("queue unavailable")
            dynamo.enqueued.append(outbox_id)
            return {"MessageId": "synthetic"}

    def client(name: str, **kwargs: Any) -> Any:
        if name == "sqs":
            config = kwargs["config"]
            assert config.connect_timeout == 1 and config.read_timeout == 2
            assert config.retries == {"total_max_attempts": 1}
            return Queue()
        return dynamo

    class Processor:
        def __init__(self, store: Any, *_args: Any, **_kwargs: Any) -> None:
            self.store = store

        def process(self, business_id: str, provider_id: str) -> None:
            processed.append(provider_id)
            receipt = InboundReceipt(
                business_id, provider_id, "+15005550006", "+15005550009", "Hello",
                NOW, role, "client-1" if role == SenderRole.CLIENT else None,
                Keyword.OTHER, True)
            dynamo.failed_write = failure == "write" and provider_id == "SM-first"
            # Real adapter persists reply + outbox atomically, then records handoff.
            try:
                self.store.put_reply(receipt, "Synthetic completed answer", "lease", NOW)
            finally:
                finished.append(provider_id)
            if failure == "after_commit" and provider_id == "SM-first":
                raise RuntimeError("worker failed after saving reply")

    for key, value in {
        "SMS_CONVERSATION_ENABLED": "authorized", "SCHEDULING_TABLE_NAME": "synthetic",
        "BUSINESS_ID": "pilot", "OUTBOX_QUEUE_URL": "https://sqs.example.test/outbox",
        "OPENAI_API_KEY_PARAM": "/scheduling/synthetic/key",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sms_conversation.boto3, "client", client)
    monkeypatch.setattr(sms_conversation, "_openai_key", lambda *_: "synthetic")
    monkeypatch.setattr(sms_conversation, "OpenAIMessageInterpreter", lambda *_, **__: object())
    monkeypatch.setattr(sms_conversation, "ReceiptProcessor", Processor)

    records = [{"messageId": sid, "body": json.dumps({
        "business_id": "pilot", "provider_id": sid})} for sid in ("SM-first", "SM-second")]
    result = sms_conversation.handler({"Records": records}, None)
    expected_failures = ([{"itemIdentifier": "SM-first"}]
                         if failure in ("write", "after_commit") else [])
    assert result == {"batchItemFailures": expected_failures}
    assert processed == ["SM-first", "SM-second"]
    assert "sms-reply#SM-second" in dynamo.enqueued
    first = dynamo.items.get("OUTBOX#sms-reply#SM-first")
    if failure == "write":
        assert first is None
        assert "sms-reply#SM-first" not in queue_attempts
    elif failure == "queue":
        assert first is not None and first["dispatch_after"]["S"] == NOW.isoformat(
            timespec="microseconds")  # Still due for scheduled recovery.
        assert "sms-reply#SM-first" not in dynamo.enqueued
    else:
        assert "sms-reply#SM-first" in dynamo.enqueued
    logs = capsys.readouterr().out
    assert "Synthetic completed answer" not in logs and "+15005550006" not in logs
    assert '"failed": 1' in logs if failure == "queue" else '"failed": 0' in logs


@pytest.mark.parametrize("crash_after_commit", [False, True])
def test_committed_booking_is_dispatched_after_draft_attachment_or_safe_failure(
    monkeypatch: pytest.MonkeyPatch, crash_after_commit: bool,
) -> None:
    commits: list[HoldCommit] = []

    class Calendar(InMemoryCalendarRepository):
        def commit_hold(self, expected_revision: int, commit: HoldCommit) -> None:
            super().commit_hold(expected_revision, commit)
            commits.append(commit)

    hold = HoldService(Calendar()).create(CreateHold(
        "pilot", "client-1", "client-1", "SM-book", NOW + timedelta(days=1, hours=16), 60), NOW)
    dynamo = Dynamo()
    dynamo.items["SMS#SM-book"] = {
        "provider_id": {"S": "SM-book"}, "sender": {"S": "+15005550006"},
        "recipient": {"S": "+15005550009"}, "body": {"S": "Book that time"},
        "received_at": {"S": NOW.isoformat()}, "role": {"S": "client"},
        "client_id": {"S": "client-1"}, "keyword": {"S": Keyword.OTHER.value},
        "authorized_for_commands": {"BOOL": True},
    }
    handling = False
    final_text = "Your request is pending owner approval."

    class ModelConversation:
        def __init__(self, calendar: Any, *_args: Any, **_kwargs: Any) -> None:
            self.calendar = calendar

        def handle(self, receipt: InboundReceipt) -> ConversationOutcome:
            nonlocal handling
            handling = True
            self.calendar.commit_hold(0, commits[0])  # Actual Dynamo adapter callback.
            assert dynamo.enqueued == []  # No handoff while wording is in progress.
            handling = False
            if crash_after_commit:
                raise RuntimeError("model failed after booking commit")
            return ConversationOutcome(final_text, True, hold.hold_id, f"{hold.hold_id}#client")

    class Queue:
        def send_message(self, **kwargs: Any) -> dict[str, str]:
            assert not handling
            payload = json.loads(kwargs["MessageBody"])
            outbox_id = payload["outbox_id"]
            if outbox_id.endswith("#client"):
                item = dynamo.items[f"OUTBOX#{outbox_id}"]
                if crash_after_commit:
                    assert "reply_provider_id" not in item  # Original safe notification.
                else:
                    assert item["reply_provider_id"] == {"S": "SM-book"}
                    assert dynamo.items["SMS#SM-book"]["reply_text"] == {"S": final_text}
            dynamo.enqueued.append(outbox_id)
            return {"MessageId": "synthetic"}

    for key, value in {
        "SMS_CONVERSATION_ENABLED": "authorized", "SCHEDULING_TABLE_NAME": "synthetic",
        "BUSINESS_ID": "pilot", "OUTBOX_QUEUE_URL": "https://sqs.example.test/outbox",
        "OPENAI_API_KEY_PARAM": "/scheduling/synthetic/key", "OWNER_NUMBER": "+15005550009",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sms_conversation.boto3, "client",
                        lambda name, **_: Queue() if name == "sqs" else dynamo)
    monkeypatch.setattr(sms_conversation, "_openai_key", lambda *_: "synthetic")
    monkeypatch.setattr(sms_conversation, "OpenAIMessageInterpreter", lambda *_, **__: object())
    monkeypatch.setattr(sms_conversation, "ConversationService", ModelConversation)
    result = sms_conversation.handler({"Records": [{
        "messageId": "record", "body": json.dumps({
            "business_id": "pilot", "provider_id": "SM-book"}),
    }]}, None)
    assert result == {"batchItemFailures": ([{"itemIdentifier": "record"}]
                                           if crash_after_commit else [])}
    assert set(dynamo.enqueued) == {f"{hold.hold_id}#client", f"{hold.hold_id}#owner"}
    assert len(dynamo.enqueued) == 2


@pytest.mark.parametrize("kind", ["lifecycle", "counteroffer"])
@pytest.mark.parametrize("failed_write", [False, True])
def test_scheduling_notifications_handoff_only_after_successful_commit(
    kind: str, failed_write: bool,
) -> None:
    dynamo = Dynamo()
    pending: list[tuple[str, str]] = []

    def created(business_id: str, outbox_id: str) -> None:
        assert f"OUTBOX#{outbox_id}" in dynamo.items
        pending.append((business_id, outbox_id))

    if kind == "lifecycle":
        commits: list[TransitionCommit] = []

        class Calendar(InMemoryCalendarRepository):
            def commit_transition(self, revision: int, commit: TransitionCommit) -> None:
                super().commit_transition(revision, commit)
                commits.append(commit)

        calendar = Calendar()
        hold = HoldService(calendar).create(CreateHold(
            "pilot", "client-1", "client-1", "book", NOW + timedelta(days=1, hours=16), 60), NOW)
        LifecycleService(calendar, lambda: NOW).apply(AppointmentCommand(
            "pilot", hold.hold_id, "owner", ActorRole.OWNER, Action.APPROVE, "approve", 1))
        repository = DynamoDBCalendarRepository(dynamo, "synthetic", outbox_created=created)
        write = lambda: repository.commit_transition(1, commits[0])
        expected = {intent.outbox_id for intent in commits[0].outbox}
    else:
        offer = Counteroffer(
            "pilot", "offer-1", "+15005550009", "request-1", 1, "client-1",
            "+15005550006", NOW + timedelta(days=1), 60, "Could tomorrow work?",
            OfferState.PROPOSED, 1, NOW, NOW + timedelta(minutes=30))
        outbox = OutboxRecord(
            business_id="pilot", outbox_id="offer-notice", entity_id="offer-1",
            recipient="client", template="owner-counteroffer", state=DeliveryState.PENDING,
            created_at=NOW, next_attempt_at=NOW, dispatch_after=NOW, event_version=2)
        offers = DynamoCounterofferStore(dynamo, "synthetic", outbox_created=created)
        write = lambda: offers.confirm(offer, "SM-confirm", NOW, outbox)
        expected = {outbox.outbox_id}

    dynamo.failed_write = failed_write
    if failed_write:
        with pytest.raises(RuntimeError, match="database unavailable"):
            write()
        assert pending == []
        return
    write()
    assert {outbox_id for _, outbox_id in pending} == expected

    class Queue:
        def enqueue(self, business_id: str, outbox_id: str) -> None:
            dynamo.enqueued.append(outbox_id)

    dispatcher = DispatchService(DynamoOutboxStore(dynamo, "synthetic"), Queue(), lambda: NOW)
    assert all(dispatcher.dispatch_one(*reference) for reference in pending)
    assert set(dynamo.enqueued) == expected
