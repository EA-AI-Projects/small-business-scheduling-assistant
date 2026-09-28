"""DynamoDB client and note writes keep unique identity and scoped keys."""

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
        self.reject = False

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def query(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Items": []}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.scans.append(kwargs)
        return self.scan_pages.pop(0)

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transactions.append(kwargs)
        if self.reject:
            raise ClientError({"Error": {"Code": "TransactionCanceledException",
                                         "Message": "conditional failure"}}, "TransactWriteItems")
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


def test_last_visit_scans_strongly_and_paginates() -> None:
    client = RecordingClient()
    client.scan_pages = [
        {"Items": [{"end_at": {"S": "2026-08-01T10:00:00+00:00"}}],
         "LastEvaluatedKey": {"PK": {"S": "page-1"}}},
        {"Items": [{"end_at": {"S": "2026-09-01T10:00:00+00:00"}}]},
    ]
    repository = DynamoDBCalendarRepository(client, "scheduling")
    assert repository.last_visit_end("business-1", "client-1", NOW) == datetime(
        2026, 9, 1, 10, tzinfo=UTC)
    assert client.scans[0]["ConsistentRead"] is True
    assert client.scans[1]["ExclusiveStartKey"] == {"PK": {"S": "page-1"}}
