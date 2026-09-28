"""The HTTP boundary derives owner identity and preserves domain write semantics."""

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from scheduling.adapters.dynamodb import encode_policy
from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.availability import pilot_policy
from scheduling.owner_api import OwnerPrincipal, create_owner_app

NOW = datetime(2026, 6, 29, 12, tzinfo=UTC)
START = "2026-07-06T16:00:00Z"
BASE = "/v1/owner/businesses/pilot"


def client() -> tuple[TestClient, InMemoryCalendarRepository]:
    repository = InMemoryCalendarRepository()

    def verify(token: str) -> OwnerPrincipal:
        if token != "verified-owner":
            raise ValueError("Invalid token")
        return OwnerPrincipal("owner-1", "pilot")

    return TestClient(create_owner_app(repository, verify, lambda: NOW)), repository


def headers(key: str = "request-1") -> dict[str, str]:
    return {"Authorization": "Bearer verified-owner", "Idempotency-Key": key}


def test_owner_routes_reject_missing_invalid_and_cross_business_credentials() -> None:
    api, repository = client()
    assert api.post(f"{BASE}/policy/seed", headers={"Idempotency-Key": "seed"}).status_code == 401
    assert api.post(f"{BASE}/policy/seed", headers={
        "Authorization": "Bearer fake", "Idempotency-Key": "seed",
    }).status_code == 401
    assert api.post("/v1/owner/businesses/other/policy/seed", headers=headers("seed")).status_code == 403
    assert repository.read_revision("pilot") == 0
    assert api.post(f"{BASE}/policy/seed", headers={
        "Authorization": "Bearer verified-owner",
    }).status_code == 422


def test_owner_request_approval_replay_and_stale_conflict() -> None:
    api, repository = client()
    assert api.post(f"{BASE}/policy/seed", headers=headers("seed")).status_code == 200
    payload = {"client_id": "client-1", "start_at": START, "duration_minutes": 120}
    created = api.post(f"{BASE}/requests", json=payload, headers=headers("hold"))
    assert created.status_code == 200, created.text
    request_id = created.json()["hold_id"]
    assert len(api.get(f"{BASE}/requests", headers=headers()).json()) == 1
    assert api.post(f"{BASE}/requests", json=payload, headers=headers("hold")).json() == created.json()
    reused = api.post(f"{BASE}/requests", json={**payload, "duration_minutes": 60},
                      headers=headers("hold"))
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    decision = api.post(f"{BASE}/requests/{request_id}/approve",
                        json={"expected_version": 1}, headers=headers("approve"))
    assert decision.status_code == 200, decision.text
    assert decision.json()["appointment"]["status"] == "CONFIRMED"
    assert api.get(f"{BASE}/requests", headers=headers()).json() == []
    assert api.post(f"{BASE}/requests/{request_id}/approve",
                    json={"expected_version": 1}, headers=headers("approve")).json() == decision.json()
    stale = api.post(f"{BASE}/appointments/{request_id}/cancel",
                     json={"expected_version": 1}, headers=headers("cancel"))
    assert stale.status_code == 409
    assert stale.json()["current"]["status"] == "CONFIRMED"
    assert repository.read_revision("pilot") == 3


def test_owner_block_and_manual_appointment_use_same_revision_guard() -> None:
    api, repository = client()
    api.post(f"{BASE}/policy/seed", headers=headers("seed"))
    block = api.post(f"{BASE}/blocks", json={
        "expected_revision": 1, "start_at": START, "end_at": "2026-07-06T17:00:00Z",
    }, headers=headers("block"))
    assert block.status_code == 200, block.text
    assert block.json()["block"]["version"] == 1
    conflict = api.post(f"{BASE}/appointments", json={
        "expected_revision": 2, "client_id": "client-1", "start_at": START,
        "duration_minutes": 60,
    }, headers=headers("manual"))
    assert conflict.status_code == 409
    assert repository.read_revision("pilot") == 2
    block_id = block.json()["block"]["block_id"]
    removed = api.request("DELETE", f"{BASE}/blocks/{block_id}", json={
        "expected_revision": 2, "expected_version": 1,
    }, headers=headers("remove"))
    assert removed.status_code == 200, removed.text
    manual = api.post(f"{BASE}/appointments", json={
        "expected_revision": 3, "client_id": "client-1", "start_at": START,
        "duration_minutes": 60,
    }, headers=headers("manual"))
    assert manual.status_code == 200, manual.text
    assert manual.json()["appointment"]["status"] == "CONFIRMED"


def test_owner_api_rejects_ambiguous_target_and_caller_role_without_write() -> None:
    api, repository = client()
    api.post(f"{BASE}/policy/seed", headers=headers("seed"))
    unknown = api.post(f"{BASE}/requests/unknown/approve",
                       json={"expected_version": 1}, headers=headers("unknown"))
    assert unknown.status_code == 409
    assert unknown.json()["error"]["code"] == "INVALID_TARGET"
    role = api.post(f"{BASE}/requests", json={
        "client_id": "client-1", "start_at": START, "duration_minutes": 60,
        "actor_role": "owner",
    }, headers=headers("role"))
    assert role.status_code == 422
    assert repository.read_revision("pilot") == 1


def test_owner_policy_edit_uses_persisted_version_and_revision() -> None:
    api, repository = client()
    api.post(f"{BASE}/policy/seed", headers=headers("seed"))
    policy = json.loads(encode_policy(pilot_policy()))
    policy["date_exceptions"] = {"2026-07-06": []}
    body = {"expected_revision": 1, "expected_version": 1, "policy": policy}
    edited = api.put(f"{BASE}/policy", json=body, headers=headers("close-day"))
    assert edited.status_code == 200, edited.text
    assert edited.json()["record"]["version"] == 2
    assert edited.json()["calendar_revision"] == 2
    assert api.put(f"{BASE}/policy", json=body, headers=headers("close-day")).json() == edited.json()
    stale = api.put(f"{BASE}/policy", json={**body, "policy": {
        **policy, "date_exceptions": {},
    }}, headers=headers("different"))
    assert stale.status_code == 409
    assert repository.read_revision("pilot") == 2
