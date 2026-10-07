"""Authenticated settings flow for future booking invitation selection."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.owner_api import OwnerPrincipal, create_owner_app

BASE = "/v1/owner/businesses/pilot/booking-outreach"
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


def test_owner_can_configure_and_disable_outreach_without_starting_sms() -> None:
    repository = InMemoryCalendarRepository()
    app = create_owner_app(repository,
        lambda token: OwnerPrincipal("owner-1", "pilot") if token == "valid" else
        (_ for _ in ()).throw(ValueError("bad token")), lambda: NOW)
    api = TestClient(app)
    headers = {"Authorization": "Bearer valid", "Idempotency-Key": "first"}
    default = api.get(BASE, headers=headers)
    assert default.status_code == 200
    assert default.json() == {"settings": {"enabled": False, "weekday": None,
        "local_time": None, "lookahead_weeks": None, "message": None}, "version": 0}
    assert api.get(BASE).status_code == 401
    assert api.get(BASE.replace("pilot", "other"), headers=headers).status_code == 403

    body = {"expected_version": 0, "settings": {"enabled": True,
        "weekday": 0, "local_time": "09:00", "lookahead_weeks": 2}}
    assert api.put(BASE, json=body, headers=headers).status_code == 409
    OwnerPolicyService(repository, lambda: NOW).seed("pilot", "owner-1", "seed")
    assert api.put(BASE, json={**body, "settings": {**body["settings"],
        "lookahead_weeks": 3}}, headers=headers).status_code == 422
    assert api.put(BASE, json={**body, "settings": {**body["settings"],
        "weekday": None}}, headers=headers).status_code == 422
    saved = api.put(BASE, json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1
    assert repository.read_outreach("pilot").settings.lookahead_weeks == 2
    assert saved.json()["settings"]["message"] is None
    assert api.put(BASE, json=body, headers=headers).json() == saved.json()
    assert api.put(BASE, json={**body, "settings": {**body["settings"],
        "weekday": 1}}, headers=headers).json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert api.put(BASE, json=body, headers={**headers, "Idempotency-Key": "stale"}).status_code == 409

    disabled = api.put(BASE, json={"expected_version": 1, "settings": {
        **body["settings"], "enabled": False}},
        headers={**headers, "Idempotency-Key": "disable"})
    assert disabled.status_code == 200
    assert disabled.json()["settings"]["enabled"] is False
    assert api.get(BASE, headers=headers).json() == disabled.json()
    assert repository.read_revision("pilot") == 1


def test_owner_can_save_any_nonblank_invitation_text_and_legacy_put_preserves_it() -> None:
    repository = InMemoryCalendarRepository()
    OwnerPolicyService(repository, lambda: NOW).seed("pilot", "owner-1", "seed")
    app = create_owner_app(repository,
        lambda token: OwnerPrincipal("owner-1", "pilot"), lambda: NOW)
    api = TestClient(app)
    headers = {"Authorization": "Bearer valid", "Idempotency-Key": "custom"}
    settings = {"enabled": True, "weekday": 0, "local_time": "09:00",
                "lookahead_weeks": 1, "message": "Your appointment is waiting."}
    saved = api.put(BASE, json={"expected_version": 0, "settings": settings}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["message"] == settings["message"]
    legacy = api.put(BASE, json={"expected_version": 1, "settings": {
        key: value for key, value in settings.items() if key != "message"}},
        headers={**headers, "Idempotency-Key": "legacy"})
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["settings"]["message"] == settings["message"]
