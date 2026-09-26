"""Deterministic availability for explicitly configured pilot scheduling rules."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from scheduling.domain.calendar import CalendarEvent, CalendarRepository, CalendarStatus
from scheduling.domain.holidays import observed_us_federal_holidays


class HolidayCalendar(StrEnum):
    US_FEDERAL = "US_FEDERAL"


@dataclass(frozen=True)
class LocalWindow:
    opens: time
    closes: time

    def __post_init__(self) -> None:
        if self.opens.tzinfo is not None or self.closes.tzinfo is not None:
            raise ValueError("Working hours must be local wall times")
        if self.closes <= self.opens:
            raise ValueError("Working window must open and close on the same day")


@dataclass(frozen=True)
class AvailabilityPolicy:
    timezone: str
    weekly_windows: dict[int, tuple[LocalWindow, ...]]
    booking_horizon_days: int
    slot_increment_minutes: int
    maximum_visit_minutes: int
    minimum_visit_gap_minutes: int
    opening_buffer_minutes: int
    closing_buffer_minutes: int
    date_exceptions: dict[date, tuple[LocalWindow, ...]] = field(default_factory=dict)
    holiday_calendar: HolidayCalendar | None = None

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if not all(0 <= day <= 6 for day in self.weekly_windows):
            raise ValueError("Weekdays must be 0 (Monday) through 6 (Sunday)")
        if self.booking_horizon_days <= 0 or self.slot_increment_minutes <= 0:
            raise ValueError("Horizon and slot increment must be positive")
        if self.maximum_visit_minutes <= 0:
            raise ValueError("Maximum visit duration must be positive")
        if min(
            self.minimum_visit_gap_minutes,
            self.opening_buffer_minutes,
            self.closing_buffer_minutes,
        ) < 0:
            raise ValueError("Buffers cannot be negative")


def _local_instants(day: date, wall_time: time, zone: ZoneInfo) -> tuple[datetime, ...]:
    """Return valid UTC instants; nonexistent wall times have none, fall-back times have two."""
    naive = datetime.combine(day, wall_time)
    instants: set[datetime] = set()
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        utc = local.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            instants.add(utc)
    return tuple(sorted(instants))


def _visit_conflicts(
    start: datetime,
    end: datetime,
    event: CalendarEvent,
    candidate_gap: timedelta,
) -> bool:
    if event.status == CalendarStatus.UNAVAILABLE:
        return start < event.end_at and event.start_at < end
    required_gap = max(candidate_gap, timedelta(minutes=event.buffer_minutes))
    return not (end + required_gap <= event.start_at or event.end_at + required_gap <= start)


def available_starts(
    policy: AvailabilityPolicy,
    day: date,
    duration_minutes: int,
    events: tuple[CalendarEvent, ...],
    now: datetime,
) -> tuple[datetime, ...]:
    """Return UTC starts; callers must revalidate within the calendar revision transaction."""
    if now.tzinfo is None:
        raise ValueError("Current instant must be timezone-aware")
    if duration_minutes <= 0 or duration_minutes > policy.maximum_visit_minutes:
        raise ValueError("Visit duration is outside the configured range")

    zone = ZoneInfo(policy.timezone)
    local_today = now.astimezone(zone).date()
    if day < local_today or day > local_today + timedelta(days=policy.booking_horizon_days):
        return ()

    if day in policy.date_exceptions:
        windows = policy.date_exceptions[day]
    elif (
        policy.holiday_calendar == HolidayCalendar.US_FEDERAL
        and day in observed_us_federal_holidays(day.year)
    ):
        windows = ()
    else:
        windows = policy.weekly_windows.get(day.weekday(), ())
    duration = timedelta(minutes=duration_minutes)
    gap = timedelta(minutes=policy.minimum_visit_gap_minutes)
    opening_buffer = timedelta(minutes=policy.opening_buffer_minutes)
    closing_buffer = timedelta(minutes=policy.closing_buffer_minutes)
    horizon_end = now + timedelta(days=policy.booking_horizon_days)
    active_events = tuple(event for event in events if event.occupies_time(now))
    result: set[datetime] = set()

    for window in windows:
        wall = datetime.combine(day, window.opens)
        last_wall = datetime.combine(day, window.closes)
        while wall < last_wall:
            for start in _local_instants(day, wall.time(), zone):
                end = start + duration
                end_local = end.astimezone(zone)
                if not (now < start <= horizon_end):
                    continue
                if end_local.date() != day or end_local.replace(tzinfo=None) > last_wall:
                    continue
                for open_instant in _local_instants(day, window.opens, zone):
                    if start >= open_instant + opening_buffer:
                        break
                else:
                    continue
                if not any(
                    end <= close_instant - closing_buffer
                    for close_instant in _local_instants(day, window.closes, zone)
                ):
                    continue
                if any(_visit_conflicts(start, end, event, gap) for event in active_events):
                    continue
                result.add(start)
            wall += timedelta(minutes=policy.slot_increment_minutes)

    return tuple(sorted(result))


def pilot_policy(
    date_exceptions: dict[date, tuple[LocalWindow, ...]] | None = None,
) -> AvailabilityPolicy:
    """Owner-approved starting policy; persisted owner settings replace these values."""
    weekday_hours = (LocalWindow(time(8), time(17)),)
    return AvailabilityPolicy(
        timezone="America/Los_Angeles",
        weekly_windows={weekday: weekday_hours for weekday in range(5)},
        booking_horizon_days=14,
        slot_increment_minutes=15,
        maximum_visit_minutes=180,
        minimum_visit_gap_minutes=30,
        opening_buffer_minutes=0,
        closing_buffer_minutes=0,
        date_exceptions=date_exceptions or {},
        holiday_calendar=HolidayCalendar.US_FEDERAL,
    )


class AvailabilityRepository(CalendarRepository, Protocol):
    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...


class AvailabilityService:
    def __init__(self, repository: AvailabilityRepository) -> None:
        self._repository = repository

    def find_starts(
        self, business_id: str, day: date, duration_minutes: int, now: datetime
    ) -> tuple[datetime, ...]:
        if not business_id:
            raise ValueError("Business ID is required")
        policy = self._repository.read_policy(business_id)
        snapshot = self._repository.read_calendar(business_id)
        return available_starts(policy, day, duration_minutes, snapshot.events, now)
