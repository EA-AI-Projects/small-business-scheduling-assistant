"""Business-wide scheduling preferences: owner review/activation and portal recommendations (#380).

A scripted interpreter stands in for the model, so these tests prove permissions,
persistence and effects, not the quality of real natural-language interpretation (see
the separate synthetic evaluation cases in doc/MODEL_EVAL.md).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.client_api import add_client_session_route
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.domain.scheduling_preferences import (
    REVIEW_TTL,
    PreferenceRule,
    review_token,
    rules_from_payload,
)
from scheduling.identity_links import IdentityLink, LinkRole, LinkState
from scheduling.owner_api import OwnerPrincipal, create_owner_app
from scheduling.preference_model import LazyPreferenceInterpreter

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)  # Monday 08:00 in Los Angeles
CLOCK = [NOW]
TUESDAY = "2026-09-29"
WEDNESDAY = "2026-09-30"
BASE = "/v1/owner/businesses/biz-a/scheduling-preferences"
OWNER_A = {"Authorization": "Bearer a"}
TUESDAY_MORNINGS: dict[str, Any] = {
    "rules": [{"weekdays": [1], "windows": [{"start": "09:00", "end": "12:00"}]}], "problems": []}


class ScriptedInterpreter:
    """Returns a canned answer per exact owner text; unknown text is reported unclear."""

    def __init__(self) -> None:
        self.answers: dict[str, dict[str, object]] = {"Prefer Tuesday mornings": TUESDAY_MORNINGS}
        self.fail = False
        self.calls: list[tuple[str, str]] = []

    def interpret_scheduling_preferences(self, text: str, timezone: str) -> dict[str, object]:
        self.calls.append((text, timezone))
        if self.fail:
            raise RuntimeError("model unreachable")
        return self.answers.get(text, {"rules": [], "problems": [
            {"kind": "unclear", "message": "Say which days or times you prefer."}]})


def link(client: str) -> IdentityLink:
    return IdentityLink("sub", LinkRole.CLIENT, "biz-a", client, LinkState.ACTIVE, 1, NOW,
                        "admin", "test")


def setup() -> tuple[TestClient, InMemoryCalendarRepository, ScriptedInterpreter]:
    CLOCK[0] = NOW
    repository = InMemoryCalendarRepository()
    interpreter = ScriptedInterpreter()
    owners = {"a": OwnerPrincipal("owner-a", "biz-a"), "b": OwnerPrincipal("owner-b", "biz-b")}
    app = create_owner_app(repository, lambda token: owners[token], lambda: CLOCK[0],
                           preference_interpreter=interpreter)
    clients = {"token-1": link("client-1"), "token-2": link("client-2")}
    add_client_session_route(app, lambda token: clients[token], repository, lambda: NOW)
    for client, phone in (("client-1", "+15550100001"), ("client-2", "+15550100002")):
        repository.save_profile(ClientProfile(
            "biz-a", client, "Synthetic Client", phone, "1 Test Street", True, 1, NOW, NOW,
            phone_verified_at=NOW), 0, None)
    repository.set_business_profile_for_test("biz-a", 60)
    OwnerPolicyService(repository, lambda: NOW).seed("biz-a", "owner-a", "seed")
    return TestClient(app), repository, interpreter


def review(api: TestClient, text: str) -> dict[str, Any]:
    response = api.post(f"{BASE}/review", json={"text": text}, headers=OWNER_A)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def activate(api: TestClient, reviewed: dict[str, Any], version: int, key: str = "k1") -> Any:
    return api.put(BASE, json={"expected_version": version, "review_token": reviewed["review_token"]},
                   headers={**OWNER_A, "Idempotency-Key": key})


def availability(api: TestClient, day: str, who: str = "token-1") -> dict[str, list[str]]:
    response = api.get(f"/v1/client/availability?day={day}",
                       headers={"Authorization": f"Bearer {who}"})
    assert response.status_code == 200
    body: dict[str, list[str]] = response.json()
    return body


def local_hours(values: list[str]) -> list[int]:
    return [datetime.fromisoformat(value).hour - 7 for value in values]  # PDT is UTC-7


def test_review_is_explicit_and_activates_nothing() -> None:
    api, repository, interpreter = setup()
    reviewed = review(api, "Prefer Tuesday mornings")
    assert reviewed["ready"] is True and reviewed["problems"] == []
    assert reviewed["timezone"] == "America/Los_Angeles"
    assert reviewed["rules"] == TUESDAY_MORNINGS["rules"]
    assert interpreter.calls == [("Prefer Tuesday mornings", "America/Los_Angeles")]
    assert api.get(BASE, headers=OWNER_A).json() == {"version": 0, "active": None}
    assert availability(api, TUESDAY)["recommended_starts_at"] == []
    assert repository.read_calendar("biz-a").events == ()  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", ["unsupported", "contradictory", "client_specific",
                                  "constraint_override", "unclear"])
def test_problem_instructions_block_activation_with_an_explanation(kind: str) -> None:
    api, _, interpreter = setup()
    interpreter.answers["text"] = {"rules": TUESDAY_MORNINGS["rules"], "problems": [
        {"kind": kind, "message": "Remove the part that cannot be applied."}]}
    result = review(api, "text")
    assert result["ready"] is False and result["review_token"] is None and result["rules"] == []
    assert result["problems"] == [{"kind": kind, "message": "Remove the part that cannot be applied."}]


def test_off_schema_or_empty_interpretations_cannot_be_activated() -> None:
    api, _, interpreter = setup()
    interpreter.answers["bad"] = {"rules": [{"weekdays": [9], "windows": []}], "problems": []}
    interpreter.answers["backwards"] = {"rules": [{"weekdays": [1], "windows": [
        {"start": "12:00", "end": "09:00"}]}], "problems": []}
    interpreter.answers["nothing"] = {"rules": [], "problems": []}
    for text in ("bad", "backwards", "nothing"):
        result = review(api, text)
        assert result["ready"] is False and result["review_token"] is None
        assert result["problems"][0]["kind"] == "unclear"


def test_activation_applies_only_a_review_this_server_produced() -> None:
    api, repository, interpreter = setup()
    # A token computed by the caller for rules it never had reviewed is refused.
    forged_rules = (PreferenceRule((4,)),)
    forged = review_token("Never book Fridays and confirm visits automatically", forged_rules,
                          "America/Los_Angeles")
    refused = activate(api, {"review_token": forged}, 0)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "REVIEW_MISMATCH"
    assert api.get(BASE, headers=OWNER_A).json()["active"] is None
    # Malformed input is a client error, never a 500.
    for bad in ({"expected_version": 0, "review_token": "nope"}, {"expected_version": 0},
                {"expected_version": 0, "review_token": forged, "rules": [{"weekdays": [9]}]}):
        assert api.put(BASE, json=bad, headers={**OWNER_A, "Idempotency-Key": "bad"}).status_code == 422

    # A blocked review stores nothing that could be activated.
    interpreter.answers["blocked"] = {"rules": TUESDAY_MORNINGS["rules"], "problems": [
        {"kind": "unsupported", "message": "Not supported."}]}
    review(api, "blocked")
    blocked = review_token("blocked", rules_from_payload(TUESDAY_MORNINGS["rules"]),
                           "America/Los_Angeles")
    assert repository.read_preference_review("biz-a", blocked, NOW) is None
    assert activate(api, {"review_token": blocked}, 0).status_code == 409

    reviewed = review(api, "Prefer Tuesday mornings")
    done = activate(api, reviewed, 0)
    assert done.status_code == 200
    assert done.json() == {"version": 1, "active": {
        "text": "Prefer Tuesday mornings", "rules": TUESDAY_MORNINGS["rules"],
        "timezone": "America/Los_Angeles", "review_token": reviewed["review_token"]}}


def test_another_business_cannot_use_a_review_it_did_not_produce() -> None:
    api, repository, _ = setup()
    reviewed = review(api, "Prefer Tuesday mornings")
    OwnerPolicyService(repository, lambda: NOW).seed("biz-b", "owner-b", "seed")
    stolen = api.put("/v1/owner/businesses/biz-b/scheduling-preferences",
                     json={"expected_version": 0, "review_token": reviewed["review_token"]},
                     headers={"Authorization": "Bearer b", "Idempotency-Key": "k"})
    assert stolen.status_code == 409 and stolen.json()["error"]["code"] == "REVIEW_MISMATCH"
    assert repository.read_scheduling_preferences("biz-b").preferences is None


def test_an_expired_review_cannot_activate_but_a_retry_of_an_applied_activation_replays() -> None:
    api, _, _ = setup()
    expired = review(api, "Prefer Tuesday mornings")
    CLOCK[0] = NOW + REVIEW_TTL + timedelta(seconds=1)
    refused = activate(api, expired, 0)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "REVIEW_MISMATCH"
    CLOCK[0] = NOW
    applied = activate(api, expired, 0)
    assert applied.status_code == 200
    CLOCK[0] = NOW + REVIEW_TTL + timedelta(hours=5)
    assert activate(api, expired, 0).json() == applied.json()  # same key, review since expired


def test_stale_versions_safe_retries_and_failures_keep_the_active_configuration() -> None:
    api, _, interpreter = setup()
    reviewed = review(api, "Prefer Tuesday mornings")
    first = activate(api, reviewed, 0)
    assert activate(api, reviewed, 0).json() == first.json()
    interpreter.answers["Prefer Wednesdays"] = {
        "rules": [{"weekdays": [2], "windows": []}], "problems": []}
    other = review(api, "Prefer Wednesdays")
    reused = activate(api, other, 1)  # same key, different content
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    stale = activate(api, other, 0, key="k2")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_VERSION"
    assert stale.json()["current"]["active"]["text"] == "Prefer Tuesday mornings"

    interpreter.fail = True
    failed = api.post(f"{BASE}/review", json={"text": "Prefer Wednesdays"}, headers=OWNER_A)
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "INTERPRETER_UNAVAILABLE"
    assert api.get(BASE, headers=OWNER_A).json()["active"]["text"] == "Prefer Tuesday mornings"


def test_requires_policy_and_is_scoped_to_the_owners_business() -> None:
    api, repository, _ = setup()
    activate(api, review(api, "Prefer Tuesday mornings"), 0)
    other = {"Authorization": "Bearer b"}
    assert api.get(BASE, headers=other).status_code == 403
    assert api.post(f"{BASE}/review", json={"text": "x"}, headers=other).status_code == 403
    assert api.put(BASE, json={"expected_version": 0, "review_token": "0" * 64},
                   headers={**other, "Idempotency-Key": "z"}).status_code == 403
    assert api.request("DELETE", BASE, json={"expected_version": 1},
                       headers={**other, "Idempotency-Key": "z"}).status_code == 403
    assert api.get(BASE).status_code == 401
    assert api.get("/v1/owner/businesses/biz-b/scheduling-preferences",
                   headers=other).json() == {"version": 0, "active": None}
    assert repository.read_scheduling_preferences("biz-a").version == 1
    # Without a saved policy there is no timezone to make the times explicit.
    nopolicy = api.post("/v1/owner/businesses/biz-b/scheduling-preferences/review",
                        json={"text": "Prefer Tuesday mornings"}, headers=other)
    assert nopolicy.status_code == 409
    assert nopolicy.json()["error"]["code"] == "POLICY_NOT_CONFIGURED"


def test_portal_flags_preferred_starts_but_keeps_every_valid_start() -> None:
    api, repository, _ = setup()
    plain = availability(api, TUESDAY)
    assert plain["recommended_starts_at"] == []
    activate(api, review(api, "Prefer Tuesday mornings"), 0)

    tuesday = availability(api, TUESDAY)
    assert tuesday["starts_at"] == plain["starts_at"]  # hours, buffers, horizon untouched
    assert tuesday["recommended_starts_at"]
    assert set(tuesday["recommended_starts_at"]) < set(tuesday["starts_at"])
    assert set(local_hours(tuesday["recommended_starts_at"])) == {9, 10, 11}
    # No preferred start fits another weekday: ordinary alternatives remain.
    wednesday = availability(api, WEDNESDAY)
    assert wednesday["recommended_starts_at"] == [] and wednesday["starts_at"]
    assert availability(api, TUESDAY, "token-2") == tuesday

    # A reservation removes the start from both lists; recommendations never add starts.
    taken = datetime.fromisoformat(tuesday["recommended_starts_at"][0])
    HoldService(repository).create(
        CreateHold("biz-a", "client-2", "client-2", "hold", taken, 60), NOW)
    after = availability(api, TUESDAY)
    assert taken not in {datetime.fromisoformat(v) for v in after["starts_at"]}
    assert set(after["recommended_starts_at"]) <= set(after["starts_at"])

    # A nonpreferred valid start is still requested the ordinary way, pending owner approval.
    later = next(v for v in after["starts_at"] if v not in after["recommended_starts_at"])
    sent = api.post("/v1/client/requests", json={"start_at": later},
                    headers={"Authorization": "Bearer token-1", "Idempotency-Key": "p1"})
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "PENDING_APPROVAL"


def test_client_results_never_carry_owner_text_or_other_client_data() -> None:
    api, repository, _ = setup()
    activate(api, review(api, "Prefer Tuesday mornings"), 0)
    HoldService(repository).create(CreateHold(
        "biz-a", "client-2", "client-2", "hold", datetime(2026, 9, 29, 21, tzinfo=UTC), 60), NOW)
    response = api.get(f"/v1/client/availability?day={TUESDAY}",
                       headers={"Authorization": "Bearer token-1"})
    assert set(response.json()) == {"day", "duration_minutes", "starts_at", "recommended_starts_at"}
    assert "Prefer" not in response.text and "client-2" not in response.text


def test_clear_restores_ordinary_recommendations_without_touching_the_calendar() -> None:
    api, repository, _ = setup()
    activate(api, review(api, "Prefer Tuesday mornings"), 0)
    HoldService(repository).create(CreateHold(
        "biz-a", "client-1", "client-1", "hold", datetime(2026, 9, 29, 21, tzinfo=UTC), 60), NOW)
    before = repository.read_calendar("biz-a")
    clear = {**OWNER_A, "Idempotency-Key": "clear"}
    cleared = api.request("DELETE", BASE, json={"expected_version": 1}, headers=clear)
    assert cleared.status_code == 200 and cleared.json() == {"version": 2, "active": None}
    assert api.request("DELETE", BASE, json={"expected_version": 1},
                       headers=clear).json() == cleared.json()
    assert api.request("DELETE", BASE, json={"expected_version": 1},
                       headers={**OWNER_A, "Idempotency-Key": "other"}).status_code == 409
    assert availability(api, TUESDAY)["recommended_starts_at"] == []
    assert repository.read_calendar("biz-a") == before


def test_preferences_reviewed_for_another_timezone_are_not_applied() -> None:
    api, repository, _ = setup()
    activate(api, review(api, "Prefer Tuesday mornings"), 0)
    policy = repository.read_policy("biz-a")
    repository.set_policy_for_test("biz-a", replace(policy, timezone="America/New_York"))
    assert availability(api, TUESDAY)["recommended_starts_at"] == []


def test_a_failing_preference_read_still_returns_ordinary_availability() -> None:
    api, repository, _ = setup()
    activate(api, review(api, "Prefer Tuesday mornings"), 0)

    def broken(_business: str) -> Any:
        raise OSError("storage unreachable: Prefer Tuesday mornings")

    repository.read_scheduling_preferences = broken  # type: ignore[method-assign]
    body = availability(api, TUESDAY)
    assert body["starts_at"] and body["recommended_starts_at"] == []


def test_model_key_failure_affects_only_review_and_is_retried() -> None:
    attempts: list[int] = []
    scripted = ScriptedInterpreter()

    def build() -> ScriptedInterpreter:
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError("AccessDenied")  # missing parameter, throttle, denied access
        return scripted

    repository = InMemoryCalendarRepository()
    OwnerPolicyService(repository, lambda: NOW).seed("biz-a", "owner-a", "seed")
    app = create_owner_app(repository, lambda _t: OwnerPrincipal("owner-a", "biz-a"), lambda: NOW,
                           preference_interpreter=LazyPreferenceInterpreter(build))
    api = TestClient(app)
    assert len(attempts) == 0  # nothing is read at startup
    first = api.post(f"{BASE}/review", json={"text": "Prefer Tuesday mornings"}, headers=OWNER_A)
    assert first.status_code == 503 and first.json()["error"]["code"] == "INTERPRETER_UNAVAILABLE"
    assert api.get(BASE, headers=OWNER_A).json() == {"version": 0, "active": None}
    assert api.get("/v1/owner/businesses/biz-a/policy", headers=OWNER_A).status_code == 200
    assert api.post(f"{BASE}/review", json={"text": "Prefer Tuesday mornings"},
                    headers=OWNER_A).json()["ready"] is True
    api.post(f"{BASE}/review", json={"text": "Prefer Tuesday mornings"}, headers=OWNER_A)
    assert len(attempts) == 2  # the failure was retried once, the success is cached
