"""Policy-independent calendar snapshot contract.

Issue #1 must supply operating rules before booking operations are enabled. This
read seam is shared by the API and future SMS adapter; neither adapter owns rules.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class CalendarStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    CONFIRMED = "CONFIRMED"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    start_at: datetime
    end_at: datetime
    status: CalendarStatus
    hold_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("Calendar instants must be timezone-aware")
        if self.end_at <= self.start_at:
            raise ValueError("Calendar event end must follow start")
        if self.status == CalendarStatus.PENDING_APPROVAL and self.hold_expires_at is None:
            raise ValueError("Pending events require hold expiry")
        if self.hold_expires_at is not None and self.hold_expires_at.tzinfo is None:
            raise ValueError("Hold expiry must be timezone-aware")

    def occupies_time(self, now: datetime) -> bool:
        if self.status in (CalendarStatus.CONFIRMED, CalendarStatus.UNAVAILABLE):
            return True
        return (
            self.status == CalendarStatus.PENDING_APPROVAL
            and self.hold_expires_at is not None
            and self.hold_expires_at > now
        )


@dataclass(frozen=True)
class CalendarSnapshot:
    business_id: str
    revision: int
    events: tuple[CalendarEvent, ...]


class CalendarRepository(Protocol):
    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...


class CalendarService:
    def __init__(self, repository: CalendarRepository) -> None:
        self._repository = repository

    def get_calendar(
        self,
        business_id: str,
        start_at: datetime,
        end_at: datetime,
        now: datetime | None = None,
    ) -> CalendarSnapshot:
        if not business_id:
            raise ValueError("Business ID is required")
        if start_at.tzinfo is None or end_at.tzinfo is None or end_at <= start_at:
            raise ValueError("Calendar window requires ordered timezone-aware instants")
        instant = now or datetime.now(UTC)
        snapshot = self._repository.read_calendar(business_id)
        return CalendarSnapshot(
            business_id=snapshot.business_id,
            revision=snapshot.revision,
            events=tuple(
                event
                for event in snapshot.events
                if event.occupies_time(instant)
                and event.start_at < end_at
                and start_at < event.end_at
            ),
        )
