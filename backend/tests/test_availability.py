from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from scheduling.domain.availability import (
    AvailabilityPolicy,
    InvalidPolicy,
    LocalWindow,
    available_starts,
    pilot_policy,
)
from scheduling.domain.calendar import CalendarEvent, CalendarStatus
from scheduling.domain.holidays import observed_us_federal_holidays


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
        slot_increment_minutes=15,
        maximum_visit_minutes=180,
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


def test_long_visit_started_before_working_window_and_exception_closure() -> None:
    day = date(2026, 10, 5)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    long_visit = CalendarEvent(
        "long", local(day, 6), local(day, 9), CalendarStatus.CONFIRMED
    )
    starts = available_starts(policy(), day, 60, (long_visit,), now)
    assert local(day, 8) not in starts
    assert local(day, 9, 30) in starts
    assert available_starts(policy(exceptions={day: ()}), day, 60, (), now) == ()


def test_visit_started_on_previous_date_excludes_early_query_slots() -> None:
    day = date(2026, 10, 5)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    prior_day = day - timedelta(days=1)
    visit = CalendarEvent(
        "cross-date", local(prior_day, 23), local(day, 1), CalendarStatus.CONFIRMED
    )
    overnight_test_policy = policy(hours=(LocalWindow(time(0), time(3)),))
    starts = available_starts(overnight_test_policy, day, 60, (visit,), now)
    assert local(day, 0) not in starts
    assert local(day, 1, 15) not in starts
    assert local(day, 1, 30) in starts


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
    assert wall_hours == [1, 1, 1, 1, 3, 3, 3]


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
    assert len(repeated_one_oclock) == 8
    assert len(set(repeated_one_oclock)) == 8


def test_nonexistent_dst_window_boundary_is_rejected_explicitly() -> None:
    day = date(2026, 3, 8)
    with pytest.raises(InvalidPolicy, match="nonexistent DST boundary"):
        pilot_policy({day: (LocalWindow(time(2, 30), time(4)),)})

    weekly = policy(hours=(LocalWindow(time(2, 30), time(4)),), day_of_week=6)
    with pytest.raises(InvalidPolicy, match="nonexistent DST boundary"):
        available_starts(weekly, day, 30, (), datetime(2026, 3, 7, 12, tzinfo=UTC))


def test_horizon_duration_and_today_cutoff() -> None:
    day = date(2026, 10, 5)
    now = local(day, 10)
    starts = available_starts(policy(), day, 60, (), now)
    assert local(day, 10) not in starts
    assert local(day, 10, 30) in starts
    assert available_starts(policy(), day + timedelta(days=15), 60, (), now) == ()


def test_current_three_hour_visit_limit() -> None:
    day = date(2026, 10, 5)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    assert local(day, 8) in available_starts(policy(), day, 180, (), now)
    assert local(day, 8, 15) in available_starts(policy(), day, 180, (), now)
    with pytest.raises(ValueError):
        available_starts(policy(), day, 181, (), now)


def test_observed_us_federal_holiday_dates_match_opm_schedule() -> None:
    assert observed_us_federal_holidays(2026) == frozenset({
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3),
        date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11),
        date(2026, 11, 26), date(2026, 12, 25),
    })
    assert date(2027, 6, 18) in observed_us_federal_holidays(2027)
    assert date(2027, 7, 5) in observed_us_federal_holidays(2027)
    assert date(2027, 12, 24) in observed_us_federal_holidays(2027)
    assert date(2021, 12, 31) in observed_us_federal_holidays(2021)


def test_pilot_policy_weekdays_holiday_override_and_first_last_boundaries() -> None:
    now = datetime(2026, 6, 29, 12, tzinfo=UTC)
    baseline = pilot_policy()
    assert baseline.timezone == "America/Los_Angeles"
    assert baseline.slot_increment_minutes == 15
    assert baseline.maximum_visit_minutes == 180
    assert baseline.minimum_visit_gap_minutes == 30
    assert available_starts(baseline, date(2026, 7, 3), 60, (), now) == ()
    assert available_starts(baseline, date(2026, 7, 4), 60, (), now) == ()

    open_holiday = pilot_policy({date(2026, 7, 3): (LocalWindow(time(8), time(17)),)})
    assert local(date(2026, 7, 3), 8) in available_starts(
        open_holiday, date(2026, 7, 3), 60, (), now,
    )
    normal_monday = date(2026, 7, 6)
    starts = available_starts(baseline, normal_monday, 60, (), now)
    assert local(normal_monday, 8) in starts
    assert local(normal_monday, 16) in starts

    closed_monday = pilot_policy({normal_monday: ()})
    assert available_starts(closed_monday, normal_monday, 60, (), now) == ()
