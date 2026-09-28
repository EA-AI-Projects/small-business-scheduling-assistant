"""The owner page uses protected detail and local-time reads."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.owner_api import OwnerPrincipal, OwnerUiConfig, create_owner_app

NOW = datetime(2026, 6, 29, 12, tzinfo=UTC)
BASE = "/v1/owner/businesses/pilot"
AUTH = {"Authorization": "Bearer verified-owner"}


def app() -> TestClient:
    repository = InMemoryCalendarRepository()

    def verify(token: str) -> OwnerPrincipal:
        if token != "verified-owner":
            raise ValueError("Invalid owner")
        return OwnerPrincipal("owner-1", "pilot")

    config = OwnerUiConfig("pilot", "public-client-id",
                           "https://login.example.test/oauth2/authorize",
                           "https://login.example.test/oauth2/token",
                           "https://owner.example.test/owner")
    return TestClient(create_owner_app(repository, verify, lambda: NOW, config))


def test_owner_page_is_static_and_api_reads_remain_authenticated() -> None:
    client = app()
    assert client.get("/owner").status_code == 200
    assert "Scheduling" in client.get("/owner").text
    assert "script-src 'self'" in client.get("/owner").headers["content-security-policy"]
    assert "async function signIn" in client.get("/owner/app.js").text
    assert client.get("/owner/style.css").status_code == 200
    config = client.get("/owner/config").json()
    assert config["client_id"] == "public-client-id"
    assert "owner_sub" not in config
    assert client.get(f"{BASE}/calendar").status_code == 401
    assert client.get(f"{BASE}/appointments/unknown", headers=AUTH).status_code == 404
    assert client.get(f"{BASE}/blocks/unknown", headers=AUTH).status_code == 404
    assert client.get("/v1/owner/businesses/other/appointments/unknown", headers=AUTH).status_code == 403


def test_local_time_resolves_one_instant_and_rejects_dst_ambiguity() -> None:
    client = app()
    assert client.post(f"{BASE}/policy/seed", headers={**AUTH, "Idempotency-Key": "seed"}).status_code == 200
    resolved = client.get(f"{BASE}/local-time", params={"value": "2026-07-06T09:00"},
                          headers=AUTH)
    assert resolved.status_code == 200
    assert resolved.json()["instant"] == "2026-07-06T16:00:00+00:00"
    for value in ("2026-03-08T02:30", "2026-11-01T01:30", "2026-07-06T09:00Z"):
        assert client.get(f"{BASE}/local-time", params={"value": value},
                          headers=AUTH).status_code == 422
    assert client.get(f"{BASE}/local-time", params={"value": "2026-07-06T09:00"}).status_code == 401


def test_owner_can_read_exact_calendar_details_after_creation() -> None:
    client = app()
    assert client.post(f"{BASE}/policy/seed", headers={**AUTH, "Idempotency-Key": "seed"}).status_code == 200
    appointment = client.post(f"{BASE}/appointments", json={
        "expected_revision": 1, "client_id": "client-1",
        "start_at": "2026-07-06T16:00:00Z", "duration_minutes": 60,
    }, headers={**AUTH, "Idempotency-Key": "appointment"})
    assert appointment.status_code == 200
    appointment_id = appointment.json()["appointment"]["appointment_id"]
    detail = client.get(f"{BASE}/appointments/{appointment_id}", headers=AUTH)
    assert detail.status_code == 200
    assert detail.json()["version"] == 1
    assert detail.json()["client_id"] == "client-1"
    assert client.get(f"{BASE}/appointments/{appointment_id}").status_code == 401

    block = client.post(f"{BASE}/blocks", json={
        "expected_revision": 2, "start_at": "2026-07-06T18:00:00Z",
        "end_at": "2026-07-06T19:00:00Z",
    }, headers={**AUTH, "Idempotency-Key": "block"})
    assert block.status_code == 200
    block_id = block.json()["block"]["block_id"]
    assert client.get(f"{BASE}/blocks/{block_id}", headers=AUTH).json()["version"] == 1
