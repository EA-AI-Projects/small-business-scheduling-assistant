"""DynamoDB lifecycle writes keep appointment, calendar, and notices in one transaction."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from scheduling.adapters.dynamodb import DynamoDBCalendarRepository
from scheduling.domain.appointments import Appointment
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.holds import OutboxIntent
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    TransitionCommit,
    TransitionResult,
)

START = datetime(2026, 9, 29, 16, tzinfo=UTC)
NOW = datetime(2026, 9, 28, 16, tzinfo=UTC)


class RecordingClient:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        item = self.items.get((key["PK"]["S"], key["SK"]["S"]))
        return {"Item": item} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return {"Items": []}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transactions.append(kwargs)
        return {}


def pending() -> Appointment:
    return Appointment(
        "appointment-1", "business-1", "client-1", START, START + timedelta(hours=1),
        CalendarStatus.PENDING_APPROVAL, NOW + timedelta(hours=24), 60, 30, 1,
    )


def transition(
    before: Appointment,
    action: Action,
    after: Appointment,
    replaced: Appointment | None = None,
    clear_guard: bool = False,
) -> TransitionCommit:
    command = AppointmentCommand(
        before.business_id, before.appointment_id, "owner-1", ActorRole.OWNER,
        action, f"{action.value}-key", before.version,
    )
    result = TransitionResult(after, 8, replaced)
    return TransitionCommit(
        command, command.request_hash(), before, result, "audit-1",
        (OutboxIntent("notice-1", before.appointment_id, "client", action.value),),
        NOW, clear_guard,
    )


def test_approve_checks_expiry_and_writes_metadata_event_audit_and_outbox_atomically() -> None:
    client = RecordingClient()
    before = pending()
    after = replace(before, status=CalendarStatus.CONFIRMED, version=2)

    DynamoDBCalendarRepository(client, "scheduling").commit_transition(
        7, transition(before, Action.APPROVE, after)
    )

    writes = client.transactions[0]["TransactItems"]
    assert len(writes) == 6
    assert writes[0]["Update"]["ConditionExpression"] == "revision = :old"
    assert "hold_expires_at > :decision_at" in writes[1]["Put"]["ConditionExpression"]
    assert "hold_expires_at > :decision_at" in writes[2]["Put"]["ConditionExpression"]
    assert writes[1]["Put"]["Item"]["status"] == {"S": "CONFIRMED"}
    assert writes[2]["Put"]["Item"]["version"] == {"N": "2"}
    assert any(
        write.get("Put", {}).get("Item", {}).get("SK", {}).get("S", "").startswith("AUDIT#")
        for write in writes
    )
    assert any(
        write.get("Put", {}).get("Item", {}).get("SK", {}).get("S", "").startswith("OUTBOX#")
        for write in writes
    )


def test_decline_deletes_calendar_event_and_expiry_uses_opposite_time_condition() -> None:
    client = RecordingClient()
    before = pending()
    declined = replace(before, status=CalendarStatus.DECLINED, version=2)
    expired = replace(before, status=CalendarStatus.EXPIRED, version=2)
    repository = DynamoDBCalendarRepository(client, "scheduling")

    repository.commit_transition(7, transition(before, Action.DECLINE, declined))
    repository.commit_transition(7, transition(before, Action.EXPIRE, expired))

    decline_writes = client.transactions[0]["TransactItems"]
    expire_writes = client.transactions[1]["TransactItems"]
    assert "Delete" in decline_writes[2]
    assert "hold_expires_at <= :decision_at" in expire_writes[1]["Put"]["ConditionExpression"]
    assert "Delete" in expire_writes[2]


def test_replacement_approval_cancels_original_and_deletes_guard_in_one_transaction() -> None:
    client = RecordingClient()
    original = replace(
        pending(), appointment_id="original-1", status=CalendarStatus.CONFIRMED,
        version=2,
    )
    before = replace(pending(), replaces_appointment_id="original-1")
    after = replace(before, status=CalendarStatus.CONFIRMED, version=2)
    replaced = replace(original, status=CalendarStatus.CANCELLED, version=3)
    original_item = DynamoDBCalendarRepository._appointment_item(original)
    client.items[("APPOINTMENT#original-1", "META")] = original_item

    DynamoDBCalendarRepository(client, "scheduling").commit_transition(
        7, transition(before, Action.APPROVE, after, replaced, True)
    )

    writes = client.transactions[0]["TransactItems"]
    assert any(
        write.get("Put", {}).get("Item", {}).get("PK") == {"S": "APPOINTMENT#original-1"}
        and write["Put"]["Item"]["status"] == {"S": "CANCELLED"}
        for write in writes
    )
    assert any(
        write.get("Delete", {}).get("Key", {}).get("SK") == {"S": "REPLACEMENT#original-1"}
        for write in writes
    )
    assert sum("Delete" in write for write in writes) == 2
