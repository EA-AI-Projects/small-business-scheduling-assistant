"""Appointment transitions preserve reservations and notification intent atomically."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock

import pytest

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.holds import CreateHold, HoldService, ReplacementPending, SlotConflict
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    HoldExpired,
    InvalidTransition,
    LifecycleService,
    StaleVersion,
    TransitionCommit,
)

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
START = datetime(2026, 9, 29, 16, tzinfo=UTC)


def create_hold(
    repository: InMemoryCalendarRepository,
    key: str,
    start: datetime = START,
    client: str = "client-1",
    replaces: str | None = None,
) -> str:
    return HoldService(repository).create(
        CreateHold("business-1", client, client, key, start, 60, replaces), NOW
    ).hold_id


def command(
    appointment_id: str,
    action: Action,
    key: str,
    version: int = 1,
    role: ActorRole = ActorRole.OWNER,
    actor: str = "owner-1",
    start: datetime | None = None,
    duration: int | None = None,
) -> AppointmentCommand:
    return AppointmentCommand(
        "business-1", appointment_id, actor, role, action, key, version, start, duration
    )


def test_approval_replay_and_client_cancellation_release_slot() -> None:
    repository = InMemoryCalendarRepository()
    hold_id = create_hold(repository, "hold")
    service = LifecycleService(repository, lambda: NOW)

    approved = service.apply(command(hold_id, Action.APPROVE, "approve"))
    assert approved.appointment.status == CalendarStatus.CONFIRMED
    assert approved.appointment.version == 2
    assert service.apply(command(hold_id, Action.APPROVE, "approve")) == approved

    cancelled = service.apply(
        command(
            hold_id, Action.CANCEL, "cancel", version=2,
            role=ActorRole.CLIENT, actor="client-1",
        )
    )
    assert cancelled.appointment.status == CalendarStatus.CANCELLED
    assert repository.read_calendar("business-1").events == ()
    assert len(repository._audit) == 3
    assert len(repository._outbox) == 6
    assert create_hold(repository, "new", client="client-2")


def test_expired_hold_cannot_be_approved_and_expiry_is_replay_safe() -> None:
    repository = InMemoryCalendarRepository()
    hold_id = create_hold(repository, "hold")
    expired_at = repository.read_appointment(hold_id).hold_expires_at
    assert expired_at is not None
    service = LifecycleService(repository, lambda: expired_at)

    with pytest.raises(HoldExpired):
        service.apply(command(hold_id, Action.APPROVE, "approve"))
    result = service.apply(
        command(hold_id, Action.EXPIRE, "expire", role=ActorRole.SYSTEM, actor="expiry-job")
    )
    assert result.appointment.status == CalendarStatus.EXPIRED
    assert service.apply(
        command(hold_id, Action.EXPIRE, "expire", role=ActorRole.SYSTEM, actor="expiry-job")
    ) == result
    assert repository.read_calendar("business-1").events == ()


def test_replacement_decline_keeps_original_and_approval_swaps_atomically() -> None:
    repository = InMemoryCalendarRepository()
    service = LifecycleService(repository, lambda: NOW)
    original_id = create_hold(repository, "original")
    service.apply(command(original_id, Action.APPROVE, "approve-original"))

    replacement_id = create_hold(
        repository, "replacement", START + timedelta(minutes=30), replaces=original_id
    )
    with pytest.raises(ReplacementPending):
        create_hold(repository, "duplicate", START + timedelta(minutes=45), replaces=original_id)
    with pytest.raises(ReplacementPending):
        service.apply(command(original_id, Action.CANCEL, "cancel-original", version=2))

    declined = service.apply(command(replacement_id, Action.DECLINE, "decline"))
    assert declined.appointment.status == CalendarStatus.DECLINED
    assert repository.read_appointment(original_id).status == CalendarStatus.CONFIRMED
    assert repository.read_replacement_guard("business-1", original_id) is None

    next_id = create_hold(
        repository, "replacement-2", START + timedelta(minutes=30), replaces=original_id
    )
    approved = service.apply(command(next_id, Action.APPROVE, "approve-replacement"))
    assert approved.appointment.status == CalendarStatus.CONFIRMED
    assert approved.replaced_appointment is not None
    assert approved.replaced_appointment.status == CalendarStatus.CANCELLED
    assert repository.read_appointment(original_id).status == CalendarStatus.CANCELLED
    assert tuple(event.event_id for event in repository.read_calendar("business-1").events) == (
        next_id,
    )


def test_failed_reschedule_and_conflicting_edit_leave_original_unchanged() -> None:
    repository = InMemoryCalendarRepository()
    service = LifecycleService(repository, lambda: NOW)
    original_id = create_hold(repository, "original")
    service.apply(command(original_id, Action.APPROVE, "approve-original"))
    other_id = create_hold(repository, "other", START + timedelta(hours=2), "client-2")
    service.apply(command(other_id, Action.APPROVE, "approve-other"))
    audit_count = len(repository._audit)
    outbox_count = len(repository._outbox)

    with pytest.raises(SlotConflict):
        create_hold(
            repository, "bad-replacement", START + timedelta(minutes=75), replaces=original_id
        )
    with pytest.raises(SlotConflict):
        service.apply(command(original_id, Action.EDIT, "longer", version=2, duration=120))

    assert repository.read_appointment(original_id).duration_minutes == 60
    assert repository.read_appointment(original_id).status == CalendarStatus.CONFIRMED
    assert len(repository._audit) == audit_count
    assert len(repository._outbox) == outbox_count


def test_replacement_expiry_keeps_original_and_client_withdrawal_frees_guard() -> None:
    repository = InMemoryCalendarRepository()
    owner_service = LifecycleService(repository, lambda: NOW)
    original_id = create_hold(repository, "original")
    owner_service.apply(command(original_id, Action.APPROVE, "approve-original"))
    replacement_id = create_hold(
        repository, "replacement", START + timedelta(minutes=30), replaces=original_id
    )
    expiry = repository.read_appointment(replacement_id).hold_expires_at
    assert expiry is not None
    LifecycleService(repository, lambda: expiry).apply(
        command(replacement_id, Action.EXPIRE, "expire", role=ActorRole.SYSTEM, actor="job")
    )
    assert repository.read_appointment(original_id).status == CalendarStatus.CONFIRMED
    assert repository.read_replacement_guard("business-1", original_id) is None

    second_id = create_hold(
        repository, "replacement-2", START + timedelta(minutes=30), replaces=original_id
    )
    owner_service.apply(
        command(
            second_id, Action.CANCEL, "withdraw", role=ActorRole.CLIENT, actor="client-1"
        )
    )
    assert repository.read_appointment(original_id).status == CalendarStatus.CONFIRMED
    assert repository.read_replacement_guard("business-1", original_id) is None


def test_successful_owner_edit_snapshots_duration_and_emits_change_notice() -> None:
    repository = InMemoryCalendarRepository()
    service = LifecycleService(repository, lambda: NOW)
    appointment_id = create_hold(repository, "hold")
    service.apply(command(appointment_id, Action.APPROVE, "approve"))

    edited = service.apply(
        command(
            appointment_id, Action.EDIT, "edit", version=2,
            start=START + timedelta(minutes=15), duration=90,
        )
    )

    assert edited.appointment.status == CalendarStatus.CONFIRMED
    assert edited.appointment.duration_minutes == 90
    assert edited.appointment.buffer_minutes == 30
    assert edited.appointment.version == 3
    assert tuple(event.start_at for event in repository.read_calendar("business-1").events) == (
        START + timedelta(minutes=15),
    )
    assert any(intent.template == "edit_appointment" for intent in repository._outbox.values())


def test_approval_and_expiry_race_commit_at_most_one_terminal_result() -> None:
    class RacingRepository(InMemoryCalendarRepository):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = Barrier(2)
            self.counter_lock = Lock()
            self.transition_count = 0

        def commit_transition(self, expected_revision: int, commit: TransitionCommit) -> None:
            with self.counter_lock:
                self.transition_count += 1
                race = self.transition_count <= 2
            if race:
                self.barrier.wait(timeout=5)
            super().commit_transition(expected_revision, commit)

    repository = RacingRepository()
    hold_id = create_hold(repository, "hold")
    expiry = repository.read_appointment(hold_id).hold_expires_at
    assert expiry is not None
    before_expiry = expiry - timedelta(microseconds=1)

    def approve() -> str:
        try:
            result = LifecycleService(repository, lambda: before_expiry).apply(
                command(hold_id, Action.APPROVE, "approve")
            )
            return "approved" if result.appointment.status == CalendarStatus.CONFIRMED else "lost"
        except (InvalidTransition, HoldExpired, StaleVersion):
            return "lost"

    def expire() -> str:
        try:
            result = LifecycleService(repository, lambda: expiry).apply(
                command(hold_id, Action.EXPIRE, "expire", role=ActorRole.SYSTEM, actor="job")
            )
            return "expired" if result.appointment.status == CalendarStatus.EXPIRED else "lost"
        except (InvalidTransition, HoldExpired, StaleVersion):
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda fn: fn(), (approve, expire)))

    assert outcomes.count("lost") == 1
    assert len(repository._audit) == 2
    assert repository.read_appointment(hold_id).status in (
        CalendarStatus.CONFIRMED, CalendarStatus.EXPIRED
    )
