"""Atomic pending hold command; transport adapters must supply verified actor identity."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment, ReplacementGuard
from scheduling.domain.availability import AvailabilityPolicy, available_starts
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot, CalendarStatus

# Owner notification for a request a client created by accepting an owner counteroffer.
COUNTEROFFER_REQUEST_TEMPLATE = "counteroffer-request"


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


class InvalidReplacement(Exception):
    """The original is not a confirmed visit owned by this client."""


class ReplacementPending(Exception):
    """This original already has an active replacement request."""


@dataclass(frozen=True)
class CreateHold:
    business_id: str
    actor_id: str
    client_id: str
    idempotency_key: str
    start_at: datetime
    duration_minutes: int
    replaces_appointment_id: str | None = None
    # Set only when the original is the client's still-pending request that an owner
    # counteroffer replaces: it must be that pending request at exactly this version.
    # Unset keeps the rule that a replacement targets a confirmed visit.
    replaces_pending_version: int | None = None
    # Set by the portal for a confirmed original: the replacement is refused when the
    # original is no longer at the version the client was shown (an edit or other change).
    replaces_confirmed_version: int | None = None

    def __post_init__(self) -> None:
        if not all((self.business_id, self.actor_id, self.client_id, self.idempotency_key)):
            raise ValueError("Hold identity fields are required")
        if self.start_at.tzinfo is None:
            raise ValueError("Start must be timezone-aware")
        if self.replaces_pending_version is not None and (
                self.replaces_appointment_id is None or self.replaces_pending_version <= 0):
            raise ValueError("A pending replacement names its original and version")
        if self.replaces_confirmed_version is not None and (
                self.replaces_appointment_id is None or self.replaces_confirmed_version <= 0
                or self.replaces_pending_version is not None):
            raise ValueError("A confirmed replacement names its original and version")

    def request_hash(self) -> str:
        payload: dict[str, object] = {
            "business_id": self.business_id,
            "actor_id": self.actor_id,
            "client_id": self.client_id,
            "start_at": self.start_at.astimezone(UTC).isoformat(),
            "duration_minutes": self.duration_minutes,
            "replaces_appointment_id": self.replaces_appointment_id,
        }
        if self.replaces_pending_version is not None:
            payload["replaces_pending_version"] = self.replaces_pending_version
        if self.replaces_confirmed_version is not None:
            payload["replaces_confirmed_version"] = self.replaces_confirmed_version
        canonical = json.dumps(
            payload,
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
    replaces_appointment_id: str | None = None
    created_at: datetime | None = None  # When the hold was made (the command clock).

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
    created_at: datetime


class HoldRepository(Protocol):
    def read_idempotency(self, command: CreateHold) -> IdempotencyRecord | None: ...

    def read_revision(self, business_id: str) -> int: ...

    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...

    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...

    def read_calendar_for_hold(
        self, business_id: str, start_at: datetime, end_at: datetime, policy: AvailabilityPolicy
    ) -> CalendarSnapshot: ...

    def read_appointment(self, appointment_id: str) -> Appointment | None: ...

    def read_replacement_guard(
        self, business_id: str, original_id: str
    ) -> ReplacementGuard | None: ...

    def commit_hold(self, expected_revision: int, commit: HoldCommit) -> None: ...


class HoldService:
    def __init__(self, repository: HoldRepository, max_attempts: int = 3) -> None:
        if max_attempts <= 0:
            raise ValueError("Retry budget must be positive")
        self._repository = repository
        self._max_attempts = max_attempts

    def existing(self, command: CreateHold) -> PendingHold | None:
        """The hold this exact command already committed, for a redelivered request."""
        record = self._repository.read_idempotency(command)
        return None if record is None else self._replay(record, command.request_hash())

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
            original: Appointment | None = None
            if command.replaces_appointment_id is not None:
                original = self._repository.read_appointment(command.replaces_appointment_id)
                if command.replaces_pending_version is not None:
                    if (
                        original is None
                        or original.business_id != command.business_id
                        or original.client_id != command.client_id
                        or original.status != CalendarStatus.PENDING_APPROVAL
                        or original.version != command.replaces_pending_version
                        or original.replaces_appointment_id is not None
                        or original.hold_expires_at is None
                        or original.hold_expires_at <= now
                    ):
                        raise InvalidReplacement("Original pending request was not found")
                elif (
                    original is None
                    or original.business_id != command.business_id
                    or original.client_id != command.client_id
                    or original.status != CalendarStatus.CONFIRMED
                    or (command.replaces_confirmed_version is not None
                        and original.version != command.replaces_confirmed_version)
                ):
                    raise InvalidReplacement("Original confirmed appointment was not found")
                guard = self._repository.read_replacement_guard(
                    command.business_id, original.appointment_id
                )
                if guard is not None and guard.expires_at > now:
                    raise ReplacementPending("Original already has an active replacement")
            snapshot = self._repository.read_calendar_for_hold(
                command.business_id,
                command.start_at,
                command.start_at + timedelta(minutes=command.duration_minutes),
                policy,
            )
            if snapshot.revision != revision:
                continue
            events = tuple(
                event for event in snapshot.events
                if original is None or event.event_id != original.appointment_id
            )
            local_day = command.start_at.astimezone(ZoneInfo(policy.timezone)).date()
            starts = available_starts(
                policy, local_day, command.duration_minutes, events, now
            )
            start = command.start_at.astimezone(UTC)
            if start not in starts:
                # The bounded candidate query is enough to validate this slot,
                # but suggestions need a full-day snapshot of other visits.
                full_snapshot = self._repository.read_calendar(command.business_id)
                if full_snapshot.revision != revision:
                    continue
                alternatives = available_starts(
                    policy,
                    local_day,
                    command.duration_minutes,
                    tuple(
                        event for event in full_snapshot.events
                        if original is None or event.event_id != original.appointment_id
                    ),
                    now,
                )
                # A retry may have raced its own first request: if that committed after the
                # key check above, this slot is taken by the caller's own hold, so replay it.
                committed = self._repository.read_idempotency(command)
                if committed is not None:
                    return self._replay(committed, request_hash)
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
                replaces_appointment_id=command.replaces_appointment_id,
                created_at=now.astimezone(UTC),
            )
            commit = HoldCommit(
                command=command,
                request_hash=request_hash,
                result=result,
                audit_id=f"hold-created#{hold_id}",
                outbox=(
                    OutboxIntent(
                        f"{hold_id}#owner", hold_id, "owner",
                        COUNTEROFFER_REQUEST_TEMPLATE
                        if command.replaces_pending_version is not None else "hold-request"),
                    OutboxIntent(f"{hold_id}#client", hold_id, "client", "hold-pending"),
                ),
                created_at=now.astimezone(UTC),
            )
            try:
                self._repository.commit_hold(revision, commit)
                return result
            except RevisionConflict:
                existing = self._repository.read_idempotency(command)
                if existing is not None:
                    return self._replay(existing, request_hash)
        committed = self._repository.read_idempotency(command)
        if committed is not None:
            return self._replay(committed, request_hash)
        raise TooManyConflicts("Calendar changed during every hold attempt")

    @staticmethod
    def _replay(record: IdempotencyRecord, request_hash: str) -> PendingHold:
        if record.request_hash != request_hash:
            raise IdempotencyKeyReused("Key already used for a different hold")
        return record.response
