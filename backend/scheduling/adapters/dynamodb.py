"""Strong-read and conditional-transaction adapter for the single-crew calendar."""

import json
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from typing import Any, Protocol

from scheduling.domain.availability import AvailabilityPolicy, HolidayCalendar, LocalWindow
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot, CalendarStatus
from scheduling.domain.holds import (
    CreateHold,
    HoldCommit,
    IdempotencyRecord,
    PendingHold,
    RevisionConflict,
)


class DynamoClient(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def query(self, **kwargs: Any) -> dict[str, Any]: ...

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]: ...


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _hold_payload(hold: PendingHold) -> dict[str, Any]:
    return {
        "hold_id": hold.hold_id,
        "business_id": hold.business_id,
        "client_id": hold.client_id,
        "start_at": _instant(hold.start_at),
        "end_at": _instant(hold.end_at),
        "hold_expires_at": _instant(hold.hold_expires_at),
        "duration_minutes": hold.duration_minutes,
        "buffer_minutes": hold.buffer_minutes,
        "calendar_revision": hold.calendar_revision,
    }


def _idempotency_sort_key(command: CreateHold) -> str:
    identity = json.dumps(
        [command.actor_id, "create_hold", command.idempotency_key], separators=(",", ":")
    )
    return f"IDEMPOTENCY#{sha256(identity.encode()).hexdigest()}"


