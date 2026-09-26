from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository, RevisionConflict
from scheduling.api import create_app
from scheduling.domain.calendar import CalendarEvent, CalendarStatus


def test_api_uses_injected_calendar_adapter_and_ignores_expired_hold() -> None:
    repository = InMemoryCalendarRepository()
    now = datetime.now(UTC)
    repository.replace_for_test(
        "pilot",
        expected_revision=0,
        events=(
            CalendarEvent("confirmed", now, now + timedelta(hours=2), CalendarStatus.CONFIRMED),
            CalendarEvent(
                "expired", now, now + timedelta(hours=1), CalendarStatus.PENDING_APPROVAL,
                hold_expires_at=now - timedelta(seconds=1),
            ),
        ),
    )
    client = TestClient(create_app(repository))

    assert client.get("/health").json() == {"status": "ok"}
    response = client.get(
        "/v1/businesses/pilot/calendar",
        params={
            "start_at": (now - timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=3)).isoformat(),
        },
    )
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert [event["event_id"] for event in response.json()["events"]] == ["confirmed"]


def test_calendar_query_rejects_naive_or_reversed_window() -> None:
    client = TestClient(create_app())
    assert client.get(
        "/v1/businesses/pilot/calendar",
        params={"start_at": "2026-10-01T10:00:00", "end_at": "2026-10-01T12:00:00"},
    ).status_code == 422
    assert client.get(
        "/v1/businesses/pilot/calendar",
        params={"start_at": "2026-10-01T12:00:00Z", "end_at": "2026-10-01T10:00:00Z"},
    ).status_code == 422


def test_local_adapter_rejects_stale_calendar_revision() -> None:
    repository = InMemoryCalendarRepository()
    repository.replace_for_test("pilot", expected_revision=0, events=())
    with pytest.raises(RevisionConflict):
        repository.replace_for_test("pilot", expected_revision=0, events=())


def test_availability_api_uses_pilot_policy_and_active_calendar() -> None:
    repository = InMemoryCalendarRepository()
    now = datetime(2026, 6, 29, 12, tzinfo=UTC)
    day = datetime(2026, 7, 6, tzinfo=UTC).date()
    start = datetime(2026, 7, 6, 16, tzinfo=UTC)  # 9 a.m. Pacific daylight time
    repository.replace_for_test(
        "pilot",
        0,
        (
            CalendarEvent(
                "visit", start, start + timedelta(hours=1), CalendarStatus.CONFIRMED
            ),
        ),
    )
    client = TestClient(create_app(repository, clock=lambda: now))

    response = client.get(
        "/v1/businesses/pilot/availability",
        params={"day": day.isoformat(), "duration_minutes": 60},
    )
    assert response.status_code == 200
    starts = response.json()["starts_at"]
    assert "2026-07-06T15:00:00Z" not in starts  # 8 a.m. lacks travel gap before visit
    assert "2026-07-06T17:30:00Z" in starts  # 10:30 a.m. has travel gap

    holiday = client.get(
        "/v1/businesses/pilot/availability",
        params={"day": "2026-07-03", "duration_minutes": 60},
    )
    assert holiday.json()["starts_at"] == []
