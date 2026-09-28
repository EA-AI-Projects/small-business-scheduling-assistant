"""Owner-managed scheduling policy, guarded by the calendar revision."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from zoneinfo import ZoneInfo

from scheduling.domain.availability import (
    AvailabilityPolicy,
    LocalWindow,
    available_starts,
    pilot_policy,
)
from scheduling.domain.calendar import CalendarSnapshot, CalendarStatus
from scheduling.domain.holds import IdempotencyKeyReused, RevisionConflict


class PolicyConflict(Exception):
    """A policy edit would invalidate a future active reservation."""


class PolicyNotConfigured(Exception):
    """The owner must persist the pilot policy before editing it."""


@dataclass(frozen=True)
class PolicyRecord:
    policy: AvailabilityPolicy
    version: int


@dataclass(frozen=True)
class PolicyResult:
    record: PolicyRecord
    calendar_revision: int


@dataclass(frozen=True)
class PolicyCommand:
    business_id: str
    actor_id: str
    idempotency_key: str
    expected_revision: int
    expected_version: int | None
    policy: AvailabilityPolicy

    def __post_init__(self) -> None:
        if not all((self.business_id, self.actor_id, self.idempotency_key)):
            raise ValueError("Owner policy command identity is required")
        if self.expected_revision < 0 or (
            self.expected_version is not None and self.expected_version <= 0
        ):
            raise ValueError("Expected revision and version must be nonnegative")

    @property
    def operation(self) -> str:
        return "seed_policy" if self.expected_version is None else "edit_business_calendar"

    def request_hash(self) -> str:
        def windows(values: tuple[LocalWindow, ...]) -> list[tuple[str, str]]:
            return [(w.opens.isoformat(), w.closes.isoformat()) for w in values]

        policy = self.policy
        payload = {
            "business_id": self.business_id,
            "actor_id": self.actor_id,
            "expected_revision": self.expected_revision,
            "expected_version": self.expected_version,
            "policy": {
                "timezone": policy.timezone,
                "weekly_windows": {
                    str(day): windows(values) for day, values in policy.weekly_windows.items()
                },
                "date_exceptions": {
                    day.isoformat(): windows(values)
                    for day, values in policy.date_exceptions.items()
                },
                "booking_horizon_days": policy.booking_horizon_days,
                "slot_increment_minutes": policy.slot_increment_minutes,
                "maximum_visit_minutes": policy.maximum_visit_minutes,
                "minimum_visit_gap_minutes": policy.minimum_visit_gap_minutes,
                "opening_buffer_minutes": policy.opening_buffer_minutes,
                "closing_buffer_minutes": policy.closing_buffer_minutes,
                "hold_minutes": policy.hold_minutes,
                "maximum_buffer_minutes": policy.maximum_buffer_minutes,
                "holiday_calendar": policy.holiday_calendar,
            },
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PolicyCommit:
    command: PolicyCommand
    request_hash: str
    result: PolicyResult
    decision_at: datetime
    audit_id: str


@dataclass(frozen=True)
class PolicyReplay:
    request_hash: str
    result: PolicyResult


class PolicyRepository(Protocol):
    def read_revision(self, business_id: str) -> int: ...

    def read_policy_record(self, business_id: str) -> PolicyRecord | None: ...

    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...

    def read_policy_replay(self, command: PolicyCommand) -> PolicyReplay | None: ...

    def commit_policy(self, expected_revision: int, commit: PolicyCommit) -> None: ...


class OwnerPolicyService:
    def __init__(self, repository: PolicyRepository, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    def seed(self, business_id: str, actor_id: str, idempotency_key: str) -> PolicyResult:
        """Persist the confirmed pilot defaults once; production writes require this record."""
        return self.apply(PolicyCommand(
            business_id, actor_id, idempotency_key, 0, None, pilot_policy()
        ))

    def apply(self, command: PolicyCommand) -> PolicyResult:
        request_hash = command.request_hash()
        replay = self._repository.read_policy_replay(command)
        if replay is not None:
            return self._replay(replay, request_hash)
        try:
            return self._apply_new(command, request_hash)
        except RevisionConflict:
            replay = self._repository.read_policy_replay(command)
            if replay is not None:
                return self._replay(replay, request_hash)
            raise

    def _apply_new(self, command: PolicyCommand, request_hash: str) -> PolicyResult:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Current instant must be timezone-aware")
        now = now.astimezone(UTC)
        revision = self._repository.read_revision(command.business_id)
        if revision != command.expected_revision:
            raise RevisionConflict("Calendar revision changed")
        previous = self._repository.read_policy_record(command.business_id)
        if command.expected_version is None:
            if previous is not None:
                raise RevisionConflict("Scheduling policy is already configured")
            version = 1
        else:
            if previous is None:
                raise PolicyNotConfigured("Persist the pilot policy first")
            if previous.version != command.expected_version:
                raise RevisionConflict("Policy version changed")
            version = previous.version + 1
            snapshot = self._repository.read_calendar(command.business_id)
            if snapshot.revision != revision:
                raise RevisionConflict("Calendar changed during policy validation")
            self._validate_reservations(command.policy, snapshot, now)
        result = PolicyResult(PolicyRecord(command.policy, version), revision + 1)
        identity = json.dumps(
            [command.business_id, command.actor_id, command.operation, command.idempotency_key],
            separators=(",", ":"),
        )
        audit_id = f"{command.operation}#{sha256(identity.encode()).hexdigest()}"
        commit = PolicyCommit(command, request_hash, result, now, audit_id)
        self._repository.commit_policy(revision, commit)
        return result

    @staticmethod
    def _replay(replay: PolicyReplay, request_hash: str) -> PolicyResult:
        if replay.request_hash != request_hash:
            raise IdempotencyKeyReused("Key already used for another policy edit")
        return replay.result

    @staticmethod
    def _validate_reservations(
        policy: AvailabilityPolicy, snapshot: CalendarSnapshot, now: datetime
    ) -> None:
        zone = ZoneInfo(policy.timezone)
        for event in snapshot.events:
            if event.status not in (CalendarStatus.CONFIRMED, CalendarStatus.PENDING_APPROVAL):
                continue
            if not event.occupies_time(now) or event.end_at <= now:
                continue
            if (
                event.duration_minutes is None
                or event.duration_minutes > policy.maximum_visit_minutes
                or event.buffer_minutes > policy.maximum_buffer_minutes
            ):
                raise PolicyConflict("Policy cap would exclude an active reservation")
            if event.start_at <= now:
                # Hours/closure changes cannot alter an already-started visit,
                # but its snapshots must remain inside the query lookback caps.
                continue
            day = event.start_at.astimezone(zone).date()
            days_ahead = (day - now.astimezone(zone).date()).days
            check_policy = replace(
                policy,
                booking_horizon_days=max(policy.booking_horizon_days, days_ahead + 1),
                minimum_visit_gap_minutes=max(
                    policy.minimum_visit_gap_minutes, event.buffer_minutes
                ),
            )
            other_events = tuple(
                other for other in snapshot.events if other.event_id != event.event_id
            )
            starts = available_starts(
                check_policy, day, event.duration_minutes, other_events, now
            )
            if event.start_at not in starts:
                raise PolicyConflict("Policy edit would invalidate an active reservation")
