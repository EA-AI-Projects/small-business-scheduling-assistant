"""Bounded, repeatable dispatch of due hold-expiry transitions."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from scheduling.domain.appointments import Appointment
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    InvalidTransition,
    LifecycleService,
    StaleVersion,
)


class DueHoldRepository(Protocol):
    def due_hold_ids(self, now: datetime, limit: int) -> Iterable[str]: ...

    def read_appointment(self, appointment_id: str) -> Appointment | None: ...


@dataclass(frozen=True)
class ExpiryReport:
    examined: int
    expired: int
    stale: int
    oldest_overdue_seconds: float


class ExpiryService:
    def __init__(
        self, repository: DueHoldRepository, lifecycle: LifecycleService,
        clock: Callable[[], datetime], *, batch_size: int = 100,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("Expiry batch size must be positive")
        self._repository = repository
        self._lifecycle = lifecycle
        self._clock = clock
        self._batch_size = batch_size

    def run_once(self) -> ExpiryReport:
        now = self._utc(self._clock())
        examined = expired = stale = 0
        oldest_overdue = 0.0
        for appointment_id in self._repository.due_hold_ids(now, self._batch_size):
            examined += 1
            appointment = self._repository.read_appointment(appointment_id)
            if (
                appointment is None
                or appointment.status != CalendarStatus.PENDING_APPROVAL
                or appointment.hold_expires_at is None
                or appointment.hold_expires_at > now
            ):
                stale += 1
                continue
            oldest_overdue = max(
                oldest_overdue, (now - appointment.hold_expires_at).total_seconds()
            )
            command = AppointmentCommand(
                business_id=appointment.business_id,
                appointment_id=appointment.appointment_id,
                actor_id="system:hold-expiry",
                actor_role=ActorRole.SYSTEM,
                operation=Action.EXPIRE,
                idempotency_key=f"scheduled-expire:{appointment.appointment_id}:{appointment.version}",
                expected_version=appointment.version,
            )
            try:
                result = self._lifecycle.apply(command)
            except (InvalidTransition, StaleVersion):
                # Another transition won the race; the next sweep handles any
                # still-pending newer version.
                stale += 1
                continue
            if result.appointment.status == CalendarStatus.EXPIRED:
                expired += 1
            else:
                stale += 1
        return ExpiryReport(examined, expired, stale, oldest_overdue)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Expiry clock must return a timezone-aware instant")
        return value.astimezone(UTC)
