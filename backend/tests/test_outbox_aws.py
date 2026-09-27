"""DynamoDB/SQS adapter requests preserve the due index and lease conditions."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from scheduling.adapters.outbox_aws import DynamoOutboxStore, SQSIntentQueue, due_keys
from scheduling.domain.outbox import DeliveryState

NOW = datetime(2026, 9, 28, 16, tzinfo=UTC)


class RecordingClient:
    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self.sends: list[dict[str, Any]] = []
        self.pages: list[dict[str, Any]] = []
        self.item = {
            "PK": {"S": "BUSINESS#business-1"},
            "SK": {"S": "OUTBOX#notice-1"},
            "outbox_id": {"S": "notice-1"},
            "hold_id": {"S": "appointment-1"},
            "recipient": {"S": "client"},
            "template": {"S": "hold-pending"},
            "event_version": {"N": "1"},
            "delivery_state": {"S": "PENDING"},
            "created_at": {"S": NOW.isoformat(timespec="microseconds")},
            "next_attempt_at": {"S": NOW.isoformat(timespec="microseconds")},
            "dispatch_after": {"S": NOW.isoformat(timespec="microseconds")},
            "attempts": {"N": "0"},
            **due_keys(DeliveryState.PENDING, NOW, "notice-1"),
        }

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        return self.pages.pop(0) if self.pages else {"Items": []}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.gets.append(kwargs)
        return {"Item": self.item}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        if kwargs.get("ReturnValues") == "ALL_NEW":
            return {"Attributes": {
                **self.item,
                "delivery_state": {"S": "SENDING"},
                "next_attempt_at": kwargs["ExpressionAttributeValues"][":lease"],
                "dispatch_after": kwargs["ExpressionAttributeValues"][":lease"],
                "attempts": {"N": "1"},
                "lease_token": kwargs["ExpressionAttributeValues"][":token"],
            }}
        return {}

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        self.sends.append(kwargs)
        return {"MessageId": "queue-1"}


def test_due_query_paginates_index_keys_then_strongly_reads_base_item() -> None:
    client = RecordingClient()
    key = {"PK": {"S": "BUSINESS#business-1"}, "SK": {"S": "OUTBOX#notice-1"}}
    client.pages = [
        {"Items": [key], "LastEvaluatedKey": key},
        {"Items": []},
    ]
    store = DynamoOutboxStore(client, "scheduling")

    assert list(store.due(NOW, 10)) == [("business-1", "notice-1")]
    assert client.queries[0]["IndexName"] == "OutboxDueIndex"
    assert "ConsistentRead" not in client.queries[0]
    assert client.queries[1]["ExclusiveStartKey"] == key
    assert store.get("business-1", "notice-1") is not None
    assert client.gets[0]["ConsistentRead"] is True


def test_dispatch_and_delivery_updates_use_conditional_ownership() -> None:
    client = RecordingClient()
    store = DynamoOutboxStore(client, "scheduling")
    record = store.get("business-1", "notice-1")
    assert record is not None
    store.mark_enqueued(record, NOW, NOW + timedelta(minutes=5))
    assert "dispatch_after = :old_dispatch" in client.updates[0]["ConditionExpression"]
    assert "next_attempt_at = :old_attempt" in client.updates[0]["ConditionExpression"]

    claimed = store.claim(record, NOW, NOW + timedelta(minutes=2))
    claim = client.updates[1]
    assert "next_attempt_at <= :now" in claim["ConditionExpression"]
    assert "if_not_exists(attempts, :zero) + :one" in claim["UpdateExpression"]
    assert claimed.state == DeliveryState.SENDING
    assert claimed.attempts == 1
    store.mark_sent(claimed, "provider-1", NOW)
    assert "lease_token = :token" in client.updates[2]["ConditionExpression"]
    assert "REMOVE outbox_due_pk, outbox_due_sk" in client.updates[2]["UpdateExpression"]


def test_retry_and_terminal_failure_update_due_index_and_sqs_body() -> None:
    client = RecordingClient()
    store = DynamoOutboxStore(client, "scheduling")
    record = store.get("business-1", "notice-1")
    assert record is not None
    claimed = store.claim(record, NOW, NOW + timedelta(minutes=2))

    store.mark_failure(
        claimed, DeliveryState.RETRYABLE, NOW + timedelta(seconds=30),
        "PROVIDER_UNAVAILABLE", NOW,
    )
    retry = client.updates[1]
    assert retry["ExpressionAttributeValues"][":due_pk"] == {"S": "OUTBOX#RETRYABLE"}
    assert "lease_token = :token" in retry["ConditionExpression"]

    store.mark_failure(claimed, DeliveryState.FAILED, None, "PROVIDER_UNAVAILABLE", NOW)
    terminal = client.updates[2]
    assert "REMOVE outbox_due_pk, outbox_due_sk" in terminal["UpdateExpression"]

    SQSIntentQueue(client, "queue-url").enqueue("business-1", "notice-1")
    assert json.loads(client.sends[0]["MessageBody"]) == {
        "business_id": "business-1", "outbox_id": "notice-1",
    }
