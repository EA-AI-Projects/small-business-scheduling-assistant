"""Strong-read and conditional-transaction adapter for the single-crew calendar."""

import json
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from typing import Any, Protocol

from scheduling.adapters.outbox_aws import due_keys
from scheduling.domain.appointments import Appointment, ReplacementGuard
from scheduling.domain.availability import AvailabilityPolicy, HolidayCalendar, LocalWindow
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot, CalendarStatus
from scheduling.domain.holds import (
    CreateHold,
    HoldCommit,
    IdempotencyRecord,
    PendingHold,
    RevisionConflict,
)
from scheduling.domain.lifecycle import (
    Action,
    AppointmentCommand,
    TransitionCommit,
    TransitionRecord,
    TransitionResult,
)
from scheduling.domain.outbox import DeliveryState
from scheduling.domain.owner_calendar import (
    OwnerCalendarCommand,
    OwnerCalendarCommit,
    OwnerCalendarReplay,
    OwnerCalendarResult,
    UnavailableBlock,
)
from scheduling.domain.owner_policy import (
    PolicyCommand,
    PolicyCommit,
    PolicyNotConfigured,
    PolicyRecord,
    PolicyReplay,
    PolicyResult,
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
        "replaces_appointment_id": hold.replaces_appointment_id,
    }


def _command_sort_key(actor_id: str, operation: str, idempotency_key: str) -> str:
    identity = json.dumps(
        [actor_id, operation, idempotency_key], separators=(",", ":")
    )
    return f"IDEMPOTENCY#{sha256(identity.encode()).hexdigest()}"


def _idempotency_sort_key(command: CreateHold) -> str:
    return _command_sort_key(command.actor_id, "create_hold", command.idempotency_key)


def _appointment_payload(appointment: Appointment) -> dict[str, Any]:
    return {
        "appointment_id": appointment.appointment_id,
        "business_id": appointment.business_id,
        "client_id": appointment.client_id,
        "start_at": _instant(appointment.start_at),
        "end_at": _instant(appointment.end_at),
        "status": appointment.status.value,
        "hold_expires_at": (
            _instant(appointment.hold_expires_at) if appointment.hold_expires_at else None
        ),
        "duration_minutes": appointment.duration_minutes,
        "buffer_minutes": appointment.buffer_minutes,
        "version": appointment.version,
        "replaces_appointment_id": appointment.replaces_appointment_id,
    }


def _appointment_from_payload(raw: dict[str, Any]) -> Appointment:
    return Appointment(
        appointment_id=raw["appointment_id"],
        business_id=raw["business_id"],
        client_id=raw["client_id"],
        start_at=datetime.fromisoformat(raw["start_at"]),
        end_at=datetime.fromisoformat(raw["end_at"]),
        status=CalendarStatus(raw["status"]),
        hold_expires_at=(
            datetime.fromisoformat(raw["hold_expires_at"])
            if raw["hold_expires_at"] else None
        ),
        duration_minutes=raw["duration_minutes"],
        buffer_minutes=raw["buffer_minutes"],
        version=raw["version"],
        replaces_appointment_id=raw["replaces_appointment_id"],
    )


def _block_payload(block: UnavailableBlock) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "business_id": block.business_id,
        "start_at": _instant(block.start_at),
        "end_at": _instant(block.end_at),
        "version": block.version,
    }


