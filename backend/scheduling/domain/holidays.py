"""Observed nationwide US federal holiday dates for the pilot closure baseline.

Matches the recurring nationwide holidays and Friday/Monday observations shown by
the US Office of Personnel Management: https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/
The business can override any date in its editable calendar configuration.
"""

from datetime import date, timedelta
from functools import lru_cache


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _actual_holidays(year: int) -> tuple[date, ...]:
    return (
        date(year, 1, 1),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _last_weekday(year, 5, 0),  # Memorial Day
        date(year, 6, 19),  # Juneteenth
        date(year, 7, 4),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        date(year, 11, 11),  # Veterans Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving Day
        date(year, 12, 25),  # Christmas Day
    )


@lru_cache(maxsize=16)
def observed_us_federal_holidays(year: int) -> frozenset[date]:
    """Include cross-year observations such as Friday, Dec 31 for a Saturday Jan 1."""
    return frozenset(
        observed
        for holiday_year in (year - 1, year, year + 1)
        for holiday in _actual_holidays(holiday_year)
        if (observed := _observed(holiday)).year == year
    )
