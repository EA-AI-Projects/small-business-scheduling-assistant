"""Appointment transitions; only trusted adapters may construct actor context."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment, ReplacementGuard
from scheduling.domain.availability import AvailabilityPolicy, available_starts
from scheduling.domain.calendar import CalendarSnapshot, CalendarStatus
from scheduling.domain.holds import (
    IdempotencyKeyReused,
    OutboxIntent,
    ReplacementPending,
    RevisionConflict,
    SlotConflict,
    TooManyConflicts,
)


class Action(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"
    EXPIRE = "expire"
    CANCEL = "cancel"
    EDIT = "edit_appointment"


class ActorRole(StrEnum):
    OWNER = "owner"
    CLIENT = "client"
    SYSTEM = "system"


class InvalidTransition(Exception):
    """The target is absent, ineligible, or not owned by this actor."""


class StaleVersion(Exception):
    """The caller's expected appointment version is no longer current."""


class HoldExpired(Exception):
    """A pending hold reached its expiry before owner approval."""


@dataclass(frozen=True)
class AppointmentCommand:
    business_id: str
    appointment_id: str
    actor_id: str
    actor_role: ActorRole
    operation: Action
    idempotency_key: str
    expected_version: int
    start_at: datetime | None = None
    duration_minutes: int | None = None

    def __post_init__(self) -> None:
        if not all((self.business_id, self.appointment_id, self.actor_id, self.idempotency_key)):
            raise ValueError("Command identity is required")
        if self.expected_version <= 0:
            raise ValueError("Expected version must be positive")
        if self.start_at is not None and self.start_at.tzinfo is None:
            raise ValueError("Edited start must be timezone-aware")
        if self.operation != Action.EDIT and (
            self.start_at is not None or self.duration_minutes is not None
        ):
            raise ValueError("Only appointment edits accept a new start or duration")

    def request_hash(self) -> str:
        payload = {
            "business_id": self.business_id,
            "appointment_id": self.appointment_id,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role.value,
            "operation": self.operation.value,
            "expected_version": self.expected_version,
            "start_at": self.start_at.astimezone(UTC).isoformat() if self.start_at else None,
            "duration_minutes": self.duration_minutes,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class TransitionResult:
    appointment: Appointment
    calendar_revision: int
    replaced_appointment: Appointment | None = None


@dataclass(frozen=True)
class TransitionRecord:
    request_hash: str
    result: TransitionResult


@dataclass(frozen=True)
class TransitionCommit:
    command: AppointmentCommand
    request_hash: str
    before: Appointment
    result: TransitionResult
    audit_id: str
    outbox: tuple[OutboxIntent, ...]
    decision_at: datetime
    clear_replacement_guard: bool


class LifecycleRepository(Protocol):
    def read_transition_idempotency(self, command: AppointmentCommand) -> TransitionRecord | None: ...

    def read_revision(self, business_id: str) -> int: ...

    def read_appointment(self, appointment_id: str) -> Appointment | None: ...

    def read_replacement_guard(
        self, business_id: str, original_id: str
    ) -> ReplacementGuard | None: ...

    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...

    def read_calendar_for_hold(
        self, business_id: str, start_at: datetime, end_at: datetime, policy: AvailabilityPolicy
    ) -> CalendarSnapshot: ...

    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...

    def commit_transition(self, expected_revision: int, commit: TransitionCommit) -> None: ...


class LifecycleService:
    def __init__(
        self,
        repository: LifecycleRepository,
        clock: Callable[[], datetime],
        max_attempts: int = 3,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("Retry budget must be positive")
        self._repository = repository
        self._clock = clock
        self._max_attempts = max_attempts

    def apply(self, command: AppointmentCommand) -> TransitionResult:
        request_hash = command.request_hash()
        existing = self._repository.read_transition_idempotency(command)
        if existing is not None:
            return self._replay(existing, request_hash)
        audit_id = f"{command.operation.value}#{uuid4()}"

        for _ in range(self._max_attempts):
            revision = self._repository.read_revision(command.business_id)
            before = self._repository.read_appointment(command.appointment_id)
            if before is None or before.business_id != command.business_id:
                raise InvalidTransition("Appointment was not found")
            self._authorize(command, before)
            now = self._now()
            if command.operation == Action.EXPIRE and before.status != CalendarStatus.PENDING_APPROVAL:
                return TransitionResult(before, revision)
            if before.version != command.expected_version:
                raise StaleVersion("Appointment version changed")

            original: Appointment | None = None
            after: Appointment
            if command.operation == Action.APPROVE:
                self._require_pending(before, now)
                original = self._replacement_original(before, now)
                try:
                    self._require_free(before, revision, now, original)
                except RevisionConflict:
                    continue
                after = replace(before, status=CalendarStatus.CONFIRMED, version=before.version + 1)
            elif command.operation == Action.DECLINE:
                self._require_pending(before, now)
                after = replace(before, status=CalendarStatus.DECLINED, version=before.version + 1)
            elif command.operation == Action.EXPIRE:
                if before.status != CalendarStatus.PENDING_APPROVAL:
                    return TransitionResult(before, revision)
                if before.hold_expires_at is None or before.hold_expires_at > now:
                    raise InvalidTransition("Hold is not due for expiry")
                after = replace(before, status=CalendarStatus.EXPIRED, version=before.version + 1)
            elif command.operation == Action.CANCEL:
                if before.status not in (CalendarStatus.CONFIRMED, CalendarStatus.PENDING_APPROVAL):
                    raise InvalidTransition("Only active appointments can be cancelled")
                if before.status == CalendarStatus.CONFIRMED:
                    guard = self._repository.read_replacement_guard(
                        before.business_id, before.appointment_id
                    )
                    if guard is not None and guard.expires_at > now:
                        raise ReplacementPending("Withdraw the active replacement first")
                after = replace(before, status=CalendarStatus.CANCELLED, version=before.version + 1)
            elif command.operation == Action.EDIT:
                if before.status != CalendarStatus.CONFIRMED:
                    raise InvalidTransition("Only confirmed appointments can be edited")
                start = command.start_at.astimezone(UTC) if command.start_at else before.start_at
                duration = (
                    command.duration_minutes
                    if command.duration_minutes is not None else before.duration_minutes
                )
                if start == before.start_at and duration == before.duration_minutes:
                    raise InvalidTransition("Edit must change time or duration")
                after = replace(
                    before,
                    start_at=start,
                    end_at=start + timedelta(minutes=duration),
                    duration_minutes=duration,
                    version=before.version + 1,
                )
                try:
                    self._require_free(after, revision, now, before)
                except RevisionConflict:
                    continue
            else:
                raise ValueError("Unsupported operation")

            replaced = (
                replace(original, status=CalendarStatus.CANCELLED, version=original.version + 1)
                if original is not None else None
            )
            result = TransitionResult(after, revision + 1, replaced)
            decision_at = self._now()
            if command.operation == Action.APPROVE and (
                before.hold_expires_at is None or before.hold_expires_at <= decision_at
            ):
                raise HoldExpired("Hold expired during approval preparation")
            if command.operation == Action.EXPIRE and (
                before.hold_expires_at is None or before.hold_expires_at > decision_at
            ):
                raise InvalidTransition("Hold is not due for expiry")
            clear_guard = False
            if before.replaces_appointment_id is not None:
                guard = self._repository.read_replacement_guard(
                    before.business_id, before.replaces_appointment_id
                )
                clear_guard = guard is not None and guard.replacement_id == before.appointment_id
            commit = TransitionCommit(
                command=command,
                request_hash=request_hash,
                before=before,
                result=result,
                audit_id=audit_id,
                outbox=self._outbox(command, before, result, audit_id),
                decision_at=decision_at,
                clear_replacement_guard=clear_guard,
            )
            try:
                self._repository.commit_transition(revision, commit)
                return result
            except RevisionConflict:
                existing = self._repository.read_transition_idempotency(command)
                if existing is not None:
                    return self._replay(existing, request_hash)
        raise TooManyConflicts("Calendar changed during every appointment attempt")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Clock must return timezone-aware instants")
        return now.astimezone(UTC)

    @staticmethod
    def _authorize(command: AppointmentCommand, before: Appointment) -> None:
        if command.operation in (Action.APPROVE, Action.DECLINE, Action.EDIT):
            if command.actor_role != ActorRole.OWNER:
                raise InvalidTransition("Owner authorization is required")
        elif command.operation == Action.EXPIRE:
            if command.actor_role != ActorRole.SYSTEM:
                raise InvalidTransition("System authorization is required")
        elif command.actor_role == ActorRole.CLIENT and command.actor_id != before.client_id:
            raise InvalidTransition("Client does not own this appointment")
        elif command.actor_role != ActorRole.OWNER and command.actor_role != ActorRole.CLIENT:
            raise InvalidTransition("Cancellation requires owner or client")

    @staticmethod
    def _require_pending(before: Appointment, now: datetime) -> None:
        if before.status != CalendarStatus.PENDING_APPROVAL:
            raise InvalidTransition("Request is not pending")
        if before.hold_expires_at is None or before.hold_expires_at <= now:
            raise HoldExpired("Hold expired before owner decision")

    def _replacement_original(self, before: Appointment, now: datetime) -> Appointment | None:
        original_id = before.replaces_appointment_id
        if original_id is None:
            return None
        original = self._repository.read_appointment(original_id)
        guard = self._repository.read_replacement_guard(before.business_id, original_id)
        if (
            original is None
            or original.business_id != before.business_id
            or original.client_id != before.client_id
            or original.status != CalendarStatus.CONFIRMED
            or guard is None
            or guard.replacement_id != before.appointment_id
            or guard.expires_at <= now
        ):
            raise InvalidTransition("Original confirmed appointment is no longer eligible")
        return original

    def _require_free(
        self,
        candidate: Appointment,
        revision: int,
        now: datetime,
        excluded: Appointment | None,
    ) -> None:
        policy = self._repository.read_policy(candidate.business_id)
        gap = max(policy.minimum_visit_gap_minutes, candidate.buffer_minutes)
        if gap > policy.maximum_buffer_minutes:
            raise InvalidTransition("Stored buffer exceeds the configured maximum")
        policy = replace(policy, minimum_visit_gap_minutes=gap)
        snapshot = self._repository.read_calendar_for_hold(
            candidate.business_id, candidate.start_at, candidate.end_at, policy
        )
        if snapshot.revision != revision:
            raise RevisionConflict("Calendar changed while validating appointment")
        excluded_ids = {candidate.appointment_id}
        if excluded is not None:
            excluded_ids.add(excluded.appointment_id)
        events = tuple(event for event in snapshot.events if event.event_id not in excluded_ids)
        local_day = candidate.start_at.astimezone(ZoneInfo(policy.timezone)).date()
        starts = available_starts(policy, local_day, candidate.duration_minutes, events, now)
        if candidate.start_at.astimezone(UTC) not in starts:
            full_snapshot = self._repository.read_calendar(candidate.business_id)
            if full_snapshot.revision != revision:
                raise RevisionConflict("Calendar changed while finding alternatives")
            all_events = tuple(
                event for event in full_snapshot.events if event.event_id not in excluded_ids
            )
            alternatives = available_starts(
                policy, local_day, candidate.duration_minutes, all_events, now
            )
            raise SlotConflict(alternatives[:5])

    @staticmethod
    def _outbox(
        command: AppointmentCommand,
        before: Appointment,
        result: TransitionResult,
        audit_id: str,
    ) -> tuple[OutboxIntent, ...]:
        template = command.operation.value
        if command.operation == Action.EXPIRE:
            return (OutboxIntent(f"{audit_id}#client", before.appointment_id, "client", template),)
        if command.operation == Action.CANCEL and command.actor_role == ActorRole.OWNER:
            return (OutboxIntent(f"{audit_id}#client", before.appointment_id, "client", template),)
        if result.replaced_appointment is not None:
            template = "replacement-approved"
        if before.replaces_appointment_id and command.operation in (Action.DECLINE, Action.CANCEL):
            template = "replacement-original-retained"
        return (
            OutboxIntent(f"{audit_id}#client", before.appointment_id, "client", template),
            OutboxIntent(f"{audit_id}#owner", before.appointment_id, "owner", template),
        )

    @staticmethod
    def _replay(record: TransitionRecord, request_hash: str) -> TransitionResult:
        if record.request_hash != request_hash:
            raise IdempotencyKeyReused("Key already used for a different appointment command")
        return record.result
