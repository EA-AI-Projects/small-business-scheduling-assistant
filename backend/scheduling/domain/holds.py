"""Atomic pending hold command; transport adapters must supply verified actor identity."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from scheduling.domain.availability import AvailabilityPolicy, available_starts
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot, CalendarStatus


class RevisionConflict(Exception):
    """A calendar write advanced the revision during this command."""


class IdempotencyKeyReused(Exception):
    """An actor used one key for two different commands."""


class SlotConflict(Exception):
    def __init__(self, alternatives: tuple[datetime, ...]) -> None:
        self.alternatives = alternatives
        super().__init__("Requested slot is no longer available")


class TooManyConflicts(Exception):
    """The bounded revision retry budget was exhausted."""


@dataclass(frozen=True)
class CreateHold:
    business_id: str
    actor_id: str
    client_id: str
    idempotency_key: str
    start_at: datetime
    duration_minutes: int

    def __post_init__(self) -> None:
        if not all((self.business_id, self.actor_id, self.client_id, self.idempotency_key)):
            raise ValueError("Hold identity fields are required")
        if self.start_at.tzinfo is None:
            raise ValueError("Start must be timezone-aware")

    def request_hash(self) -> str:
        canonical = json.dumps(
            {
                "business_id": self.business_id,
                "actor_id": self.actor_id,
                "client_id": self.client_id,
                "start_at": self.start_at.astimezone(UTC).isoformat(),
                "duration_minutes": self.duration_minutes,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class PendingHold:
    hold_id: str
    business_id: str
    client_id: str
    start_at: datetime
    end_at: datetime
    hold_expires_at: datetime
    duration_minutes: int
    buffer_minutes: int
    calendar_revision: int

    def calendar_event(self) -> CalendarEvent:
        return CalendarEvent(
            event_id=self.hold_id,
            start_at=self.start_at,
            end_at=self.end_at,
            status=CalendarStatus.PENDING_APPROVAL,
            hold_expires_at=self.hold_expires_at,
            duration_minutes=self.duration_minutes,
            buffer_minutes=self.buffer_minutes,
        )


@dataclass(frozen=True)
class IdempotencyRecord:
    request_hash: str
    response: PendingHold


@dataclass(frozen=True)
class OutboxIntent:
    outbox_id: str
    hold_id: str
    recipient: str
    template: str
    delivery_state: str = "PENDING"


@dataclass(frozen=True)
class HoldCommit:
    command: CreateHold
    request_hash: str
    result: PendingHold
    audit_id: str
    outbox: tuple[OutboxIntent, ...]


class HoldRepository(Protocol):
    def read_idempotency(self, command: CreateHold) -> IdempotencyRecord | None: ...

    def read_revision(self, business_id: str) -> int: ...

    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...

    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...

    def read_calendar_for_hold(
        self, business_id: str, start_at: datetime, end_at: datetime, policy: AvailabilityPolicy
    ) -> CalendarSnapshot: ...

    def commit_hold(self, expected_revision: int, commit: HoldCommit) -> None: ...


class HoldService:
    def __init__(self, repository: HoldRepository, max_attempts: int = 3) -> None:
        if max_attempts <= 0:
            raise ValueError("Retry budget must be positive")
        self._repository = repository
        self._max_attempts = max_attempts

    def create(self, command: CreateHold, now: datetime) -> PendingHold:
        if now.tzinfo is None:
            raise ValueError("Current time must be timezone-aware")
        request_hash = command.request_hash()
        existing = self._repository.read_idempotency(command)
        if existing is not None:
            return self._replay(existing, request_hash)

        hold_id = str(uuid4())
        for _ in range(self._max_attempts):
            # Revision is intentionally read before the policy and event snapshot.
            revision = self._repository.read_revision(command.business_id)
            policy = self._repository.read_policy(command.business_id)
            snapshot = self._repository.read_calendar_for_hold(
                command.business_id,
                command.start_at,
                command.start_at + timedelta(minutes=command.duration_minutes),
                policy,
            )
            if snapshot.revision != revision:
                continue
            local_day = command.start_at.astimezone(ZoneInfo(policy.timezone)).date()
            starts = available_starts(
                policy, local_day, command.duration_minutes, snapshot.events, now
            )
            start = command.start_at.astimezone(UTC)
            if start not in starts:
                # The bounded candidate query is enough to validate this slot,
                # but suggestions need a full-day snapshot of other visits.
                full_snapshot = self._repository.read_calendar(command.business_id)
                if full_snapshot.revision != revision:
                    continue
                alternatives = available_starts(
                    policy, local_day, command.duration_minutes, full_snapshot.events, now
                )
                raise SlotConflict(alternatives[:5])
            result = PendingHold(
                hold_id=hold_id,
                business_id=command.business_id,
                client_id=command.client_id,
                start_at=start,
                end_at=start + timedelta(minutes=command.duration_minutes),
                hold_expires_at=now.astimezone(UTC) + timedelta(minutes=policy.hold_minutes),
                duration_minutes=command.duration_minutes,
                buffer_minutes=policy.minimum_visit_gap_minutes,
                calendar_revision=revision + 1,
            )
            commit = HoldCommit(
                command=command,
                request_hash=request_hash,
                result=result,
                audit_id=f"hold-created#{hold_id}",
                outbox=(
                    OutboxIntent(f"{hold_id}#owner", hold_id, "owner", "hold-request"),
                    OutboxIntent(f"{hold_id}#client", hold_id, "client", "hold-pending"),
                ),
            )
            try:
                self._repository.commit_hold(revision, commit)
                return result
            except RevisionConflict:
                existing = self._repository.read_idempotency(command)
                if existing is not None:
                    return self._replay(existing, request_hash)
        raise TooManyConflicts("Calendar changed during every hold attempt")

    @staticmethod
    def _replay(record: IdempotencyRecord, request_hash: str) -> PendingHold:
        if record.request_hash != request_hash:
            raise IdempotencyKeyReused("Key already used for a different hold")
        return record.response
