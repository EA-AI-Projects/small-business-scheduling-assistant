"""The local adapter exercises the same optimistic write boundary as DynamoDB."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Lock

import pytest

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.holds import (
    CreateHold,
    HoldCommit,
    HoldService,
    IdempotencyKeyReused,
    RevisionConflict,
    SlotConflict,
)

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
START = datetime(2026, 9, 29, 16, tzinfo=UTC)


def command(key: str, client: str = "client-1") -> CreateHold:
    return CreateHold("business-1", client, client, key, START, 60)


def test_hold_commits_snapshots_idempotency_audit_and_outbox() -> None:
    repository = InMemoryCalendarRepository()
    service = HoldService(repository)

    hold = service.create(command("key-1"), NOW)
    replay = service.create(command("key-1"), NOW)

    assert replay == hold
    assert hold.calendar_revision == 1
    assert hold.duration_minutes == 60
    assert hold.buffer_minutes == 30
    assert hold.hold_expires_at > NOW
    assert repository.read_calendar("business-1").events == (hold.calendar_event(),)
    assert len(repository._audit) == 1
    assert {intent.template for intent in repository._outbox.values()} == {
        "hold-request",
        "hold-pending",
    }
    with pytest.raises(IdempotencyKeyReused):
        service.create(CreateHold("business-1", "client-1", "client-1", "key-1", START, 120), NOW)


def test_two_concurrent_overlapping_holds_commit_exactly_one() -> None:
    class RacingRepository(InMemoryCalendarRepository):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = Barrier(2)
            self.counter_lock = Lock()
            self.read_count = 0

        def read_revision(self, business_id: str) -> int:
            revision = super().read_revision(business_id)
            with self.counter_lock:
                self.read_count += 1
                race = self.read_count <= 2
            if race:
                self.barrier.wait(timeout=5)
            return revision

    repository = RacingRepository()
    service = HoldService(repository)

    def attempt(index: int) -> str:
        try:
            service.create(command(f"key-{index}", f"client-{index}"), NOW)
            return "committed"
        except SlotConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (1, 2)))

    assert sorted(outcomes) == ["committed", "conflict"]
    assert repository.read_revision("business-1") == 1
    assert len(repository._audit) == 1
    assert len(repository._outbox) == 2


def test_expired_hold_does_not_block_new_hold() -> None:
    repository = InMemoryCalendarRepository()
    service = HoldService(repository)
    first = service.create(command("first"), NOW)
    later = first.hold_expires_at
    second = service.create(command("second", "client-2"), later)
    assert second.calendar_revision == 2


def test_revision_change_retries_and_commits_when_slot_remains_free() -> None:
    class InterleavingRepository(InMemoryCalendarRepository):
        def __init__(self) -> None:
            super().__init__()
            self.interleaved = False

        def commit_hold(self, expected_revision: int, commit: HoldCommit) -> None:
            if not self.interleaved:
                self.interleaved = True
                self.replace_for_test(commit.command.business_id, expected_revision, ())
                raise RevisionConflict("Another writer changed the calendar")
            super().commit_hold(expected_revision, commit)

    repository = InterleavingRepository()
    hold = HoldService(repository).create(command("retry"), NOW)
    assert hold.calendar_revision == 2
    assert repository.read_revision("business-1") == 2
