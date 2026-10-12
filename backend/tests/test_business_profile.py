"""Owner business profile: create, edit, stale version, validation, and isolation (#336)."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.owner_api import OwnerPrincipal, create_owner_app

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
PROFILE = {"display_name": "Example Cleaning Co", "description": "Home cleaning.",
           "support_contact": "help@example.test", "service_name": "Standard clean",
           "service_description": "Whole-home cleaning visit.", "default_duration_minutes": 120}


def _api(repository: InMemoryCalendarRepository) -> TestClient:
    owners = {"a": OwnerPrincipal("owner-a", "biz-a"), "b": OwnerPrincipal("owner-b", "biz-b")}
    return TestClient(create_owner_app(repository, lambda token: owners[token], lambda: NOW))


def test_profile_create_edit_stale_validation_and_business_isolation() -> None:
    repository = InMemoryCalendarRepository()
    api = _api(repository)
    url = "/v1/owner/businesses/biz-a/profile"
    a = {"Authorization": "Bearer a", "Idempotency-Key": "k1"}

    assert api.get(url).status_code == 401
    assert api.get(url, headers=a).json() == {"profile": None, "version": 0}

    created = api.put(url, json={"expected_version": 0, "profile": PROFILE}, headers=a)
    assert created.status_code == 200, created.text
    assert created.json() == {"profile": PROFILE, "version": 1}
    assert api.put(url, json={"expected_version": 0, "profile": PROFILE},
                   headers=a).json() == created.json()  # idempotent replay
    assert api.put(url, json={"expected_version": 0, "profile": {**PROFILE,
        "service_name": "Deep clean"}}, headers=a).json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    edit = {**PROFILE, "service_name": "Deep clean", "default_duration_minutes": 180}
    edited = api.put(url, json={"expected_version": 1, "profile": edit},
                     headers={**a, "Idempotency-Key": "k2"})
    assert edited.json() == {"profile": edit, "version": 2}
    stale = api.put(url, json={"expected_version": 1, "profile": PROFILE},
                    headers={**a, "Idempotency-Key": "k3"})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_VERSION"
    assert stale.json()["current"] == edited.json()

    for bad in ({**PROFILE, "display_name": "  "}, {**PROFILE, "default_duration_minutes": 0},
                {**PROFILE, "extra": "x"}, {k: v for k, v in PROFILE.items() if k != "service_name"}):
        assert api.put(url, json={"expected_version": 2, "profile": bad},
                       headers={**a, "Idempotency-Key": "bad"}).status_code == 422

    OwnerPolicyService(repository, lambda: NOW).seed("biz-a", "owner-a", "seed")
    too_long = api.put(url, json={"expected_version": 2, "profile": {
        **PROFILE, "default_duration_minutes": 240}}, headers={**a, "Idempotency-Key": "long"})
    assert too_long.status_code == 422

    b = {"Authorization": "Bearer b", "Idempotency-Key": "k1"}
    assert api.get(url, headers=b).status_code == 403
    assert api.put(url, json={"expected_version": 2, "profile": PROFILE}, headers=b).status_code == 403
    assert api.get("/v1/owner/businesses/biz-b/profile", headers=b).json() == {
        "profile": None, "version": 0}
    assert repository.read_business_profile("biz-a").version == 2
