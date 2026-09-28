"""Owner calendar writes share the booking revision and conflict boundary."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import AvailabilityPolicy, available_starts
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot, CalendarStatus
from scheduling.domain.holds import (
    IdempotencyKeyReused,
    OutboxIntent,
    RevisionConflict,
    SlotConflict,
)


class OwnerAction(StrEnum):
    CREATE_BLOCK = "block_time"
    MOVE_BLOCK = "edit_block"
    REMOVE_BLOCK = "remove_block"
    CREATE_APPOINTMENT = "create_owner_appointment"


class BlockNotFound(Exception):
    """The targeted owner block does not exist."""


@dataclass(frozen=True)
class UnavailableBlock:
    block_id: str
    business_id: str
    start_at: datetime
    end_at: datetime
    version: int

    def calendar_event(self) -> CalendarEvent:
        return CalendarEvent(
            self.block_id, self.start_at, self.end_at, CalendarStatus.UNAVAILABLE,
            duration_minutes=int((self.end_at - self.start_at).total_seconds() // 60),
        )


@dataclass(frozen=True)
class OwnerCalendarCommand:
    business_id: str
    actor_id: str
    idempotency_key: str
    operation: OwnerAction
    expected_revision: int
    block_id: str | None = None
    expected_version: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    client_id: str | None = None
    duration_minutes: int | None = None

    def __post_init__(self) -> None:
        if not all((self.business_id, self.actor_id, self.idempotency_key)):
            raise ValueError("Owner command identity is required")
        if self.expected_revision < 0:
            raise ValueError("Expected calendar revision must be nonnegative")
        if self.operation in (OwnerAction.MOVE_BLOCK, OwnerAction.REMOVE_BLOCK):
            if not self.block_id or not self.expected_version or self.expected_version <= 0:
                raise ValueError("Block edit requires ID and positive expected version")
        elif self.block_id is not None or self.expected_version is not None:
            raise ValueError("New calendar entries cannot specify an existing ID or version")
        if self.operation in (OwnerAction.CREATE_BLOCK, OwnerAction.MOVE_BLOCK):
            if self.client_id is not None or self.duration_minutes is not None:
                raise ValueError("Block commands do not accept appointment fields")
            if self.start_at is None or self.end_at is None:
                raise ValueError("Block interval is required")
            if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
                raise ValueError("Block instants must be timezone-aware")
            if self.end_at <= self.start_at or (
                self.end_at - self.start_at
            ).total_seconds() % 60:
                raise ValueError("Block interval must have positive whole-minute duration")
        if self.operation == OwnerAction.CREATE_APPOINTMENT:
            if self.end_at is not None:
                raise ValueError("Manual appointment end is derived from duration")
            if not self.client_id or self.start_at is None or self.duration_minutes is None:
                raise ValueError("Manual appointment requires client, start and duration")
            if self.start_at.tzinfo is None or self.duration_minutes <= 0:
                raise ValueError("Manual appointment start and duration are invalid")
        if self.operation == OwnerAction.REMOVE_BLOCK and (
            self.start_at is not None or self.end_at is not None
            or self.client_id is not None or self.duration_minutes is not None
        ):
            raise ValueError("Removing a block does not accept a new interval")

    def request_hash(self) -> str:
        payload = {
            "business_id": self.business_id,
            "actor_id": self.actor_id,
            "operation": self.operation.value,
            "expected_revision": self.expected_revision,
            "block_id": self.block_id,
            "expected_version": self.expected_version,
            "start_at": self.start_at.astimezone(UTC).isoformat() if self.start_at else None,
            "end_at": self.end_at.astimezone(UTC).isoformat() if self.end_at else None,
            "client_id": self.client_id,
            "duration_minutes": self.duration_minutes,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class OwnerCalendarResult:
    calendar_revision: int
    block: UnavailableBlock | None = None
    appointment: Appointment | None = None


@dataclass(frozen=True)
class OwnerCalendarReplay:
    request_hash: str
    result: OwnerCalendarResult


@dataclass(frozen=True)
class OwnerCalendarCommit:
    command: OwnerCalendarCommand
    request_hash: str
    before_block: UnavailableBlock | None
    result: OwnerCalendarResult
    decision_at: datetime
    audit_id: str
    outbox: tuple[OutboxIntent, ...]


class OwnerCalendarRepository(Protocol):
    def read_revision(self, business_id: str) -> int: ...

    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...

    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...

    def read_calendar_for_hold(
        self, business_id: str, start_at: datetime, end_at: datetime, policy: AvailabilityPolicy
    ) -> CalendarSnapshot: ...

    def read_block(self, business_id: str, block_id: str) -> UnavailableBlock | None: ...

    def read_owner_calendar_replay(
        self, command: OwnerCalendarCommand
    ) -> OwnerCalendarReplay | None: ...

    def commit_owner_calendar(self, expected_revision: int, commit: OwnerCalendarCommit) -> None: ...


class OwnerCalendarService:
    def __init__(self, repository: OwnerCalendarRepository, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    def apply(self, command: OwnerCalendarCommand) -> OwnerCalendarResult:
        request_hash = command.request_hash()
        replay = self._repository.read_owner_calendar_replay(command)
        if replay is not None:
            return self._replay(replay, request_hash)
        try:
            return self._apply_new(command, request_hash)
        except (RevisionConflict, BlockNotFound):
            replay = self._repository.read_owner_calendar_replay(command)
            if replay is not None:
                return self._replay(replay, request_hash)
            raise

    def _apply_new(
        self, command: OwnerCalendarCommand, request_hash: str
    ) -> OwnerCalendarResult:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Current instant must be timezone-aware")
        now = now.astimezone(UTC)
        revision = self._repository.read_revision(command.business_id)
        if revision != command.expected_revision:
            raise RevisionConflict("Calendar revision changed")
        policy = self._repository.read_policy(command.business_id)
        before: UnavailableBlock | None = None
        block: UnavailableBlock | None = None
        appointment: Appointment | None = None
        if command.operation == OwnerAction.CREATE_APPOINTMENT:
            assert command.start_at is not None and command.duration_minutes is not None
            assert command.client_id is not None
            start = command.start_at.astimezone(UTC)
            end = start + timedelta(minutes=command.duration_minutes)
            snapshot = self._repository.read_calendar_for_hold(
                command.business_id, start, end, policy
            )
            if snapshot.revision != revision:
                raise RevisionConflict("Calendar changed during manual appointment validation")
            day = start.astimezone(ZoneInfo(policy.timezone)).date()
            if start not in available_starts(
                policy, day, command.duration_minutes, snapshot.events, now
            ):
                raise SlotConflict(())
            appointment = Appointment(
                str(uuid4()), command.business_id, command.client_id, start, end,
                CalendarStatus.CONFIRMED, None, command.duration_minutes,
                policy.minimum_visit_gap_minutes, 1,
            )
        else:
            if command.operation != OwnerAction.CREATE_BLOCK:
                assert command.block_id is not None
                before = self._repository.read_block(command.business_id, command.block_id)
                if before is None:
                    raise BlockNotFound("Owner block was not found")
                if before.version != command.expected_version:
                    raise RevisionConflict("Block version changed")
            if command.operation != OwnerAction.REMOVE_BLOCK:
                assert command.start_at is not None and command.end_at is not None
                block = UnavailableBlock(
                    before.block_id if before else str(uuid4()), command.business_id,
                    command.start_at.astimezone(UTC), command.end_at.astimezone(UTC),
                    before.version + 1 if before else 1,
                )
                snapshot = self._repository.read_calendar(command.business_id)
                if snapshot.revision != revision:
                    raise RevisionConflict("Calendar changed during block validation")
                for event in snapshot.events:
                    if event.event_id == block.block_id or not event.occupies_time(now):
                        continue
                    if block.start_at < event.end_at and event.start_at < block.end_at:
                        raise SlotConflict(())
        result = OwnerCalendarResult(revision + 1, block, appointment)
        identity = json.dumps(
            [command.business_id, command.actor_id, command.operation.value, command.idempotency_key],
            separators=(",", ":"),
        )
        audit_id = f"{command.operation.value}#{sha256(identity.encode()).hexdigest()}"
        recipients = ("client", "owner") if appointment else ("owner",)
        if appointment is not None:
            target_id = appointment.appointment_id
        elif block is not None:
            target_id = block.block_id
        else:
            assert before is not None
            target_id = before.block_id
        outbox = tuple(
            OutboxIntent(
                f"{audit_id}#{recipient}", target_id, recipient, command.operation.value
            )
            for recipient in recipients
        )
        commit = OwnerCalendarCommit(command, request_hash, before, result, now, audit_id, outbox)
        self._repository.commit_owner_calendar(revision, commit)
        return result

    @staticmethod
    def _replay(replay: OwnerCalendarReplay, request_hash: str) -> OwnerCalendarResult:
        if replay.request_hash != request_hash:
            raise IdempotencyKeyReused("Key already used for another owner calendar command")
        return replay.result
