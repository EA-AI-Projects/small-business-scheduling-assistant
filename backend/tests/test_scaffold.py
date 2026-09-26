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