def encode_policy(policy: AvailabilityPolicy) -> str:
    """Encode the persisted POLICY#SCHEDULING payload used by strong reads."""
    def windows(values: tuple[LocalWindow, ...]) -> list[dict[str, str]]:
        return [{"opens": value.opens.isoformat(), "closes": value.closes.isoformat()} for value in values]

    return json.dumps(
        {
            "timezone": policy.timezone,
            "weekly_windows": {str(day): windows(values) for day, values in policy.weekly_windows.items()},
            "booking_horizon_days": policy.booking_horizon_days,
            "slot_increment_minutes": policy.slot_increment_minutes,
            "maximum_visit_minutes": policy.maximum_visit_minutes,
            "minimum_visit_gap_minutes": policy.minimum_visit_gap_minutes,
            "opening_buffer_minutes": policy.opening_buffer_minutes,
            "closing_buffer_minutes": policy.closing_buffer_minutes,
            "hold_minutes": policy.hold_minutes,
            "maximum_buffer_minutes": policy.maximum_buffer_minutes,
            "date_exceptions": {day.isoformat(): windows(values) for day, values in policy.date_exceptions.items()},
            "holiday_calendar": policy.holiday_calendar.value if policy.holiday_calendar else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class DynamoDBCalendarRepository:
    """The caller supplies an IAM-scoped boto3 DynamoDB client and table name."""

    def __init__(self, client: DynamoClient, table_name: str) -> None:
        self._client = client
        self._table = table_name

    @staticmethod
    def _business_key(business_id: str, sort_key: str) -> dict[str, dict[str, str]]:
        return {"PK": {"S": f"BUSINESS#{business_id}"}, "SK": {"S": sort_key}}

    def _get(self, key: dict[str, dict[str, str]]) -> dict[str, Any] | None:
        return self._client.get_item(
            TableName=self._table, Key=key, ConsistentRead=True
        ).get("Item")

    def read_revision(self, business_id: str) -> int:
        item = self._get(self._business_key(business_id, "CALENDAR#REVISION"))
        return int(item["revision"]["N"]) if item else 0

    def read_policy(self, business_id: str) -> AvailabilityPolicy:
        item = self._get(self._business_key(business_id, "POLICY#SCHEDULING"))
        if item is None:
            raise ValueError("Persisted scheduling policy is required before booking")
        payload = json.loads(item["payload"]["S"])

        def windows(raw: list[dict[str, str]]) -> tuple[LocalWindow, ...]:
            return tuple(LocalWindow(time.fromisoformat(w["opens"]), time.fromisoformat(w["closes"])) for w in raw)

        return AvailabilityPolicy(
            timezone=payload["timezone"],
            weekly_windows={int(day): windows(values) for day, values in payload["weekly_windows"].items()},
            booking_horizon_days=payload["booking_horizon_days"],
            slot_increment_minutes=payload["slot_increment_minutes"],
            maximum_visit_minutes=payload["maximum_visit_minutes"],
            minimum_visit_gap_minutes=payload["minimum_visit_gap_minutes"],
            opening_buffer_minutes=payload["opening_buffer_minutes"],
            closing_buffer_minutes=payload["closing_buffer_minutes"],
            hold_minutes=payload["hold_minutes"],
            maximum_buffer_minutes=payload["maximum_buffer_minutes"],
            date_exceptions={
                date.fromisoformat(day): windows(values)
                for day, values in payload.get("date_exceptions", {}).items()
            },
            holiday_calendar=(
                HolidayCalendar(payload["holiday_calendar"])
                if payload.get("holiday_calendar") else None
            ),
        )

    def read_idempotency(self, command: CreateHold) -> IdempotencyRecord | None:
        item = self._get(self._business_key(command.business_id, _idempotency_sort_key(command)))
        if item is None:
            return None
        raw = json.loads(item["response"]["S"])
        response = PendingHold(
            hold_id=raw["hold_id"],
            business_id=raw["business_id"],
            client_id=raw["client_id"],
            start_at=datetime.fromisoformat(raw["start_at"]),
            end_at=datetime.fromisoformat(raw["end_at"]),
            hold_expires_at=datetime.fromisoformat(raw["hold_expires_at"]),
            duration_minutes=raw["duration_minutes"],
            buffer_minutes=raw["buffer_minutes"],
            calendar_revision=raw["calendar_revision"],
        )
        return IdempotencyRecord(item["request_hash"]["S"], response)

    def _query(self, business_id: str, expression: str, values: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        items: list[dict[str, Any]] = []
        last_key: dict[str, Any] | None = None
        while True:
            arguments: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": expression,
                "ExpressionAttributeValues": {":pk": {"S": f"BUSINESS#{business_id}"}, **values},
                "ConsistentRead": True,
            }
            if last_key is not None:
                arguments["ExclusiveStartKey"] = last_key
            page = self._client.query(**arguments)
            items.extend(page.get("Items", ()))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return tuple(items)

    @staticmethod
    def _event(item: dict[str, Any]) -> CalendarEvent:
        return CalendarEvent(
            event_id=item["event_id"]["S"],
            start_at=datetime.fromisoformat(item["start_at"]["S"]),
            end_at=datetime.fromisoformat(item["end_at"]["S"]),
            status=CalendarStatus(item["status"]["S"]),
            hold_expires_at=(
                datetime.fromisoformat(item["hold_expires_at"]["S"])
                if "hold_expires_at" in item else None
            ),
            duration_minutes=int(item["duration_minutes"]["N"]),
            buffer_minutes=int(item["buffer_minutes"]["N"]),
        )

    def read_calendar(self, business_id: str) -> CalendarSnapshot:
        revision = self.read_revision(business_id)
        events = self._query(business_id, "PK = :pk AND begins_with(SK, :prefix)", {":prefix": {"S": "EVENT#"}})
        blocks = self._query(business_id, "PK = :pk AND begins_with(SK, :prefix)", {":prefix": {"S": "BLOCK#"}})
        return CalendarSnapshot(business_id, revision, tuple(map(self._event, events + blocks)))

    def read_calendar_for_hold(
        self, business_id: str, start_at: datetime, end_at: datetime, policy: AvailabilityPolicy
    ) -> CalendarSnapshot:
        revision = self.read_revision(business_id)
        lookback = timedelta(minutes=policy.maximum_visit_minutes + policy.maximum_buffer_minutes)
        lookahead = timedelta(minutes=policy.maximum_buffer_minutes)
        lower = f"EVENT#{_instant(start_at - lookback)}"
        upper = f"EVENT#{_instant(end_at + lookahead)}"
        events = self._query(
            business_id, "PK = :pk AND SK BETWEEN :lower AND :upper",
            {":lower": {"S": lower}, ":upper": {"S": upper}},
        )
        blocks = self._query(business_id, "PK = :pk AND begins_with(SK, :prefix)", {":prefix": {"S": "BLOCK#"}})
        return CalendarSnapshot(business_id, revision, tuple(map(self._event, events + blocks)))

    def commit_hold(self, expected_revision: int, commit: HoldCommit) -> None:
        hold = commit.result
        business_key = self._business_key(hold.business_id, "CALENDAR#REVISION")
        revision_condition = (
            "attribute_not_exists(PK)" if expected_revision == 0 else "revision = :old"
        )
        revision_update = {
            "TableName": self._table,
            "Key": business_key,
            "UpdateExpression": "SET revision = :new",
            "ConditionExpression": revision_condition,
            "ExpressionAttributeValues": {
                ":new": {"N": str(expected_revision + 1)},
                **({":old": {"N": str(expected_revision)}} if expected_revision else {}),
            },
        }

        def put(item: dict[str, Any]) -> dict[str, Any]:
            return {"Put": {"TableName": self._table, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}}

        event_item: dict[str, Any] = {
            **self._business_key(hold.business_id, f"EVENT#{_instant(hold.start_at)}#{hold.hold_id}"),
            "event_id": {"S": hold.hold_id},
            "start_at": {"S": _instant(hold.start_at)},
            "end_at": {"S": _instant(hold.end_at)},
            "status": {"S": "PENDING_APPROVAL"},
            "hold_expires_at": {"S": _instant(hold.hold_expires_at)},
            "duration_minutes": {"N": str(hold.duration_minutes)},
            "buffer_minutes": {"N": str(hold.buffer_minutes)},
            "version": {"N": "1"},
        }
        metadata = {
            "PK": {"S": f"APPOINTMENT#{hold.hold_id}"}, "SK": {"S": "META"},
            "business_id": {"S": hold.business_id},
            "client_id": {"S": hold.client_id},
            "start_at": {"S": _instant(hold.start_at)},
            "end_at": {"S": _instant(hold.end_at)},
            "status": {"S": "PENDING_APPROVAL"},
            "hold_expires_at": {"S": _instant(hold.hold_expires_at)},
            "duration_minutes": {"N": str(hold.duration_minutes)},
            "buffer_minutes": {"N": str(hold.buffer_minutes)},
            "version": {"N": "1"},
        }
        idempotency = {
            **self._business_key(
                hold.business_id,
                _idempotency_sort_key(commit.command),
            ),
            "request_hash": {"S": commit.request_hash},
            "response": {"S": json.dumps(_hold_payload(hold), sort_keys=True)},
        }
        audit = {
            **self._business_key(hold.business_id, f"AUDIT#{commit.audit_id}"),
            "action": {"S": "create_hold"},
            "actor_id": {"S": commit.command.actor_id},
            "hold_id": {"S": hold.hold_id},
            "calendar_revision": {"N": str(hold.calendar_revision)},
        }
        outbox = [
            put({
                **self._business_key(hold.business_id, f"OUTBOX#{intent.outbox_id}"),
                "outbox_id": {"S": intent.outbox_id},
                "hold_id": {"S": hold.hold_id},
                "recipient": {"S": intent.recipient},
                "template": {"S": intent.template},
                "delivery_state": {"S": intent.delivery_state},
                "next_attempt_at": {"S": _instant(datetime.now(UTC))},
                "event_version": {"N": "1"},
            }) for intent in commit.outbox
        ]
        writes = [{"Update": revision_update}, put(metadata), put(event_item), put(idempotency), put(audit), *outbox]
        if len(writes) > 100:
            raise ValueError("Hold transaction exceeds DynamoDB item limit")
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as exc:
            response = getattr(exc, "response", {})
            if isinstance(response, dict) and response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise RevisionConflict("Calendar or idempotency condition changed") from exc
            raise
