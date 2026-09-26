"""Deterministic local calendar adapter for synthetic tests and development."""

from collections import defaultdict
from threading import RLock

from scheduling.domain.availability import AvailabilityPolicy, pilot_policy
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot


class RevisionConflict(Exception):
    """A calendar mutation lost a race with another writer."""


class InMemoryCalendarRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self._events: dict[str, tuple[CalendarEvent, ...]] = defaultdict(tuple)
        self._revisions: dict[str, int] = defaultdict(int)
        self._policies: dict[str, AvailabilityPolicy] = {}

    def read_calendar(self, business_id: str) -> CalendarSnapshot:
        with self._lock:
            return CalendarSnapshot(
                business_id=business_id,
                revision=self._revisions[business_id],
                events=self._events[business_id],
            )

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
