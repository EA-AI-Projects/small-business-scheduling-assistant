"""Owner policy changes are versioned and preserve future reservations."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.availability import pilot_policy
from scheduling.domain.calendar import CalendarEvent, CalendarStatus
from scheduling.domain.holds import CreateHold, HoldService, IdempotencyKeyReused, RevisionConflict
from scheduling.domain.owner_policy import (
    OwnerPolicyService,
    PolicyCommand,
    PolicyCommit,
    PolicyConflict,
)

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
START = datetime(2026, 9, 29, 16, tzinfo=UTC)


def test_seed_and_versioned_exception_edit_are_replay_safe() -> None:
    repository = InMemoryCalendarRepository()
    service = OwnerPolicyService(repository, lambda: NOW)

    seeded = service.seed("business-1", "owner-1", "seed")
    assert seeded.record.policy == pilot_policy()
    assert seeded.record.version == 1
    assert seeded.calendar_revision == 1
    assert service.seed("business-1", "owner-1", "seed") == seeded

    closure = replace(pilot_policy(), date_exceptions={START.date(): ()})
    command = PolicyCommand("business-1", "owner-1", "close-day", 1, 1, closure)
    edited = service.apply(command)
    assert edited.record.version == 2
    assert repository.read_policy("business-1").date_exceptions[START.date()] == ()
    assert service.apply(command) == edited
    assert len(repository._audit) == 2
    assert len(repository._outbox) == 2

    with pytest.raises(IdempotencyKeyReused):
        service.apply(replace(command, policy=pilot_policy()))


def test_closure_and_lowered_caps_preserve_active_hold() -> None:
    repository = InMemoryCalendarRepository()
    service = OwnerPolicyService(repository, lambda: NOW)
    service.seed("business-1", "owner-1", "seed")
    hold = HoldService(repository).create(
        CreateHold("business-1", "client-1", "client-1", "hold", START, 120), NOW
    )
    revision = repository.read_revision("business-1")
    audit_count = len(repository._audit)
    outbox_count = len(repository._outbox)

    with pytest.raises(PolicyConflict):
        service.apply(PolicyCommand(
            "business-1", "owner-1", "close-day", revision, 1,
            replace(pilot_policy(), date_exceptions={START.date(): ()}),
        ))
    with pytest.raises(PolicyConflict):
        service.apply(PolicyCommand(
            "business-1", "owner-1", "lower-cap", revision, 1,
            replace(pilot_policy(), maximum_visit_minutes=60),
        ))
    assert repository.read_revision("business-1") == revision
    assert repository.read_appointment(hold.hold_id) is not None
    assert repository.read_policy_record("business-1").version == 1
    assert len(repository._audit) == audit_count
    assert len(repository._outbox) == outbox_count


def test_stale_calendar_revision_and_policy_version_fail_without_write() -> None:
    repository = InMemoryCalendarRepository()
    service = OwnerPolicyService(repository, lambda: NOW)
    service.seed("business-1", "owner-1", "seed")
    closure = replace(pilot_policy(), date_exceptions={START.date(): ()})

    with pytest.raises(RevisionConflict, match="Calendar revision"):
        service.apply(PolicyCommand("business-1", "owner-1", "stale-revision", 0, 1, closure))
    with pytest.raises(RevisionConflict, match="Policy version"):
        service.apply(PolicyCommand("business-1", "owner-1", "stale-version", 1, 2, closure))
    assert repository.read_revision("business-1") == 1


def test_ongoing_visit_snapshot_blocks_lowered_lookback_cap() -> None:
    repository = InMemoryCalendarRepository()
    service = OwnerPolicyService(repository, lambda: NOW)
    service.seed("business-1", "owner-1", "seed")
    ongoing = CalendarEvent(
        "visit-1", NOW - timedelta(minutes=30), NOW + timedelta(minutes=90),
        CalendarStatus.CONFIRMED, duration_minutes=120, buffer_minutes=30,
    )
    repository.replace_for_test("business-1", 1, (ongoing,))

    with pytest.raises(PolicyConflict, match="cap"):
        service.apply(PolicyCommand(
            "business-1", "owner-1", "lower-cap", 2, 1,
            replace(pilot_policy(), maximum_visit_minutes=60),
        ))
    assert repository.read_policy_record("business-1").version == 1


def test_commit_conflict_replays_winning_policy_result() -> None:
    class AmbiguousCommitRepository(InMemoryCalendarRepository):
        def commit_policy(self, expected_revision: int, commit: PolicyCommit) -> None:
            super().commit_policy(expected_revision, commit)
            raise RevisionConflict("Winner committed before caller observed it")

    repository = AmbiguousCommitRepository()
    result = OwnerPolicyService(repository, lambda: NOW).seed(
        "business-1", "owner-1", "seed"
    )
    assert result.record.version == 1
    assert repository.read_revision("business-1") == 1


def test_revision_read_race_replays_same_key_policy_result() -> None:
    class RacingRepository(InMemoryCalendarRepository):
        raced = False

        def read_revision(self, business_id: str) -> int:
            if not self.raced:
                self.raced = True
                OwnerPolicyService(self, lambda: NOW).seed(business_id, "owner-1", "seed")
            return super().read_revision(business_id)

    repository = RacingRepository()
    result = OwnerPolicyService(repository, lambda: NOW).seed(
        "business-1", "owner-1", "seed"
    )
    assert result.record.version == 1
    assert repository.read_revision("business-1") == 1
