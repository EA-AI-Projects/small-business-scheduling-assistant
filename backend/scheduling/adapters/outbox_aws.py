"""DynamoDB outbox ownership and SQS handoff; no SMS provider dependency."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Protocol

from scheduling.domain.outbox import (
    DeliveryState,
    OutboxConflict,
    OutboxRecord,
    new_lease_token,
)


class DynamoOutboxClient(Protocol):
    def query(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...


class SQSClient(Protocol):
    def send_message(self, **kwargs: Any) -> dict[str, Any]: ...


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def due_keys(state: DeliveryState, due_at: datetime, outbox_id: str) -> dict[str, Any]:
    return {
        "outbox_due_pk": {"S": f"OUTBOX#{state.value}"},
        "outbox_due_sk": {"S": f"{_instant(due_at)}#{outbox_id}"},
    }


def _conditional_call(client: DynamoOutboxClient, **kwargs: Any) -> dict[str, Any]:
    try:
        return client.update_item(**kwargs)
    except Exception as exc:
        response = getattr(exc, "response", {})
        if isinstance(response, dict) and response.get("Error", {}).get("Code") == (
            "ConditionalCheckFailedException"
        ):
            raise OutboxConflict("Outbox state changed") from exc
        raise


class DynamoOutboxStore:
    """The due GSI wakes work; every claim uses the authoritative base item."""

    def __init__(
        self, client: DynamoOutboxClient, table_name: str,
        due_index: str = "OutboxDueIndex",
    ) -> None:
        self._client = client
        self._table = table_name
        self._due_index = due_index

    @staticmethod
    def _key(business_id: str, outbox_id: str) -> dict[str, Any]:
        return {
            "PK": {"S": f"BUSINESS#{business_id}"},
            "SK": {"S": f"OUTBOX#{outbox_id}"},
        }

    @staticmethod
    def _record(item: dict[str, Any]) -> OutboxRecord:
        return OutboxRecord(
            business_id=item["PK"]["S"].removeprefix("BUSINESS#"),
            outbox_id=item["outbox_id"]["S"],
            entity_id=item["hold_id"]["S"] if "hold_id" in item else item["appointment_id"]["S"],
            recipient=item["recipient"]["S"],
            template=item["template"]["S"],
            event_version=int(item["event_version"]["N"]),
            state=DeliveryState(item["delivery_state"]["S"]),
            created_at=datetime.fromisoformat(item["created_at"]["S"]),
            next_attempt_at=(
                datetime.fromisoformat(item["next_attempt_at"]["S"])
                if "next_attempt_at" in item else None
            ),
            dispatch_after=(
                datetime.fromisoformat(item["dispatch_after"]["S"])
                if "dispatch_after" in item else None
            ),
            attempts=int(item.get("attempts", {"N": "0"})["N"]),
            lease_token=item["lease_token"]["S"] if "lease_token" in item else None,
            provider_id=item["provider_id"]["S"] if "provider_id" in item else None,
        )

    def due(self, now: datetime, limit: int) -> Iterator[tuple[str, str]]:
        states = (DeliveryState.PENDING, DeliveryState.RETRYABLE, DeliveryState.SENDING)
        # Rotate and bound each state so a burst of new intents cannot starve retries.
        offset = int(now.timestamp() // 60) % len(states)
        rotated = states[offset:] + states[:offset]
        per_state = max(1, (limit + len(states) - 1) // len(states))
        total = 0
        for state in rotated:
            returned = 0
            last_key: dict[str, Any] | None = None
            quota = min(per_state, limit - total)
            while returned < quota:
                arguments: dict[str, Any] = {
                    "TableName": self._table,
                    "IndexName": self._due_index,
                    "KeyConditionExpression": "outbox_due_pk = :state AND outbox_due_sk <= :due",
                    "ExpressionAttributeValues": {
                        ":state": {"S": f"OUTBOX#{state.value}"},
                        ":due": {"S": f"{_instant(now)}#~"},
                    },
                    "Limit": quota - returned,
                }
                if last_key is not None:
                    arguments["ExclusiveStartKey"] = last_key
                page = self._client.query(**arguments)
                for item in page.get("Items", ()):
                    returned += 1
                    total += 1
                    yield (
                        item["PK"]["S"].removeprefix("BUSINESS#"),
                        item["SK"]["S"].removeprefix("OUTBOX#"),
                    )
                last_key = page.get("LastEvaluatedKey")
                if not last_key:
                    break

    def get(self, business_id: str, outbox_id: str) -> OutboxRecord | None:
        item = self._client.get_item(
            TableName=self._table,
            Key=self._key(business_id, outbox_id),
            ConsistentRead=True,
        ).get("Item")
        return self._record(item) if item is not None else None

    def mark_enqueued(
        self, record: OutboxRecord, now: datetime, next_dispatch_at: datetime
    ) -> None:
        if record.dispatch_after is None or record.next_attempt_at is None:
            raise OutboxConflict("Outbox record is not due")
        _conditional_call(
            self._client,
            TableName=self._table,
            Key=self._key(record.business_id, record.outbox_id),
            UpdateExpression=(
                "SET dispatch_after = :next, outbox_due_sk = :due, "
                "last_enqueued_at = :now, enqueue_count = if_not_exists(enqueue_count, :zero) + :one"
            ),
            ConditionExpression=(
                "delivery_state = :state AND dispatch_after = :old_dispatch "
                "AND next_attempt_at = :old_attempt"
            ),
            ExpressionAttributeValues={
                ":next": {"S": _instant(next_dispatch_at)},
                ":due": due_keys(record.state, next_dispatch_at, record.outbox_id)["outbox_due_sk"],
                ":now": {"S": _instant(now)},
                ":zero": {"N": "0"},
                ":one": {"N": "1"},
                ":state": {"S": record.state.value},
                ":old_dispatch": {"S": _instant(record.dispatch_after)},
                ":old_attempt": {"S": _instant(record.next_attempt_at)},
            },
        )

    def claim(self, record: OutboxRecord, now: datetime, lease_until: datetime) -> OutboxRecord:
        if record.next_attempt_at is None:
            raise OutboxConflict("Outbox record is not claimable")
        token = new_lease_token()
        response = _conditional_call(
            self._client,
            TableName=self._table,
            Key=self._key(record.business_id, record.outbox_id),
            UpdateExpression=(
                "SET delivery_state = :sending, next_attempt_at = :lease, "
                "dispatch_after = :lease, outbox_due_pk = :due_pk, outbox_due_sk = :due_sk, "
                "lease_token = :token, attempts = if_not_exists(attempts, :zero) + :one"
            ),
            ConditionExpression=(
                "delivery_state = :old_state AND next_attempt_at = :old_attempt "
                "AND next_attempt_at <= :now"
            ),
            ExpressionAttributeValues={
                ":sending": {"S": DeliveryState.SENDING.value},
                ":lease": {"S": _instant(lease_until)},
                ":due_pk": due_keys(DeliveryState.SENDING, lease_until, record.outbox_id)["outbox_due_pk"],
                ":due_sk": due_keys(DeliveryState.SENDING, lease_until, record.outbox_id)["outbox_due_sk"],
                ":token": {"S": token},
                ":zero": {"N": "0"},
                ":one": {"N": "1"},
                ":old_state": {"S": record.state.value},
                ":old_attempt": {"S": _instant(record.next_attempt_at)},
                ":now": {"S": _instant(now)},
            },
            ReturnValues="ALL_NEW",
        )
        return self._record(response["Attributes"])

    def mark_sent(self, record: OutboxRecord, provider_id: str, now: datetime) -> None:
        self._finish(
            record,
            "SET delivery_state = :state, provider_id = :provider_id, delivered_at = :now "
            "REMOVE outbox_due_pk, outbox_due_sk, dispatch_after, next_attempt_at, lease_token",
            {
                ":state": {"S": DeliveryState.SENT.value},
                ":provider_id": {"S": provider_id},
                ":now": {"S": _instant(now)},
            },
        )

    def mark_failure(
        self, record: OutboxRecord, state: DeliveryState, due_at: datetime | None,
        error_code: str, now: datetime
    ) -> None:
        if state == DeliveryState.RETRYABLE and due_at is not None:
            due = due_keys(state, due_at, record.outbox_id)
            expression = (
                "SET delivery_state = :state, next_attempt_at = :due_at, "
                "dispatch_after = :due_at, outbox_due_pk = :due_pk, outbox_due_sk = :due_sk, "
                "last_error_code = :error, last_failed_at = :now REMOVE lease_token"
            )
            values = {
                ":state": {"S": state.value},
                ":due_at": {"S": _instant(due_at)},
                ":due_pk": due["outbox_due_pk"],
                ":due_sk": due["outbox_due_sk"],
                ":error": {"S": error_code},
                ":now": {"S": _instant(now)},
            }
        elif state == DeliveryState.FAILED and due_at is None:
            expression = (
                "SET delivery_state = :state, last_error_code = :error, last_failed_at = :now "
                "REMOVE outbox_due_pk, outbox_due_sk, dispatch_after, next_attempt_at, lease_token"
            )
            values = {
                ":state": {"S": state.value},
                ":error": {"S": error_code},
                ":now": {"S": _instant(now)},
            }
        else:
            raise ValueError("Failure must be retryable with a due time or terminal")
        self._finish(record, expression, values)

    def _finish(
        self, record: OutboxRecord, expression: str, values: dict[str, Any]
    ) -> None:
        if record.lease_token is None:
            raise OutboxConflict("Delivery has no lease")
        _conditional_call(
            self._client,
            TableName=self._table,
            Key=self._key(record.business_id, record.outbox_id),
            UpdateExpression=expression,
            ConditionExpression="delivery_state = :sending AND lease_token = :token",
            ExpressionAttributeValues={
                **values,
                ":sending": {"S": DeliveryState.SENDING.value},
                ":token": {"S": record.lease_token},
            },
        )


class SQSIntentQueue:
    def __init__(self, client: SQSClient, queue_url: str) -> None:
        self._client = client
        self._queue_url = queue_url

    def enqueue(self, business_id: str, outbox_id: str) -> None:
        self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps(
                {"business_id": business_id, "outbox_id": outbox_id},
                sort_keys=True, separators=(",", ":"),
            ),
        )
