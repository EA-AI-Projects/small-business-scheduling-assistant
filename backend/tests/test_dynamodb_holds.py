"""Verify the DynamoDB adapter requests strong reads and one guarded transaction."""

from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository, encode_policy
from scheduling.domain.availability import pilot_policy
from scheduling.domain.holds import (
    CreateHold,
    HoldCommit,
    OutboxIntent,
    PendingHold,
    RevisionConflict,
)


class RecordingClient:
    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []
        self.transactions: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["Key"]["SK"]["S"] == "CALENDAR#REVISION":
            return {"Item": {"revision": {"N": "7"}}}
        if kwargs["Key"]["SK"]["S"] == "POLICY#SCHEDULING":
            return {"Item": {"payload": {"S": encode_policy(pilot_policy())}}}
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        if len(self.queries) == 1:
            return {"Items": [], "LastEvaluatedKey": {"PK": {"S": "next"}}}
        return {"Items": []}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transactions.append(kwargs)
        return {}


def test_bounded_event_query_paginates_and_blocks_are_read_separately() -> None:
    client = RecordingClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    start = datetime(2026, 9, 29, 16, tzinfo=UTC)
    snapshot = repository.read_calendar_for_hold("business-1", start, start.replace(hour=17), pilot_policy())

    assert snapshot.revision == 7
    assert len(client.queries) == 3
    assert all(query["ConsistentRead"] for query in client.queries)
    assert "BETWEEN" in client.queries[0]["KeyConditionExpression"]
    assert client.queries[1]["ExclusiveStartKey"] == {"PK": {"S": "next"}}
    assert client.queries[2]["ExpressionAttributeValues"][":prefix"] == {"S": "BLOCK#"}
    assert repository.read_policy("business-1") == pilot_policy()


def test_hold_transaction_contains_revision_metadata_event_replay_audit_and_notices() -> None:
    client = RecordingClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    start = datetime(2026, 9, 29, 16, tzinfo=UTC)
    command = CreateHold("business-1", "client-1", "client-1", "key-1", start, 60)
    hold = PendingHold(
        "hold-1", "business-1", "client-1", start, start.replace(hour=17),
        start.replace(day=30), 60, 30, 8,
    )
    commit = HoldCommit(
        command, command.request_hash(), hold, "audit-1",
        (
            OutboxIntent("owner-1", "hold-1", "owner", "hold-request"),
            OutboxIntent("client-1", "hold-1", "client", "hold-pending"),
        ),
    )

    repository.commit_hold(7, commit)

    writes = client.transactions[0]["TransactItems"]
    assert len(writes) == 7
    assert writes[0]["Update"]["ConditionExpression"] == "revision = :old"
    assert writes[0]["Update"]["ExpressionAttributeValues"][":old"] == {"N": "7"}
    assert all(write["Put"]["ConditionExpression"] == "attribute_not_exists(PK)" for write in writes[1:])
    sort_keys = [write["Put"]["Item"]["SK"]["S"] for write in writes[1:]]
    assert "META" in sort_keys
    assert any(key.startswith("EVENT#") for key in sort_keys)
    assert any(key.startswith("IDEMPOTENCY#") for key in sort_keys)
    assert any(key.startswith("AUDIT#") for key in sort_keys)
    assert sum(key.startswith("OUTBOX#") for key in sort_keys) == 2


def test_transaction_cancellation_retries_only_expected_conditional_races() -> None:
    class CancellingClient(RecordingClient):
        def __init__(self, reasons: list[dict[str, str]]) -> None:
            super().__init__()
            self.reasons = reasons

        def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                 "CancellationReasons": self.reasons},
                "TransactWriteItems",
            )

    start = datetime(2026, 9, 29, 16, tzinfo=UTC)
    command = CreateHold("business-1", "client-1", "client-1", "key-1", start, 60)
    hold = PendingHold("hold-1", "business-1", "client-1", start, start.replace(hour=17),
                       start.replace(day=30), 60, 30, 8)
    commit = HoldCommit(command, command.request_hash(), hold, "audit-1", ())

    with pytest.raises(RevisionConflict):
        DynamoDBCalendarRepository(
            CancellingClient([{"Code": "ConditionalCheckFailed"}]), "scheduling"
        ).commit_hold(7, commit)
    with pytest.raises(RevisionConflict):
        DynamoDBCalendarRepository(
            CancellingClient([{"Code": "TransactionConflict"}]), "scheduling"
        ).commit_hold(7, commit)
    with pytest.raises(ClientError):
        DynamoDBCalendarRepository(
            CancellingClient([{"Code": "ThrottlingError"}]), "scheduling"
        ).commit_hold(7, commit)
