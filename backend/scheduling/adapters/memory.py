"""Deterministic local calendar adapter for synthetic tests and development."""

from collections import defaultdict
from datetime import datetime
from threading import RLock

from scheduling.domain.availability import AvailabilityPolicy, pilot_policy
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot
from scheduling.domain.holds import (
    CreateHold,
    HoldCommit,
    IdempotencyRecord,
    RevisionConflict,
)


class InMemoryCalendarRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self._events: dict[str, tuple[CalendarEvent, ...]] = defaultdict(tuple)
        self._revisions: dict[str, int] = defaultdict(int)
        self._policies: dict[str, AvailabilityPolicy] = {}
        self._idempotency: dict[tuple[str, str, str, str], IdempotencyRecord] = {}
        self._holds: dict[str, HoldCommit] = {}
        self._outbox: dict[str, object] = {}
        self._audit: dict[str, object] = {}

    def read_revision(self, business_id: str) -> int:
        with self._lock:
            return self._revisions[business_id]

    def read_idempotency(self, command: CreateHold) -> IdempotencyRecord | None:
        with self._lock:
            return self._idempotency.get(self._idempotency_key(command))

    @staticmethod
    def _idempotency_key(command: CreateHold) -> tuple[str, str, str, str]:
        return (command.business_id, command.actor_id, "create_hold", command.idempotency_key)

    def commit_hold(self, expected_revision: int, commit: HoldCommit) -> None:
        with self._lock:
            business_id = commit.command.business_id
            if self._revisions[business_id] != expected_revision:
                raise RevisionConflict("Calendar revision changed")
            key = self._idempotency_key(commit.command)
            if key in self._idempotency:
                raise RevisionConflict("Idempotency record already exists")
            self._events[business_id] += (commit.result.calendar_event(),)
            self._holds[commit.result.hold_id] = commit
            self._idempotency[key] = IdempotencyRecord(commit.request_hash, commit.result)
            self._audit[commit.audit_id] = commit
            for intent in commit.outbox:
                self._outbox[intent.outbox_id] = intent
            self._revisions[business_id] += 1

    def read_calendar(self, business_id: str) -> CalendarSnapshot:
        with self._lock:
            return CalendarSnapshot(
                business_id=business_id,
                revision=self._revisions[business_id],
                events=self._events[business_id],
            )

    def read_calendar_for_hold(
        self,
        business_id: str,
        start_at: datetime,
        end_at: datetime,
        policy: AvailabilityPolicy,
    ) -> CalendarSnapshot:
        return self.read_calendar(business_id)

    def read_policy(self, business_id: str) -> AvailabilityPolicy:
        with self._lock:
            return self._policies.get(business_id, pilot_policy())

    def set_policy_for_test(self, business_id: str, policy: AvailabilityPolicy) -> None:
        with self._lock:
            self._policies[business_id] = policy

    def replace_for_test(
        self, business_id: str, expected_revision: int, events: tuple[CalendarEvent, ...]
    ) -> CalendarSnapshot:
        """Seed synthetic state with the same revision guard future writes need."""
        with self._lock:
            if self._revisions[business_id] != expected_revision:
                raise RevisionConflict("Calendar revision changed")
            self._events[business_id] = events
            self._revisions[business_id] += 1
            return self.read_calendar(business_id)
