"""Scheduled sweeps use the lifecycle transaction, including approval races."""

from datetime import UTC, datetime

import pytest

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.expiry import ExpiryService
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    LifecycleService,
    StaleVersion,
)

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
START = datetime(2026, 9, 29, 16, tzinfo=UTC)


class IndexedMemoryRepository(InMemoryCalendarRepository):
    def __init__(self) -> None:
        super().__init__()
        self.index_ids: list[str] = []

    def due_hold_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
        return tuple(self.index_ids[:limit])


def hold(repository: IndexedMemoryRepository, key: str, *, replaces: str | None = None) -> str:
    result = HoldService(repository).create(
        CreateHold("business-1", "client-1", "client-1", key, START, 60, replaces), NOW
    )
    repository.index_ids.append(result.hold_id)
    return result.hold_id


def approve(repository: IndexedMemoryRepository, appointment_id: str) -> None:
    LifecycleService(repository, lambda: NOW).apply(
        AppointmentCommand(
            "business-1", appointment_id, "owner-1", ActorRole.OWNER,
            Action.APPROVE, f"approve-{appointment_id}", 1,
        )
    )


def test_sweep_skips_not_due_and_is_repeatable_after_expiry() -> None:
    repository = IndexedMemoryRepository()
    appointment_id = hold(repository, "hold")
    due = repository.read_appointment(appointment_id).hold_expires_at
    assert due is not None

    before = ExpiryService(repository, LifecycleService(repository, lambda: NOW), lambda: NOW)
    assert before.run_once().expired == 0
    assert repository.read_appointment(appointment_id).status == CalendarStatus.PENDING_APPROVAL

    service = ExpiryService(repository, LifecycleService(repository, lambda: due), lambda: due)
    assert service.run_once().expired == 1
    assert service.run_once().expired == 0
    assert repository.read_appointment(appointment_id).status == CalendarStatus.EXPIRED
    assert repository.read_calendar("business-1").events == ()


def test_stale_index_entry_cannot_expire_approved_appointment() -> None:
    repository = IndexedMemoryRepository()
    appointment_id = hold(repository, "hold")
    approve(repository, appointment_id)
    due = repository.read_appointment(appointment_id).hold_expires_at
    assert due is not None

    report = ExpiryService(repository, LifecycleService(repository, lambda: due), lambda: due).run_once()
    assert (report.examined, report.expired, report.stale) == (1, 0, 1)
    assert repository.read_appointment(appointment_id).status == CalendarStatus.CONFIRMED


def test_approval_racing_between_read_and_expire_wins_without_cancellation() -> None:
    repository = IndexedMemoryRepository()
    appointment_id = hold(repository, "hold")
    due = repository.read_appointment(appointment_id).hold_expires_at
    assert due is not None

    class RacingLifecycle:
        def apply(self, command: AppointmentCommand):
            approve(repository, appointment_id)
            return LifecycleService(repository, lambda: due).apply(command)

    report = ExpiryService(repository, RacingLifecycle(), lambda: due).run_once()  # type: ignore[arg-type]
    assert (report.expired, report.stale) == (0, 1)
    assert repository.read_appointment(appointment_id).status == CalendarStatus.CONFIRMED


def test_expired_replacement_releases_guard_and_preserves_original() -> None:
    repository = IndexedMemoryRepository()
    original_id = hold(repository, "original")
    approve(repository, original_id)
    replacement_id = hold(repository, "replacement", replaces=original_id)
    due = repository.read_appointment(replacement_id).hold_expires_at
    assert due is not None

    report = ExpiryService(repository, LifecycleService(repository, lambda: due), lambda: due).run_once()
    assert report.expired == 1
    assert repository.read_appointment(replacement_id).status == CalendarStatus.EXPIRED
    assert repository.read_appointment(original_id).status == CalendarStatus.CONFIRMED
    assert repository.read_replacement_guard("business-1", original_id) is None


def test_stale_version_is_skipped_but_unexpected_failure_is_retried() -> None:
    repository = IndexedMemoryRepository()
    appointment_id = hold(repository, "hold")
    due = repository.read_appointment(appointment_id).hold_expires_at
    assert due is not None

    class StaleLifecycle:
        def apply(self, command: AppointmentCommand):
            raise StaleVersion("A concurrent writer won")

    report = ExpiryService(repository, StaleLifecycle(), lambda: due).run_once()  # type: ignore[arg-type]
    assert (report.expired, report.stale) == (0, 1)

    class FailingLifecycle:
        def apply(self, command: AppointmentCommand):
            raise RuntimeError("Database unavailable")

    with pytest.raises(RuntimeError, match="Database unavailable"):
        ExpiryService(repository, FailingLifecycle(), lambda: due).run_once()  # type: ignore[arg-type]
