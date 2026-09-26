from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from scheduling.domain.availability import AvailabilityPolicy, LocalWindow, available_starts
from scheduling.domain.calendar import CalendarEvent, CalendarStatus


def policy(
    *,
    hours: tuple[LocalWindow, ...] = (LocalWindow(time(8), time(17)),),
    day_of_week: int = 0,
    exceptions: dict[date, tuple[LocalWindow, ...]] | None = None,
) -> AvailabilityPolicy:
    return AvailabilityPolicy(
        timezone="America/Los_Angeles",
        weekly_windows={day_of_week: hours},
        booking_horizon_days=14,
        slot_increment_minutes=30,
        maximum_visit_minutes=720,
        minimum_visit_gap_minutes=30,
        opening_buffer_minutes=0,
        closing_buffer_minutes=0,
        date_exceptions=exceptions or {},
    )


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), ZoneInfo("America/Los_Angeles")).astimezone(UTC)


def test_adjacent_visits_hold_expiry_and_owner_block() -> None:
    day = date(2026, 10, 5)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    events = (
        CalendarEvent("visit", local(day, 9), local(day, 10), CalendarStatus.CONFIRMED),
        CalendarEvent("block", local(day, 12), local(day, 13), CalendarStatus.UNAVAILABLE),
        CalendarEvent(
            "expired", local(day, 13), local(day, 14), CalendarStatus.PENDING_APPROVAL,
            hold_expires_at=now,
        ),
        CalendarEvent(
            "active", local(day, 15), local(day, 16), CalendarStatus.PENDING_APPROVAL,
            hold_expires_at=now + timedelta(hours=1),
        ),
    )
    starts = available_starts(policy(), day, 60, events, now)
    assert local(day, 8) not in starts  # 30-minute travel gap before the 9 a.m. visit
    assert local(day, 10, 30) in starts
    assert local(day, 11) in starts  # an unavailable block does not add travel time
    assert local(day, 13) in starts  # expired hold is ignored immediately
    assert local(day, 14) not in starts  # active hold needs a travel gap
    assert local(day, 16) not in starts


def test_long_visit_started_before_query_day_and_exception_closure() -> None:
    day = date(2026, 10, 5)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    previous = day - timedelta(days=1)
    long_visit = CalendarEvent(
        "long", local(previous, 22), local(day, 9), CalendarStatus.CONFIRMED
    )
    starts = available_starts(policy(), day, 60, (long_visit,), now)
    assert local(day, 8) not in starts
    assert local(day, 9, 30) in starts
    assert available_starts(policy(exceptions={day: ()}), day, 60, (), now) == ()


def test_existing_visit_buffer_snapshot_survives_config_change() -> None:
    day = date(2026, 10, 5)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    visit = CalendarEvent(
        "visit", local(day, 9), local(day, 10), CalendarStatus.CONFIRMED,
        buffer_minutes=60,
    )
    starts = available_starts(policy(), day, 60, (visit,), now)
    assert local(day, 10, 30) not in starts
    assert local(day, 11) in starts


def test_spring_forward_skips_nonexistent_starts() -> None:
    day = date(2026, 3, 8)
    now = datetime(2026, 3, 7, 12, tzinfo=UTC)
    starts = available_starts(
        policy(hours=(LocalWindow(time(1), time(4)),), day_of_week=6),
        day, 30, (), now,
    )
    wall_hours = [start.astimezone(ZoneInfo("America/Los_Angeles")).hour for start in starts]
    assert wall_hours == [1, 1, 3, 3]


def test_fall_back_returns_both_real_instants_for_repeated_hour() -> None:
    day = date(2026, 11, 1)
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    starts = available_starts(
        policy(hours=(LocalWindow(time(1), time(3)),), day_of_week=6),
        day, 30, (), now,
    )
    repeated_one_oclock = [
        start for start in starts
        if start.astimezone(ZoneInfo("America/Los_Angeles")).time().hour == 1
    ]
    assert len(repeated_one_oclock) == 4
    assert len(set(repeated_one_oclock)) == 4


def test_horizon_duration_and_today_cutoff() -> None:
    day = date(2026, 10, 5)
    now = local(day, 10)
    starts = available_starts(policy(), day, 60, (), now)
    assert local(day, 10) not in starts
    assert local(day, 10, 30) in starts
    assert available_starts(policy(), day + timedelta(days=15), 60, (), now) == ()
