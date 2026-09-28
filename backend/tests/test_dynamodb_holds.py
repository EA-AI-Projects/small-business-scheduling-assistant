"""Verify the DynamoDB adapter requests strong reads and one guarded transaction."""

from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository, encode_policy
from scheduling.adapters.outbox_aws import DynamoOutboxStore
from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import pilot_policy
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.holds import (
    CreateHold,
    HoldCommit,
    OutboxIntent,
    PendingHold,
    RevisionConflict,
)
from scheduling.domain.owner_calendar import (
    OwnerAction,
    OwnerCalendarCommand,
    OwnerCalendarCommit,
    OwnerCalendarResult,
    UnavailableBlock,
)
from scheduling.domain.owner_policy import PolicyCommand, PolicyCommit, PolicyRecord, PolicyResult


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


def test_pending_owner_requests_reread_metadata_and_exclude_expired_entries() -> None:
    class PendingClient(RecordingClient):
        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs["Key"]["PK"]["S"] == "APPOINTMENT#active":
                return {"Item": {
                    "business_id": {"S": "business-1"}, "client_id": {"S": "client-1"},
                    "start_at": {"S": "2026-09-29T16:00:00+00:00"},
                    "end_at": {"S": "2026-09-29T17:00:00+00:00"},
                    "status": {"S": "PENDING_APPROVAL"},
                    "hold_expires_at": {"S": "2026-09-28T17:00:00+00:00"},
                    "duration_minutes": {"N": "60"}, "buffer_minutes": {"N": "30"},
                    "version": {"N": "1"},
                }}
            return {}

        def query(self, **kwargs: Any) -> dict[str, Any]:
            self.queries.append(kwargs)
            return {"Items": [
                {"event_id": {"S": "active"}, "status": {"S": "PENDING_APPROVAL"}},
                {"event_id": {"S": "confirmed"}, "status": {"S": "CONFIRMED"}},
            ]}

    client = PendingClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    now = datetime(2026, 9, 28, 16, tzinfo=UTC)
    assert [request.appointment_id for request in repository.read_pending_requests(
        "business-1", now
    )] == ["active"]
    assert repository.read_pending_requests("business-1", now.replace(hour=18)) == ()
    assert all(query["ConsistentRead"] for query in client.queries)


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
        start,
    )

    repository.commit_hold(7, commit)

    writes = client.transactions[0]["TransactItems"]
    assert len(writes) == 7
    assert writes[0]["Update"]["ConditionExpression"] == "revision = :old"
    assert writes[0]["Update"]["ExpressionAttributeValues"][":old"] == {"N": "7"}
    assert all(write["Put"]["ConditionExpression"] == "attribute_not_exists(PK)" for write in writes[1:])
    sort_keys = [write["Put"]["Item"]["SK"]["S"] for write in writes[1:]]
    assert "META" in sort_keys
    metadata = next(
        write["Put"]["Item"] for write in writes[1:]
        if write["Put"]["Item"]["SK"]["S"] == "META"
    )
    assert metadata["hold_due_pk"] == {"S": "HOLD#PENDING"}
    assert metadata["hold_due_sk"] == {
        "S": f"{hold.hold_expires_at.isoformat(timespec='microseconds')}#hold-1"
    }
    assert any(key.startswith("EVENT#") for key in sort_keys)
    assert any(key.startswith("IDEMPOTENCY#") for key in sort_keys)
    assert any(key.startswith("AUDIT#") for key in sort_keys)
    assert sum(key.startswith("OUTBOX#") for key in sort_keys) == 2
    notices = [
        write["Put"]["Item"] for write in writes[1:]
        if write["Put"]["Item"]["SK"]["S"].startswith("OUTBOX#")
    ]
    assert all(item["outbox_due_pk"] == {"S": "OUTBOX#PENDING"} for item in notices)
    assert all(item["dispatch_after"] == item["next_attempt_at"] for item in notices)
    assert all(item["created_at"] == {"S": start.isoformat(timespec="microseconds")} for item in notices)


