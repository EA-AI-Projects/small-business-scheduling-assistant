"""Owner blocks and confirmed visits use the same revision and conflict rules."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.availability import pilot_policy
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.holds import CreateHold, HoldService, RevisionConflict, SlotConflict
from scheduling.domain.owner_calendar import (
    OwnerAction,
    OwnerCalendarCommand,
    OwnerCalendarCommit,
    OwnerCalendarService,
)
from scheduling.domain.owner_policy import OwnerPolicyService, PolicyCommand

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
START = datetime(2026, 9, 29, 16, tzinfo=UTC)


def ready() -> tuple[InMemoryCalendarRepository, OwnerCalendarService]:
    repository = InMemoryCalendarRepository()
    OwnerPolicyService(repository, lambda: NOW).seed("business-1", "owner-1", "seed")
    return repository, OwnerCalendarService(repository, lambda: NOW)


def command(
    repository: InMemoryCalendarRepository,
    operation: OwnerAction,
    key: str,
    **kwargs: object,
) -> OwnerCalendarCommand:
    return OwnerCalendarCommand(
        "business-1", "owner-1", key, operation,
        repository.read_revision("business-1"), **kwargs,
    )


def test_manual_appointment_is_confirmed_and_replay_safe() -> None:
    repository, service = ready()
    create = command(
        repository, OwnerAction.CREATE_APPOINTMENT, "manual",
        client_id="client-1", start_at=START, duration_minutes=60,
    )
    result = service.apply(create)
    assert result.appointment is not None
    assert result.appointment.status == CalendarStatus.CONFIRMED
    assert result.appointment.duration_minutes == 60
    assert result.appointment.buffer_minutes == 30
    assert service.apply(create) == result
    assert len(repository._audit) == 2
    assert len(repository._outbox) == 3

    with pytest.raises(SlotConflict):
        service.apply(command(
            repository, OwnerAction.CREATE_APPOINTMENT, "overlap",
            client_id="client-2", start_at=START + timedelta(minutes=30),
            duration_minutes=60,
        ))


def test_block_create_move_remove_and_conflict_preserve_previous_state() -> None:
    repository, service = ready()
    appointment = service.apply(command(
        repository, OwnerAction.CREATE_APPOINTMENT, "manual",
        client_id="client-1", start_at=START, duration_minutes=60,
    )).appointment
    assert appointment is not None
    block_start = START + timedelta(hours=3)
    block = service.apply(command(
        repository, OwnerAction.CREATE_BLOCK, "block", start_at=block_start,
        end_at=block_start + timedelta(hours=2),
    )).block
    assert block is not None and block.version == 1
    prior_revision = repository.read_revision("business-1")
    prior_audit = len(repository._audit)
    with pytest.raises(SlotConflict):
        service.apply(command(
            repository, OwnerAction.MOVE_BLOCK, "conflicting-move",
            block_id=block.block_id, expected_version=1,
            start_at=START, end_at=START + timedelta(hours=1),
        ))
    assert repository.read_block("business-1", block.block_id) == block
    assert repository.read_revision("business-1") == prior_revision
    assert len(repository._audit) == prior_audit

    moved = service.apply(command(
        repository, OwnerAction.MOVE_BLOCK, "move", block_id=block.block_id,
        expected_version=1, start_at=block_start + timedelta(hours=1),
        end_at=block_start + timedelta(hours=2),
    )).block
    assert moved is not None and moved.version == 2
    with pytest.raises(RevisionConflict, match="Block version"):
        service.apply(command(
            repository, OwnerAction.REMOVE_BLOCK, "stale", block_id=block.block_id,
            expected_version=1,
        ))
    removed = service.apply(command(
        repository, OwnerAction.REMOVE_BLOCK, "remove", block_id=block.block_id,
        expected_version=2,
    ))
    assert removed.block is None
    assert repository.read_block("business-1", block.block_id) is None
    assert tuple(e.event_id for e in repository.read_calendar("business-1").events) == (
        appointment.appointment_id,
    )


def test_block_and_date_exception_prevent_owner_booking() -> None:
    repository, service = ready()
    block = service.apply(command(
        repository, OwnerAction.CREATE_BLOCK, "block", start_at=START,
        end_at=START + timedelta(hours=1),
    )).block
    assert block is not None
    with pytest.raises(SlotConflict):
        service.apply(command(
            repository, OwnerAction.CREATE_APPOINTMENT, "blocked",
            client_id="client-1", start_at=START, duration_minutes=60,
        ))
    service.apply(command(
        repository, OwnerAction.REMOVE_BLOCK, "remove", block_id=block.block_id,
        expected_version=1,
    ))
    revision = repository.read_revision("business-1")
    closure = replace(pilot_policy(), date_exceptions={START.date(): ()})
    OwnerPolicyService(repository, lambda: NOW).apply(PolicyCommand(
        "business-1", "owner-1", "closure", revision, 1, closure,
    ))
    with pytest.raises(SlotConflict):
        service.apply(command(
            repository, OwnerAction.CREATE_APPOINTMENT, "closed",
            client_id="client-1", start_at=START, duration_minutes=60,
        ))


def test_existing_hold_prevents_block_and_stale_revision_fails() -> None:
    repository, service = ready()
    HoldService(repository).create(
        CreateHold("business-1", "client-1", "client-1", "hold", START, 60), NOW
    )
    with pytest.raises(SlotConflict):
        service.apply(command(
            repository, OwnerAction.CREATE_BLOCK, "overlap", start_at=START,
            end_at=START + timedelta(hours=1),
        ))
    with pytest.raises(RevisionConflict, match="Calendar revision"):
        service.apply(OwnerCalendarCommand(
            "business-1", "owner-1", "stale", OwnerAction.CREATE_BLOCK, 1,
            start_at=START + timedelta(hours=3),
            end_at=START + timedelta(hours=4),
        ))


def test_commit_conflict_replays_winning_owner_calendar_result() -> None:
    class AmbiguousCommitRepository(InMemoryCalendarRepository):
        def commit_owner_calendar(
            self, expected_revision: int, commit: OwnerCalendarCommit
        ) -> None:
            super().commit_owner_calendar(expected_revision, commit)
            raise RevisionConflict("Winner committed before caller observed it")

    repository = AmbiguousCommitRepository()
    OwnerPolicyService(repository, lambda: NOW).seed("business-1", "owner-1", "seed")
    service = OwnerCalendarService(repository, lambda: NOW)
    result = service.apply(command(
        repository, OwnerAction.CREATE_BLOCK, "block", start_at=START,
        end_at=START + timedelta(hours=1),
    ))
    assert result.block is not None
    assert repository.read_revision("business-1") == 2


def test_revision_read_race_replays_same_key_block_result() -> None:
    class RacingRepository(InMemoryCalendarRepository):
        command_to_race: OwnerCalendarCommand | None = None

        def read_revision(self, business_id: str) -> int:
            if self.command_to_race is not None:
                command = self.command_to_race
                self.command_to_race = None
                OwnerCalendarService(self, lambda: NOW).apply(command)
            return super().read_revision(business_id)

    repository = RacingRepository()
    OwnerPolicyService(repository, lambda: NOW).seed("business-1", "owner-1", "seed")
    create = OwnerCalendarCommand(
        "business-1", "owner-1", "block", OwnerAction.CREATE_BLOCK, 1,
        start_at=START, end_at=START + timedelta(hours=1),
    )
    repository.command_to_race = create
    result = OwnerCalendarService(repository, lambda: NOW).apply(create)
    assert result.block is not None
    assert repository.read_revision("business-1") == 2
