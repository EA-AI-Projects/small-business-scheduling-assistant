"""DynamoDB client and note writes keep unique identity and scoped keys."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.domain.client_records import ClientNote, ClientProfile, HomeSize, RecordConflict

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)


class RecordingClient:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.scans: list[dict[str, Any]] = []
        self.scan_pages: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []
        self.query_pages: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.reject = False
        self.reject_update = False

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        return self.query_pages.pop(0) if self.query_pages else {"Items": []}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.scans.append(kwargs)
        return self.scan_pages.pop(0)

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transactions.append(kwargs)
        if self.reject:
            raise ClientError({"Error": {"Code": "TransactionCanceledException",
                                         "Message": "conditional failure"}}, "TransactWriteItems")
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        if self.reject_update:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                         "Message": "conditional failure"}}, "UpdateItem")
        return {}


def profile(phone: str = "+14155550101", version: int = 1) -> ClientProfile:
    return ClientProfile("business-1", "client-1", "Synthetic Client", phone,
                         "123 Test Street", HomeSize.SMALL, 60, True,
                         version, NOW, NOW)


def test_profile_phone_uniqueness_uses_conditional_transaction() -> None:
    client = RecordingClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    repository.save_profile(profile(), 0, None)
    created = client.transactions[0]["TransactItems"]
    assert len(created) == 2
    assert created[0]["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"
    assert created[1]["Put"]["Item"]["SK"] == {"S": "PHONE#+14155550101"}
    assert created[1]["Put"]["ConditionExpression"] == "attribute_not_exists(PK)"

    repository.save_profile(profile("+14155550102", 2), 1, "+14155550101")
    moved = client.transactions[1]["TransactItems"]
    assert len(moved) == 3
    assert moved[0]["Put"]["ConditionExpression"] == "version = :old_version"
    assert moved[2]["Delete"]["ConditionExpression"] == "client_id = :client_id"
    client.reject = True
    try:
        repository.save_profile(profile("+14155550103", 3), 2, "+14155550102")
    except RecordConflict:
        pass
    else:
        raise AssertionError("Expected conditional profile conflict")


def test_note_keys_are_client_scoped_and_delete_respects_legal_hold() -> None:
    client = RecordingClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    first = repository._note_key("business-1", "a", "note-1")
    second = repository._note_key("business-1", "a#b", "note-1")
    assert first["SK"] != second["SK"]
    assert not second["SK"]["S"].startswith(first["SK"]["S"])
    note = ClientNote("business-1", "a", "note-1", None, "Bring supplies",
                      "owner-1", NOW)
    repository.put_note(note)
    assert client.transactions[0]["TransactItems"][0]["Put"]["ConditionExpression"] == (
        "attribute_not_exists(PK)")
    repository.delete_note(note)
    condition = client.transactions[1]["TransactItems"][0]["Delete"]["ConditionExpression"]
    assert "attribute_not_exists(legal_hold_reason)" in condition
    repository.delete_note(note, expected_revision=7)
    guarded = client.transactions[2]["TransactItems"]
    assert guarded[0]["ConditionCheck"]["Key"]["SK"] == {"S": "CALENDAR#REVISION"}
    assert guarded[0]["ConditionCheck"]["ExpressionAttributeValues"] == {
        ":expected": {"N": "7"}}
    assert "Delete" in guarded[1]


def test_legal_hold_updates_are_conditional() -> None:
    client = RecordingClient()
    repository = DynamoDBCalendarRepository(client, "scheduling")
    note = ClientNote("business-1", "client-1", "note-1", None, "Bring supplies",
                      "owner-1", NOW)
    held = replace(note, legal_hold_reason="documented case")
    repository.update_note_hold(note, held)
    assert "attribute_not_exists(legal_hold_reason)" in client.updates[0]["ConditionExpression"]
    assert client.updates[0]["UpdateExpression"] == "SET legal_hold_reason = :reason"
    repository.update_note_hold(held, note)
    assert "legal_hold_reason = :previous_reason" in client.updates[1]["ConditionExpression"]
    assert client.updates[1]["UpdateExpression"] == "REMOVE legal_hold_reason"
    client.reject_update = True
    try:
        repository.update_note_hold(note, held)
    except RecordConflict:
        pass
    else:
        raise AssertionError("Expected conditional legal hold conflict")


def test_last_visit_queries_client_partition_strongly() -> None:
    client = RecordingClient()
    client.query_pages = [{"Items": [{"end_at": {"S": "2026-09-01T10:00:00+00:00"}}]}]
    repository = DynamoDBCalendarRepository(client, "scheduling")
    assert repository.last_visit_end("business-1", "client-1", NOW) == datetime(
        2026, 9, 1, 10, tzinfo=UTC)
    query = client.queries[0]
    assert query["ConsistentRead"] is True
    assert query["Limit"] == 1
    assert query["ScanIndexForward"] is False
    assert query["ExpressionAttributeValues"][":pk"]["S"] == repository._visit_partition(
        "business-1", "client-1")
    assert query["ExpressionAttributeValues"][":end"]["S"].startswith(
        "END#2026-09-28T15:00:00")
    assert not client.scans
    assert repository.last_visit_end("business-1", "never-visited", NOW) is None
