"""Persisted appointment state shared by the hold and lifecycle commands."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from scheduling.domain.calendar import CalendarEvent, CalendarStatus


@dataclass(frozen=True)
class Appointment:
    appointment_id: str
    business_id: str
    client_id: str
    start_at: datetime
    end_at: datetime
    status: CalendarStatus
    hold_expires_at: datetime | None
    duration_minutes: int
    buffer_minutes: int
    version: int
    replaces_appointment_id: str | None = None

    def __post_init__(self) -> None:
        if not all((self.appointment_id, self.business_id, self.client_id)):
            raise ValueError("Appointment identity is required")
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("Appointment instants must be timezone-aware")
        if self.end_at - self.start_at != timedelta(minutes=self.duration_minutes):
            raise ValueError("Appointment duration snapshot must match its interval")
        if self.buffer_minutes < 0 or self.version <= 0:
            raise ValueError("Appointment buffer and version are invalid")
        if self.status == CalendarStatus.PENDING_APPROVAL and self.hold_expires_at is None:
            raise ValueError("Pending appointments require hold expiry")

    def occupies_time(self, now: datetime) -> bool:
        if self.status == CalendarStatus.CONFIRMED:
            return True
        return (
            self.status == CalendarStatus.PENDING_APPROVAL
            and self.hold_expires_at is not None
            and self.hold_expires_at > now
        )

    def calendar_event(self) -> CalendarEvent:
        return CalendarEvent(
            event_id=self.appointment_id,
            start_at=self.start_at,
            end_at=self.end_at,
            status=self.status,
            hold_expires_at=self.hold_expires_at,
            duration_minutes=self.duration_minutes,
            buffer_minutes=self.buffer_minutes,
        )


@dataclass(frozen=True)
class ReplacementGuard:
    original_id: str
    replacement_id: str
    expires_at: datetime