def _block_from_payload(raw: dict[str, Any]) -> UnavailableBlock:
    return UnavailableBlock(
        raw["block_id"], raw["business_id"],
        datetime.fromisoformat(raw["start_at"]), datetime.fromisoformat(raw["end_at"]),
        raw["version"],
    )


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
        record = self.read_policy_record(business_id)
        if record is None:
            raise PolicyNotConfigured("Persist the pilot policy before booking")
        return record.policy

    def read_policy_record(self, business_id: str) -> PolicyRecord | None:
        item = self._get(self._business_key(business_id, "POLICY#SCHEDULING"))
        if item is None:
            return None
        return PolicyRecord(
            self._decode_policy(item["payload"]["S"]),
            int(item.get("version", {"N": "1"})["N"]),
        )

    @staticmethod
    def _decode_policy(encoded: str) -> AvailabilityPolicy:
        payload = json.loads(encoded)

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

    def read_policy_replay(self, command: PolicyCommand) -> PolicyReplay | None:
        item = self._get(self._business_key(
            command.business_id,
            _command_sort_key(command.actor_id, command.operation, command.idempotency_key),
        ))
        if item is None:
            return None
        response = json.loads(item["response"]["S"])
        return PolicyReplay(
            item["request_hash"]["S"],
            PolicyResult(
                PolicyRecord(
                    self._decode_policy(response["policy"]), response["policy_version"]
                ),
                response["calendar_revision"],
            ),
        )

    def read_block(self, business_id: str, block_id: str) -> UnavailableBlock | None:
        item = self._get(self._business_key(business_id, f"BLOCK#{block_id}"))
        if item is None:
            return None
        return UnavailableBlock(
            block_id, business_id,
            datetime.fromisoformat(item["start_at"]["S"]),
            datetime.fromisoformat(item["end_at"]["S"]),
            int(item["version"]["N"]),
        )

    def read_owner_calendar_replay(
        self, command: OwnerCalendarCommand
    ) -> OwnerCalendarReplay | None:
        item = self._get(self._business_key(
            command.business_id,
            _command_sort_key(command.actor_id, command.operation.value, command.idempotency_key),
        ))
        if item is None:
            return None
        response = json.loads(item["response"]["S"])
        return OwnerCalendarReplay(
            item["request_hash"]["S"],
            OwnerCalendarResult(
                response["calendar_revision"],
                _block_from_payload(response["block"]) if response["block"] else None,
                _appointment_from_payload(response["appointment"])
                if response["appointment"] else None,
            ),
        )

    def commit_owner_calendar(self, expected_revision: int, commit: OwnerCalendarCommit) -> None:
        command = commit.command
        business_id = command.business_id
        before = commit.before_block
        block = commit.result.block
        appointment = commit.result.appointment
        revision_condition = (
            "attribute_not_exists(PK)" if expected_revision == 0 else "revision = :old"
        )
        revision_values: dict[str, Any] = {":new": {"N": str(expected_revision + 1)}}
        if expected_revision:
            revision_values[":old"] = {"N": str(expected_revision)}
        writes: list[dict[str, Any]] = [{"Update": {
            "TableName": self._table,
            "Key": self._business_key(business_id, "CALENDAR#REVISION"),
            "UpdateExpression": "SET revision = :new",
            "ConditionExpression": revision_condition,
            "ExpressionAttributeValues": revision_values,
        }}]

        def fresh_put(item: dict[str, Any]) -> dict[str, Any]:
            return {"Put": {
                "TableName": self._table, "Item": item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }}

        if block is not None:
            block_item = {
                **self._business_key(business_id, f"BLOCK#{block.block_id}"),
                "event_id": {"S": block.block_id},
                "start_at": {"S": _instant(block.start_at)},
                "end_at": {"S": _instant(block.end_at)},
                "status": {"S": CalendarStatus.UNAVAILABLE.value},
                "duration_minutes": {
                    "N": str(int((block.end_at - block.start_at).total_seconds() // 60))
                },
                "buffer_minutes": {"N": "0"},
                "version": {"N": str(block.version)},
            }
            if before is None:
                writes.append(fresh_put(block_item))
            else:
                writes.append({"Put": {
                    "TableName": self._table,
                    "Item": block_item,
                    "ConditionExpression": "#version = :old_version",
                    "ExpressionAttributeNames": {"#version": "version"},
                    "ExpressionAttributeValues": {
                        ":old_version": {"N": str(before.version)}
                    },
                }})
        elif before is not None:
            writes.append({"Delete": {
                "TableName": self._table,
                "Key": self._business_key(business_id, f"BLOCK#{before.block_id}"),
                "ConditionExpression": "#version = :old_version",
                "ExpressionAttributeNames": {"#version": "version"},
                "ExpressionAttributeValues": {":old_version": {"N": str(before.version)}},
            }})
        elif appointment is not None:
            writes.append(fresh_put(self._appointment_item(appointment)))
            writes.append(fresh_put(self._event_item(appointment)))
        else:
            raise ValueError("Owner calendar commit has no change")

        response = {
            "calendar_revision": commit.result.calendar_revision,
            "block": _block_payload(block) if block else None,
            "appointment": _appointment_payload(appointment) if appointment else None,
        }
        writes.append(fresh_put({
            **self._business_key(
                business_id,
                _command_sort_key(
                    command.actor_id, command.operation.value, command.idempotency_key
                ),
            ),
            "request_hash": {"S": commit.request_hash},
            "response": {"S": json.dumps(response, sort_keys=True)},
        }))
        writes.append(fresh_put({
            **self._business_key(business_id, f"AUDIT#{commit.audit_id}"),
            "action": {"S": command.operation.value},
            "actor_id": {"S": command.actor_id},
            "calendar_revision": {"N": str(commit.result.calendar_revision)},
        }))
        if block is not None:
            event_version = block.version
        elif appointment is not None:
            event_version = appointment.version
        else:
            assert before is not None
            event_version = before.version
        for intent in commit.outbox:
            writes.append(fresh_put({
                **self._business_key(business_id, f"OUTBOX#{intent.outbox_id}"),
                **due_keys(DeliveryState.PENDING, commit.decision_at, intent.outbox_id),
                "outbox_id": {"S": intent.outbox_id},
                "entity_id": {"S": intent.hold_id},
                "recipient": {"S": intent.recipient},
                "template": {"S": intent.template},
                "delivery_state": {"S": "PENDING"},
                "created_at": {"S": _instant(commit.decision_at)},
                "next_attempt_at": {"S": _instant(commit.decision_at)},
                "dispatch_after": {"S": _instant(commit.decision_at)},
                "attempts": {"N": "0"},
                "event_version": {"N": str(event_version)},
            }))
        if len(writes) > 100:
            raise ValueError("Owner calendar transaction exceeds DynamoDB item limit")
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as exc:
            error_response = getattr(exc, "response", {})
            if not isinstance(error_response, dict) or error_response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            reasons = error_response.get("CancellationReasons")
            if isinstance(reasons, list):
                codes = [reason.get("Code") for reason in reasons if isinstance(reason, dict)]
                if any(code not in ("None", "ConditionalCheckFailed", "TransactionConflict") for code in codes):
                    raise
            raise RevisionConflict("Owner calendar or idempotency condition changed") from exc

    def commit_policy(self, expected_revision: int, commit: PolicyCommit) -> None:
        command = commit.command
        old_version = command.expected_version
        revision_condition = (
            "attribute_not_exists(PK)" if expected_revision == 0 else "revision = :old"
        )
        revision_values: dict[str, Any] = {":new": {"N": str(expected_revision + 1)}}
        if expected_revision:
            revision_values[":old"] = {"N": str(expected_revision)}
        policy_condition = (
            "attribute_not_exists(PK)" if old_version is None else "#version = :old_version"
        )
        policy_item = {
            **self._business_key(command.business_id, "POLICY#SCHEDULING"),
            "payload": {"S": encode_policy(commit.result.record.policy)},
            "version": {"N": str(commit.result.record.version)},
        }
        policy_put: dict[str, Any] = {
            "TableName": self._table,
            "Item": policy_item,
            "ConditionExpression": policy_condition,
        }
        if old_version is not None:
            policy_put["ExpressionAttributeNames"] = {"#version": "version"}
            policy_put["ExpressionAttributeValues"] = {
                ":old_version": {"N": str(old_version)}
            }
        replay_payload = {
            "policy": encode_policy(commit.result.record.policy),
            "policy_version": commit.result.record.version,
            "calendar_revision": commit.result.calendar_revision,
        }
        outbox_id = f"{commit.audit_id}#owner"
        writes = [
            {"Update": {
                "TableName": self._table,
                "Key": self._business_key(command.business_id, "CALENDAR#REVISION"),
                "UpdateExpression": "SET revision = :new",
                "ConditionExpression": revision_condition,
                "ExpressionAttributeValues": revision_values,
            }},
            {"Put": policy_put},
            {"Put": {
                "TableName": self._table,
                "Item": {
                    **self._business_key(
                        command.business_id,
                        _command_sort_key(
                            command.actor_id, command.operation, command.idempotency_key
                        ),
                    ),
                    "request_hash": {"S": commit.request_hash},
                    "response": {"S": json.dumps(replay_payload, sort_keys=True)},
                },
                "ConditionExpression": "attribute_not_exists(PK)",
            }},
            {"Put": {
                "TableName": self._table,
                "Item": {
                    **self._business_key(command.business_id, f"AUDIT#{commit.audit_id}"),
                    "action": {"S": command.operation},
                    "actor_id": {"S": command.actor_id},
                    "policy_version": {"N": str(commit.result.record.version)},
                    "calendar_revision": {"N": str(commit.result.calendar_revision)},
                },
                "ConditionExpression": "attribute_not_exists(PK)",
            }},
            {"Put": {
                "TableName": self._table,
                "Item": {
                    **self._business_key(command.business_id, f"OUTBOX#{outbox_id}"),
                    **due_keys(DeliveryState.PENDING, commit.decision_at, outbox_id),
                    "outbox_id": {"S": outbox_id},
                    "entity_id": {"S": command.business_id},
                    "recipient": {"S": "owner"},
                    "template": {"S": command.operation},
                    "delivery_state": {"S": "PENDING"},
                    "created_at": {"S": _instant(commit.decision_at)},
                    "next_attempt_at": {"S": _instant(commit.decision_at)},
                    "dispatch_after": {"S": _instant(commit.decision_at)},
                    "attempts": {"N": "0"},
                    "event_version": {"N": str(commit.result.record.version)},
                },
                "ConditionExpression": "attribute_not_exists(PK)",
            }},
        ]
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as exc:
            response = getattr(exc, "response", {})
            if not isinstance(response, dict) or response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            reasons = response.get("CancellationReasons")
            if isinstance(reasons, list):
                codes = [reason.get("Code") for reason in reasons if isinstance(reason, dict)]
                if any(code not in ("None", "ConditionalCheckFailed", "TransactionConflict") for code in codes):
                    raise
            raise RevisionConflict("Policy or calendar revision changed") from exc

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
            replaces_appointment_id=raw.get("replaces_appointment_id"),
        )
        return IdempotencyRecord(item["request_hash"]["S"], response)

    def read_appointment(self, appointment_id: str) -> Appointment | None:
        item = self._get({"PK": {"S": f"APPOINTMENT#{appointment_id}"}, "SK": {"S": "META"}})
        if item is None:
            return None
        return Appointment(
            appointment_id=appointment_id,
            business_id=item["business_id"]["S"],
            client_id=item["client_id"]["S"],
            start_at=datetime.fromisoformat(item["start_at"]["S"]),
            end_at=datetime.fromisoformat(item["end_at"]["S"]),
            status=CalendarStatus(item["status"]["S"]),
            hold_expires_at=(
                datetime.fromisoformat(item["hold_expires_at"]["S"])
                if "hold_expires_at" in item else None
            ),
            duration_minutes=int(item["duration_minutes"]["N"]),
            buffer_minutes=int(item["buffer_minutes"]["N"]),
            version=int(item["version"]["N"]),
            replaces_appointment_id=(
                item["replaces_appointment_id"]["S"]
                if "replaces_appointment_id" in item else None
            ),
        )

    def read_pending_requests(self, business_id: str, now: datetime) -> tuple[Appointment, ...]:
        # Event projections are strongly read and paginated; metadata is reread
        # before presenting a request, so stale due-index entries cannot leak in.
        events = self._query(business_id, "PK = :pk AND begins_with(SK, :prefix)",
                             {":prefix": {"S": "EVENT#"}})
        pending: list[Appointment] = []
        for item in events:
            if item["status"]["S"] != CalendarStatus.PENDING_APPROVAL.value:
                continue
            appointment = self.read_appointment(item["event_id"]["S"])
            if (appointment is not None and appointment.business_id == business_id
                    and appointment.status == CalendarStatus.PENDING_APPROVAL
                    and appointment.hold_expires_at is not None
                    and appointment.hold_expires_at > now):
                pending.append(appointment)
        return tuple(sorted(pending, key=lambda appointment: appointment.start_at))

    def due_hold_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
        """Use the eventual index for wake-up only; callers strongly reread metadata."""
        if limit <= 0:
            raise ValueError("Due-hold query limit must be positive")
        ids: list[str] = []
        last_key: dict[str, Any] | None = None
        while len(ids) < limit:
            arguments: dict[str, Any] = {
                "TableName": self._table,
                "IndexName": "HoldDueIndex",
                "KeyConditionExpression": "hold_due_pk = :state AND hold_due_sk <= :due",
                "ExpressionAttributeValues": {
                    ":state": {"S": "HOLD#PENDING"},
                    ":due": {"S": f"{_instant(now)}#~"},
                },
                "Limit": limit - len(ids),
            }
            if last_key is not None:
                arguments["ExclusiveStartKey"] = last_key
            page = self._client.query(**arguments)
            ids.extend(
                item["PK"]["S"].removeprefix("APPOINTMENT#")
                for item in page.get("Items", ())
            )
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                break
        return tuple(ids)

    def read_replacement_guard(
        self, business_id: str, original_id: str
    ) -> ReplacementGuard | None:
        item = self._get(self._business_key(business_id, f"REPLACEMENT#{original_id}"))
        if item is None:
            return None
        return ReplacementGuard(
            original_id=original_id,
            replacement_id=item["replacement_id"]["S"],
            expires_at=datetime.fromisoformat(item["expires_at"]["S"]),
        )

    def read_transition_idempotency(
        self, command: AppointmentCommand
    ) -> TransitionRecord | None:
        item = self._get(self._business_key(
            command.business_id,
            _command_sort_key(
                command.actor_id, command.operation.value, command.idempotency_key
            ),
        ))
        if item is None:
            return None
        raw = json.loads(item["response"]["S"])
        result = TransitionResult(
            appointment=_appointment_from_payload(raw["appointment"]),
            calendar_revision=raw["calendar_revision"],
            replaced_appointment=(
                _appointment_from_payload(raw["replaced_appointment"])
                if raw["replaced_appointment"] is not None else None
            ),
        )
        return TransitionRecord(item["request_hash"]["S"], result)

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
            "hold_due_pk": {"S": "HOLD#PENDING"},
            "hold_due_sk": {"S": f"{_instant(hold.hold_expires_at)}#{hold.hold_id}"},
        }
        if hold.replaces_appointment_id is not None:
            metadata["replaces_appointment_id"] = {"S": hold.replaces_appointment_id}
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
                **due_keys(DeliveryState(intent.delivery_state), commit.created_at, intent.outbox_id),
                "outbox_id": {"S": intent.outbox_id},
                "hold_id": {"S": hold.hold_id},
                "recipient": {"S": intent.recipient},
                "template": {"S": intent.template},
                "delivery_state": {"S": intent.delivery_state},
                "created_at": {"S": _instant(commit.created_at)},
                "next_attempt_at": {"S": _instant(commit.created_at)},
                "dispatch_after": {"S": _instant(commit.created_at)},
                "attempts": {"N": "0"},
                "event_version": {"N": "1"},
            }) for intent in commit.outbox
        ]
        writes = [{"Update": revision_update}, put(metadata), put(event_item), put(idempotency), put(audit), *outbox]
        if hold.replaces_appointment_id is not None:
            writes.append({"Put": {
                "TableName": self._table,
                "Item": {
                    **self._business_key(
                        hold.business_id, f"REPLACEMENT#{hold.replaces_appointment_id}"
                    ),
                    "replacement_id": {"S": hold.hold_id},
                    "expires_at": {"S": _instant(hold.hold_expires_at)},
                },
                "ConditionExpression": "attribute_not_exists(PK) OR expires_at <= :now",
                "ExpressionAttributeValues": {":now": {"S": _instant(commit.created_at)}},
            }})
        if len(writes) > 100:
            raise ValueError("Hold transaction exceeds DynamoDB item limit")
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as exc:
            response = getattr(exc, "response", {})
            if not isinstance(response, dict) or response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            reasons = response.get("CancellationReasons")
            if isinstance(reasons, list):
                codes = [reason.get("Code") for reason in reasons if isinstance(reason, dict)]
                if any(code not in ("None", "ConditionalCheckFailed", "TransactionConflict") for code in codes):
                    raise
                if "TransactionConflict" in codes:
                    raise RevisionConflict("Calendar transaction conflicted") from exc
                guard_index = 7 if hold.replaces_appointment_id is not None else -1
                if any(
                    codes[index] == "ConditionalCheckFailed"
                    for index in (0, 3, guard_index)
                    if 0 <= index < len(codes)
                ):
                    raise RevisionConflict("Calendar or idempotency condition changed") from exc
                raise
            # Some clients omit per-item reasons. Verify the two expected
            # conditional guards rather than interpreting all cancellations as races.
            if (
                self.read_revision(hold.business_id) != expected_revision
                or self.read_idempotency(commit.command) is not None
                or (
                    hold.replaces_appointment_id is not None
                    and (guard := self.read_replacement_guard(
                        hold.business_id, hold.replaces_appointment_id
                    )) is not None
                    and guard.expires_at > commit.created_at
                )
            ):
                raise RevisionConflict("Calendar or idempotency condition changed") from exc
            raise

    @staticmethod
    def _appointment_item(appointment: Appointment) -> dict[str, Any]:
        item: dict[str, Any] = {
            "PK": {"S": f"APPOINTMENT#{appointment.appointment_id}"},
            "SK": {"S": "META"},
            "business_id": {"S": appointment.business_id},
            "client_id": {"S": appointment.client_id},
            "start_at": {"S": _instant(appointment.start_at)},
            "end_at": {"S": _instant(appointment.end_at)},
            "status": {"S": appointment.status.value},
            "duration_minutes": {"N": str(appointment.duration_minutes)},
            "buffer_minutes": {"N": str(appointment.buffer_minutes)},
            "version": {"N": str(appointment.version)},
        }
        if appointment.hold_expires_at is not None:
            item["hold_expires_at"] = {"S": _instant(appointment.hold_expires_at)}
        if appointment.replaces_appointment_id is not None:
            item["replaces_appointment_id"] = {"S": appointment.replaces_appointment_id}
        return item

    def _event_item(self, appointment: Appointment) -> dict[str, Any]:
        item: dict[str, Any] = {
            **self._business_key(
                appointment.business_id,
                f"EVENT#{_instant(appointment.start_at)}#{appointment.appointment_id}",
            ),
            "event_id": {"S": appointment.appointment_id},
            "start_at": {"S": _instant(appointment.start_at)},
            "end_at": {"S": _instant(appointment.end_at)},
            "status": {"S": appointment.status.value},
            "duration_minutes": {"N": str(appointment.duration_minutes)},
            "buffer_minutes": {"N": str(appointment.buffer_minutes)},
            "version": {"N": str(appointment.version)},
        }
        if appointment.hold_expires_at is not None:
            item["hold_expires_at"] = {"S": _instant(appointment.hold_expires_at)}
        return item

    def commit_transition(self, expected_revision: int, commit: TransitionCommit) -> None:
        before = commit.before
        after = commit.result.appointment
        command = commit.command
        revision_update = {
            "Update": {
                "TableName": self._table,
                "Key": self._business_key(before.business_id, "CALENDAR#REVISION"),
                "UpdateExpression": "SET revision = :new",
                "ConditionExpression": "revision = :old",
                "ExpressionAttributeValues": {
                    ":old": {"N": str(expected_revision)},
                    ":new": {"N": str(expected_revision + 1)},
                },
            }
        }

        def guarded_put(item: dict[str, Any], old: Appointment) -> dict[str, Any]:
            condition = "#status = :old_status AND #version = :old_version"
            values: dict[str, Any] = {
                ":old_status": {"S": old.status.value},
                ":old_version": {"N": str(old.version)},
            }
            if command.operation == Action.APPROVE and old.appointment_id == before.appointment_id:
                condition += " AND hold_expires_at > :decision_at"
                values[":decision_at"] = {"S": _instant(commit.decision_at)}
            if command.operation == Action.EXPIRE and old.appointment_id == before.appointment_id:
                condition += " AND hold_expires_at <= :decision_at"
                values[":decision_at"] = {"S": _instant(commit.decision_at)}
            return {"Put": {
                "TableName": self._table,
                "Item": item,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                "ExpressionAttributeValues": values,
            }}

        def fresh_put(item: dict[str, Any]) -> dict[str, Any]:
            return {"Put": {
                "TableName": self._table,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }}

        def event_delete(appointment: Appointment) -> dict[str, Any]:
            return {"Delete": {
                "TableName": self._table,
                "Key": self._business_key(
                    appointment.business_id,
                    f"EVENT#{_instant(appointment.start_at)}#{appointment.appointment_id}",
                ),
                "ConditionExpression": "#version = :old_version",
                "ExpressionAttributeNames": {"#version": "version"},
                "ExpressionAttributeValues": {":old_version": {"N": str(appointment.version)}},
            }}

        writes: list[dict[str, Any]] = [
            revision_update,
            guarded_put(self._appointment_item(after), before),
        ]
        same_event_key = before.start_at == after.start_at
        if after.status == CalendarStatus.CONFIRMED and same_event_key:
            writes.append(guarded_put(self._event_item(after), before))
        else:
            writes.append(event_delete(before))
            if after.status == CalendarStatus.CONFIRMED:
                writes.append(fresh_put(self._event_item(after)))

        replaced = commit.result.replaced_appointment
        if replaced is not None:
            original_before = self.read_appointment(replaced.appointment_id)
            if (
                original_before is None
                or original_before.status != CalendarStatus.CONFIRMED
                or original_before.version + 1 != replaced.version
            ):
                raise RevisionConflict("Replacement original changed")
            writes.extend((
                guarded_put(self._appointment_item(replaced), original_before),
                event_delete(original_before),
            ))

        if commit.clear_replacement_guard and before.replaces_appointment_id is not None:
            condition = "replacement_id = :replacement_id"
            values: dict[str, Any] = {":replacement_id": {"S": before.appointment_id}}
            if command.operation == Action.APPROVE:
                condition += " AND expires_at > :decision_at"
                values[":decision_at"] = {"S": _instant(commit.decision_at)}
            writes.append({"Delete": {
                "TableName": self._table,
                "Key": self._business_key(
                    before.business_id, f"REPLACEMENT#{before.replaces_appointment_id}"
                ),
                "ConditionExpression": condition,
                "ExpressionAttributeValues": values,
            }})

        result_payload = {
            "appointment": _appointment_payload(after),
            "calendar_revision": commit.result.calendar_revision,
            "replaced_appointment": (
                _appointment_payload(replaced) if replaced is not None else None
            ),
        }
        writes.append(fresh_put({
            **self._business_key(
                before.business_id,
                _command_sort_key(
                    command.actor_id, command.operation.value, command.idempotency_key
                ),
            ),
            "request_hash": {"S": commit.request_hash},
            "response": {"S": json.dumps(result_payload, sort_keys=True)},
        }))
        writes.append(fresh_put({
            **self._business_key(before.business_id, f"AUDIT#{commit.audit_id}"),
            "action": {"S": command.operation.value},
            "actor_id": {"S": command.actor_id},
            "appointment_id": {"S": before.appointment_id},
            "calendar_revision": {"N": str(commit.result.calendar_revision)},
        }))
        for intent in commit.outbox:
            writes.append(fresh_put({
                **self._business_key(before.business_id, f"OUTBOX#{intent.outbox_id}"),
                **due_keys(DeliveryState(intent.delivery_state), commit.decision_at, intent.outbox_id),
                "outbox_id": {"S": intent.outbox_id},
                "appointment_id": {"S": before.appointment_id},
                "recipient": {"S": intent.recipient},
                "template": {"S": intent.template},
                "delivery_state": {"S": intent.delivery_state},
                "created_at": {"S": _instant(commit.decision_at)},
                "next_attempt_at": {"S": _instant(commit.decision_at)},
                "dispatch_after": {"S": _instant(commit.decision_at)},
                "attempts": {"N": "0"},
                "event_version": {"N": str(after.version)},
            }))
        if len(writes) > 100:
            raise ValueError("Appointment transaction exceeds DynamoDB item limit")
        try:
            self._client.transact_write_items(TransactItems=writes)
        except Exception as exc:
            response = getattr(exc, "response", {})
            if not isinstance(response, dict) or response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            reasons = response.get("CancellationReasons")
            if isinstance(reasons, list):
                codes = [reason.get("Code") for reason in reasons if isinstance(reason, dict)]
                if any(code not in ("None", "ConditionalCheckFailed", "TransactionConflict") for code in codes):
                    raise
                if "TransactionConflict" in codes or "ConditionalCheckFailed" in codes:
                    raise RevisionConflict("Appointment transaction condition changed") from exc
                raise
            if (
                self.read_revision(before.business_id) != expected_revision
                or self.read_appointment(before.appointment_id) != before
                or self.read_transition_idempotency(command) is not None
            ):
                raise RevisionConflict("Appointment transaction condition changed") from exc
            raise