def test_due_hold_index_query_is_bounded_and_paginates() -> None:
    class DueClient(RecordingClient):
        def query(self, **kwargs: Any) -> dict[str, Any]:
            self.queries.append(kwargs)
            if len(self.queries) == 1:
                return {
                    "Items": [{"PK": {"S": "APPOINTMENT#hold-1"}}],
                    "LastEvaluatedKey": {"PK": {"S": "APPOINTMENT#hold-1"}},
                }
            return {"Items": [{"PK": {"S": "APPOINTMENT#hold-2"}}]}

    client = DueClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    now = datetime(2026, 9, 29, 16, tzinfo=UTC)

    assert repository.due_hold_ids(now, 2) == ("hold-1", "hold-2")
    assert client.queries[0]["IndexName"] == "HoldDueIndex"
    assert client.queries[0]["Limit"] == 2
    assert client.queries[1]["Limit"] == 1
    assert client.queries[1]["ExclusiveStartKey"] == {
        "PK": {"S": "APPOINTMENT#hold-1"}
    }
    assert client.queries[0]["ExpressionAttributeValues"][":due"] == {
        "S": f"{now.isoformat(timespec='microseconds')}#~"
    }


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
    commit = HoldCommit(command, command.request_hash(), hold, "audit-1", (), start)

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


def test_replacement_hold_claims_original_guard_in_same_transaction() -> None:
    client = RecordingClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    start = datetime(2026, 9, 29, 16, tzinfo=UTC)
    command = CreateHold(
        "business-1", "client-1", "client-1", "replacement-key", start, 60,
        "original-1",
    )
    hold = PendingHold(
        "replacement-1", "business-1", "client-1", start, start.replace(hour=17),
        start.replace(day=30), 60, 30, 8, "original-1",
    )
    commit = HoldCommit(
        command, command.request_hash(), hold, "audit-1",
        (
            OutboxIntent("owner-1", hold.hold_id, "owner", "hold-request"),
            OutboxIntent("client-1", hold.hold_id, "client", "hold-pending"),
        ),
        start,
    )

    repository.commit_hold(7, commit)

    writes = client.transactions[0]["TransactItems"]
    guard = writes[-1]["Put"]
    assert guard["Item"]["SK"] == {"S": "REPLACEMENT#original-1"}
    assert guard["Item"]["replacement_id"] == {"S": "replacement-1"}
    assert guard["ConditionExpression"] == "attribute_not_exists(PK) OR expires_at <= :now"


def test_policy_seed_and_edit_write_revision_version_audit_and_outbox_atomically() -> None:
    client = RecordingClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    policy = pilot_policy()
    now = datetime(2026, 9, 28, 15, tzinfo=UTC)
    seed = PolicyCommand("business-1", "owner-1", "seed", 0, None, policy)
    repository.commit_policy(0, PolicyCommit(
        seed, seed.request_hash(), PolicyResult(PolicyRecord(policy, 1), 1), now, "seed-audit"
    ))

    writes = client.transactions[0]["TransactItems"]
    assert len(writes) == 5
    assert writes[0]["Update"]["ConditionExpression"] == "attribute_not_exists(PK)"
    assert writes[1]["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"
    assert writes[1]["Put"]["Item"]["version"] == {"N": "1"}
    assert writes[3]["Put"]["Item"]["SK"] == {"S": "AUDIT#seed-audit"}
    assert writes[4]["Put"]["Item"]["outbox_due_pk"] == {"S": "OUTBOX#PENDING"}

    edit = PolicyCommand("business-1", "owner-1", "edit", 1, 1, policy)
    repository.commit_policy(1, PolicyCommit(
        edit, edit.request_hash(), PolicyResult(PolicyRecord(policy, 2), 2), now, "edit-audit"
    ))
    edited = client.transactions[1]["TransactItems"]
    assert edited[0]["Update"]["ConditionExpression"] == "revision = :old"
    assert edited[1]["Put"]["ConditionExpression"] == "#version = :old_version"
    assert edited[1]["Put"]["ExpressionAttributeValues"][":old_version"] == {"N": "1"}
    assert DynamoOutboxStore._record(writes[4]["Put"]["Item"]).entity_id == "business-1"


def test_owner_block_and_manual_appointment_write_guarded_transactions() -> None:
    client = RecordingClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    now = datetime(2026, 9, 28, 15, tzinfo=UTC)
    start = datetime(2026, 9, 29, 16, tzinfo=UTC)
    block = UnavailableBlock("block-1", "business-1", start, start.replace(hour=17), 1)
    create = OwnerCalendarCommand(
        "business-1", "owner-1", "block-key", OwnerAction.CREATE_BLOCK, 7,
        start_at=block.start_at, end_at=block.end_at,
    )
    intent = OutboxIntent("block-notice", block.block_id, "owner", "block_time")
    repository.commit_owner_calendar(7, OwnerCalendarCommit(
        create, create.request_hash(), None, OwnerCalendarResult(8, block), now,
        "block-audit", (intent,),
    ))
    block_writes = client.transactions[0]["TransactItems"]
    assert block_writes[0]["Update"]["ConditionExpression"] == "revision = :old"
    assert block_writes[1]["Put"]["Item"]["SK"] == {"S": "BLOCK#block-1"}
    assert block_writes[1]["Put"]["Item"]["version"] == {"N": "1"}
    assert DynamoOutboxStore._record(block_writes[-1]["Put"]["Item"]).entity_id == "block-1"

    moved = UnavailableBlock("block-1", "business-1", start.replace(hour=18),
                             start.replace(hour=19), 2)
    move = OwnerCalendarCommand(
        "business-1", "owner-1", "move-key", OwnerAction.MOVE_BLOCK, 8,
        block_id="block-1", expected_version=1,
        start_at=moved.start_at, end_at=moved.end_at,
    )
    repository.commit_owner_calendar(8, OwnerCalendarCommit(
        move, move.request_hash(), block, OwnerCalendarResult(9, moved), now,
        "move-audit", (intent,),
    ))
    move_put = client.transactions[1]["TransactItems"][1]["Put"]
    assert move_put["ConditionExpression"] == "#version = :old_version"
    assert move_put["ExpressionAttributeValues"][":old_version"] == {"N": "1"}

    remove = OwnerCalendarCommand(
        "business-1", "owner-1", "remove-key", OwnerAction.REMOVE_BLOCK, 9,
        block_id="block-1", expected_version=2,
    )
    repository.commit_owner_calendar(9, OwnerCalendarCommit(
        remove, remove.request_hash(), moved, OwnerCalendarResult(10), now,
        "remove-audit", (intent,),
    ))
    remove_delete = client.transactions[2]["TransactItems"][1]["Delete"]
    assert remove_delete["ConditionExpression"] == "#version = :old_version"
    assert remove_delete["ExpressionAttributeValues"][":old_version"] == {"N": "2"}

    appointment = Appointment(
        "manual-1", "business-1", "client-1", start, start.replace(hour=17),
        CalendarStatus.CONFIRMED, None, 60, 30, 1,
    )
    manual = OwnerCalendarCommand(
        "business-1", "owner-1", "manual-key", OwnerAction.CREATE_APPOINTMENT, 8,
        start_at=start, client_id="client-1", duration_minutes=60,
    )
    client_notice = OutboxIntent("manual-notice", appointment.appointment_id,
                                 "client", "create_owner_appointment")
    repository.commit_owner_calendar(8, OwnerCalendarCommit(
        manual, manual.request_hash(), None, OwnerCalendarResult(9, appointment=appointment),
        now, "manual-audit", (client_notice,),
    ))
    manual_writes = client.transactions[3]["TransactItems"]
    assert manual_writes[1]["Put"]["Item"]["PK"] == {"S": "APPOINTMENT#manual-1"}
    assert manual_writes[2]["Put"]["Item"]["event_id"] == {"S": "manual-1"}
    assert manual_writes[1]["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"
    assert DynamoOutboxStore._record(manual_writes[-1]["Put"]["Item"]).entity_id == "manual-1"
