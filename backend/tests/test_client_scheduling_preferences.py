"""Client-specific scheduling preferences: resolution, precedence, isolation (#381).

A scripted interpreter returns the model's answer per exact owner text, so these tests
prove deterministic name resolution, profile binding, precedence and what each client
receives, not natural-language quality (see ``backend/evals``).
"""

from datetime import UTC, datetime, time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.client_api import add_client_session_route
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.domain.recommendations import recommended_starts
from scheduling.domain.scheduling_preferences import (
    ClientReference,
    PreferenceRule,
    PreferenceWindow,
    SchedulingPreferences,
    applicable_rules,
    review_token,
)
from scheduling.identity_links import IdentityLink, LinkRole, LinkState
from scheduling.owner_api import OwnerPrincipal, create_owner_app
from tests.preference_scenarios import (
    CLIENT_NAMES,
    MORNING,
    OVERRIDE,
    SCENARIOS,
    Scenario,
)

MORNING_WINDOW = PreferenceWindow(time(8), time(12))
NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
BASE = "/v1/owner/businesses/biz-a/scheduling-preferences"
OWNER_A = {"Authorization": "Bearer a"}
ZONE = "America/Los_Angeles"
CLIENT_ALDER = {"client_id": "client-1", "name": "Client Alder", "phone_last4": "0001",
                "service_address": "1 Test Street", "shared_name": False}
CLIENT_BIRCH = {**CLIENT_ALDER, "client_id": "client-2", "name": "Client Birch",
                "phone_last4": "0002"}


class Scripted:
    def __init__(self) -> None:
        self.answers: dict[str, dict[str, object]] = {}

    def interpret_scheduling_preferences(self, text: str, timezone: str) -> dict[str, object]:
        return self.answers.get(text, {"rules": [], "problems": [
            {"kind": "unclear", "message": "Say which days or times you prefer."}]})


def profile(business: str, client: str, name: str, phone: str, *, active: bool = True,
            version: int = 1) -> ClientProfile:
    return ClientProfile(business, client, name, phone, "1 Test Street", active, version, NOW, NOW,
                         phone_verified_at=NOW)


class World:
    def __init__(self) -> None:
        self.repository = InMemoryCalendarRepository()
        self.interpreter = Scripted()
        owners = {"a": OwnerPrincipal("owner-a", "biz-a"), "b": OwnerPrincipal("owner-b", "biz-b")}
        app = create_owner_app(self.repository, lambda token: owners[token], lambda: NOW,
                               preference_interpreter=self.interpreter)
        links = {f"token-{n}": IdentityLink("sub", LinkRole.CLIENT, "biz-a", f"client-{n}",
                                            LinkState.ACTIVE, 1, NOW, "admin", "test")
                 for n in (1, 2, 3)}
        add_client_session_route(app, lambda token: links[token], self.repository, lambda: NOW)
        for index, (client, name) in enumerate(CLIENT_NAMES.items(), start=1):
            self.repository.save_profile(
                profile("biz-a", client, name, f"+1555010000{index}"), 0, None)
        self.repository.set_business_profile_for_test("biz-a", 60)
        for business in ("biz-a", "biz-b"):
            OwnerPolicyService(self.repository, lambda: NOW).seed(business, f"owner-{business}", "s")
        self.api = TestClient(app)

    def review(self, text: str, selections: list[dict[str, str]] | None = None) -> dict[str, Any]:
        response = self.api.post(f"{BASE}/review", headers=OWNER_A, json={
            "text": text, "client_selections": selections or []})
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def activate(self, reviewed: dict[str, Any], version: int, key: str = "k") -> Any:
        return self.api.put(BASE, headers={**OWNER_A, "Idempotency-Key": key}, json={
            "expected_version": version, "review_token": reviewed["review_token"]})

    def hours(self, client: int, day: str) -> tuple[set[int], dict[str, Any]]:
        response = self.api.get(f"/v1/client/availability?day={day}",
                                headers={"Authorization": f"Bearer token-{client}"})
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return ({datetime.fromisoformat(v).hour - 7 for v in body["recommended_starts_at"]}, body)


def adopt(world: World, scenario: Scenario) -> None:
    world.interpreter.answers[scenario.text] = scenario.answer
    reviewed = world.review(scenario.text)
    assert reviewed["ready"] is True, reviewed
    assert world.activate(reviewed, 0).status_code == 200


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_shared_scenarios_give_each_client_their_own_recommendations(scenario: Scenario) -> None:
    world = World()
    adopt(world, scenario)
    for client, day, expected in scenario.expectations:
        hours, _ = world.hours(int(client[-1]), day)
        assert hours == set(expected), (client, day)


def test_two_clients_get_different_recommendations_from_the_same_calendar() -> None:
    world = World()
    adopt(world, OVERRIDE)
    alder, body_one = world.hours(1, "2026-10-01")
    birch, body_two = world.hours(2, "2026-10-01")
    assert alder != birch
    assert body_one["starts_at"] == body_two["starts_at"]  # same valid calendar, nothing removed
    assert set(body_one["recommended_starts_at"]) < set(body_one["starts_at"])


def test_review_names_affected_profiles_and_exact_ranges_then_activation_binds_the_ids() -> None:
    world = World()
    world.interpreter.answers[OVERRIDE.text] = OVERRIDE.answer
    reviewed = world.review(OVERRIDE.text)
    assert reviewed["clients"] == [CLIENT_ALDER]
    assert reviewed["rules"][0] == {"weekdays": [0, 1, 2, 3, 4], "windows": [MORNING]}
    assert reviewed["rules"][1]["client_id"] == "client-1"
    assert reviewed["rules"][1]["windows"] == [{"start": "13:00", "end": "16:00"}]
    assert world.api.get(BASE, headers=OWNER_A).json()["active"] is None  # review activates nothing
    done = world.activate(reviewed, 0).json()
    assert done["active"]["clients"] == [CLIENT_ALDER]


def test_a_rename_keeps_the_preference_on_the_same_profile() -> None:
    world = World()
    adopt(world, OVERRIDE)
    before, _ = world.hours(1, "2026-10-01")
    world.repository.save_profile(
        profile("biz-a", "client-1", "Renamed Household", "+15550100001", version=2),
        1, "+15550100001")
    assert world.hours(1, "2026-10-01")[0] == before == {13, 14, 15}
    assert world.hours(2, "2026-10-01")[0] == {8, 9, 10, 11}
    shown = world.api.get(BASE, headers=OWNER_A).json()["active"]["clients"]
    assert shown == [{**CLIENT_ALDER, "name": "Renamed Household"}]
    # The old name no longer matches anyone, and the new name is not what was bound.
    world.interpreter.answers["old"] = {"rules": [{"client_name": "Client Alder", "weekdays": [1],
                                                    "windows": []}], "problems": []}
    assert world.review("old")["problems"][0]["kind"] == "unknown_client"


def test_unknown_names_block_and_never_become_general_guidance() -> None:
    world = World()
    world.interpreter.answers["t"] = {"rules": [
        {"client_name": "Client Zed", "weekdays": [1], "windows": [MORNING]}], "problems": []}
    result = world.review("t")
    assert result["ready"] is False and result["review_token"] is None and result["rules"] == []
    assert [p["kind"] for p in result["problems"]] == ["unknown_client"]
    assert world.activate({"review_token": review_token("t", (PreferenceRule((1,)),), ZONE)},
                          0).status_code == 409
    # Another business's client with the same name is invisible here.
    world.repository.save_profile(profile("biz-b", "other-1", "Client Zed", "+15550109999"), 0, None)
    assert world.review("t")["problems"][0]["kind"] == "unknown_client"


def duplicate_names(world: World) -> dict[str, Any]:
    world.repository.save_profile(
        profile("biz-a", "client-2", "Client Alder", "+15550100002", version=2), 1, "+15550100002")
    world.interpreter.answers["dup"] = {"rules": [
        {"client_name": "client alder", "weekdays": [4], "windows": [MORNING]}], "problems": []}
    return world.review("dup")


def test_ambiguous_names_need_an_explicit_selection_and_a_new_review_per_change() -> None:
    world = World()
    ambiguous = duplicate_names(world)
    assert ambiguous["ready"] is False and ambiguous["review_token"] is None
    assert [p["kind"] for p in ambiguous["problems"]] == ["ambiguous_client"]
    [ask] = ambiguous["ambiguities"]
    assert {c["client_id"] for c in ask["candidates"]} == {"client-1", "client-2"}
    assert {c["phone_last4"] for c in ask["candidates"]} == {"0001", "0002"}

    first = world.review("dup", [{"name": ask["name"], "client_id": "client-1"}])
    second = world.review("dup", [{"name": ask["name"], "client_id": "client-2"}])
    assert first["ready"] and second["ready"]
    assert first["review_token"] != second["review_token"]  # a changed selection is a new review
    assert [(c["client_id"], c["shared_name"], c["phone_last4"]) for c in first["clients"]] == [
        ("client-1", True, "0001")]  # the ready review tells the owner which profile is bound
    assert world.activate(first, 0).status_code == 200
    # Activation applied exactly the first review: only client-1 is guided.
    assert world.hours(1, "2026-10-02")[0] == {8, 9, 10, 11}
    assert world.hours(2, "2026-10-02")[0] == set()


@pytest.mark.parametrize("pick", ["client-9", "client-3", "other-1", ""])
def test_a_selection_that_is_unknown_foreign_or_not_a_candidate_is_refused(pick: str) -> None:
    world = World()
    world.repository.save_profile(profile("biz-b", "other-1", "Client Alder", "+15550108888"), 0, None)
    world.interpreter.answers["dup"] = {"rules": [
        {"client_name": "Client Alder", "weekdays": [4], "windows": [MORNING]}], "problems": []}
    response = world.api.post(f"{BASE}/review", headers=OWNER_A, json={
        "text": "dup", "client_selections": [{"name": "Client Alder", "client_id": pick}]})
    assert response.status_code in (422,)  # unknown/foreign/other-name profile or blank id
    assert world.api.get(BASE, headers=OWNER_A).json()["active"] is None


def test_an_inactive_profile_is_not_a_candidate() -> None:
    world = World()
    world.repository.save_profile(
        profile("biz-a", "client-1", "Client Alder", "+15550100001", active=False, version=2),
        1, "+15550100001")
    world.interpreter.answers["t"] = {"rules": [
        {"client_name": "Client Alder", "weekdays": [4], "windows": []}], "problems": []}
    assert world.review("t")["problems"][0]["kind"] == "unknown_client"


def test_activation_refuses_a_review_whose_client_is_gone_but_replays_an_applied_one() -> None:
    world = World()
    world.interpreter.answers[OVERRIDE.text] = OVERRIDE.answer
    reviewed = world.review(OVERRIDE.text)
    applied = world.activate(reviewed, 0)
    assert applied.status_code == 200
    world.repository.save_profile(
        profile("biz-a", "client-1", "Client Alder", "+15550100001", active=False, version=2),
        1, "+15550100001")
    assert world.activate(reviewed, 0).json() == applied.json()  # safe retry still replays
    other = World()
    other.interpreter.answers[OVERRIDE.text] = OVERRIDE.answer
    stale = other.review(OVERRIDE.text)
    other.repository.save_profile(
        profile("biz-a", "client-1", "Client Alder", "+15550100001", active=False, version=2),
        1, "+15550100001")
    refused = other.activate(stale, 0)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "REVIEW_MISMATCH"
    assert other.api.get(BASE, headers=OWNER_A).json()["active"] is None


def test_same_scope_contradictions_and_unsupported_text_block_client_guidance_too() -> None:
    world = World()
    world.interpreter.answers["t"] = {"rules": [
        {"client_name": "Client Alder", "weekdays": [3], "windows": [MORNING]}], "problems": [
        {"kind": "contradictory", "message": "Alder: Thursday mornings and never Thursday."}]}
    result = world.review("t")
    assert result["ready"] is False and result["rules"] == [] and result["clients"] == []


def test_portal_responses_carry_nothing_about_other_clients_or_owner_text() -> None:
    world = World()
    adopt(world, OVERRIDE)
    HoldService(world.repository).create(
        CreateHold("biz-a", "client-2", "client-2", "hold", datetime(2026, 10, 1, 21,
                   tzinfo=UTC), 60), NOW)
    _, body = world.hours(1, "2026-10-01")
    text = str(body)
    assert set(body) == {"day", "duration_minutes", "starts_at", "recommended_starts_at"}
    for secret in ("Alder", "Birch", "client-", "Prefer", "Thursday"):
        assert secret not in text


def test_nonpreferred_starts_stay_requestable_for_a_client_with_an_exception() -> None:
    world = World()
    adopt(world, OVERRIDE)
    _, body = world.hours(1, "2026-10-01")
    morning = next(v for v in body["starts_at"] if v not in body["recommended_starts_at"])
    sent = world.api.post("/v1/client/requests", json={"start_at": morning}, headers={
        "Authorization": "Bearer token-1", "Idempotency-Key": "r1"})
    assert sent.status_code == 200 and sent.json()["status"] == "PENDING_APPROVAL"


def test_precedence_is_per_weekday_and_never_leaks_between_clients() -> None:
    general = PreferenceRule((0, 1, 2, 3, 4), (MORNING_WINDOW,))
    exception = PreferenceRule((3,), (PreferenceWindow(time(13), time(16)),), "a")
    other = PreferenceRule((1,), (), "b")
    preferences = SchedulingPreferences("t", (general, exception, other), ZONE, review_token(
        "t", (general, exception, other), ZONE))
    assert applicable_rules(preferences, ZONE, "a") == (
        exception, PreferenceRule((0, 1, 2, 4), (MORNING_WINDOW,)))
    assert applicable_rules(preferences, ZONE, "b") == (
        other, PreferenceRule((0, 2, 3, 4), (MORNING_WINDOW,)))
    assert applicable_rules(preferences, ZONE, None) == (general,)
    assert applicable_rules(preferences, "America/New_York", "a") == ()
    # Ranking only flags what it is given; unknown clients get general guidance only.
    start = datetime(2026, 10, 1, 16, tzinfo=UTC)  # Thursday 09:00 PDT
    assert recommended_starts(preferences, ZONE, [start], "a") == frozenset()
    assert recommended_starts(preferences, ZONE, [start], "zzz") == frozenset({start})
    assert ClientReference("a", "A").phone_last4 == ""


def test_a_first_name_or_a_longer_name_needs_confirmation_but_a_whole_name_binds() -> None:
    world = World()
    world.repository.save_profile(
        profile("biz-a", "client-4", "Client Alder Ortiz", "+15550100004"), 0, None)
    for written, kind_or_ready in (("Client Birch", True), ("Birch", "ambiguous_client"),
                                   ("Client Alder", "ambiguous_client")):
        world.interpreter.answers[written] = {"rules": [
            {"client_name": written, "weekdays": [4], "windows": []}], "problems": []}
        result = world.review(written)
        if kind_or_ready is True:
            assert result["ready"] is True
        else:
            assert [p["kind"] for p in result["problems"]] == [kind_or_ready]
    # "Client Alder" is a whole name for client-1 but also part of client-4's: both are offered.
    [ask] = world.review("Client Alder")["ambiguities"]
    assert {c["client_id"] for c in ask["candidates"]} == {"client-1", "client-4"}
    # A lone containment match still shows a one-option picker, and picking it makes it ready.
    [lone] = world.review("Birch")["ambiguities"]
    assert [c["client_id"] for c in lone["candidates"]] == ["client-2"]
    assert world.review("Birch", [{"name": "Birch", "client_id": "client-2"}])["ready"] is True


def test_erasing_a_bound_client_removes_their_rules_text_and_reviews_but_keeps_the_rest() -> None:
    world = World()
    other = Scenario("x", "Mornings; Client Alder Thursday; Client Birch Wednesday", {"rules": [
        {"client_name": "", "weekdays": [0, 1], "windows": [MORNING]},
        {"client_name": "Client Alder", "weekdays": [3], "windows": [MORNING]},
        {"client_name": "Client Birch", "weekdays": [2], "windows": []}], "problems": []}, ())
    adopt(world, other)
    pending = world.review(other.text)  # a stored review that binds the client too
    version = world.api.get(BASE, headers=OWNER_A).json()["version"]
    erased = world.api.delete("/v1/owner/businesses/biz-a/clients/client-1", headers=OWNER_A)
    assert erased.status_code == 204
    active = world.api.get(BASE, headers=OWNER_A).json()
    assert active["version"] == version + 1  # open editors get a stale write
    text = str(active)
    for gone in ("Alder", "client-1", "Thursday mornings"):
        assert gone not in text
    assert "Client Alder" not in active["active"]["text"]
    assert [rule.get("client_id") for rule in active["active"]["rules"]] == [None, "client-2"]
    assert active["active"]["clients"] == [CLIENT_BIRCH]
    assert world.repository.read_preference_review("biz-a", pending["review_token"], NOW) is None
    assert world.hours(2, "2026-09-30")[0] == set(range(8, 17))  # other guidance still active
    stale = world.activate(pending, version)
    assert stale.status_code == 409
    # Erasing the only bound client clears the record rather than leaving empty text.
    world.api.delete("/v1/owner/businesses/biz-a/clients/client-2", headers=OWNER_A)
    after = world.api.get(BASE, headers=OWNER_A).json()["active"]
    assert [rule.get("client_id") for rule in after["rules"]] == [None]
    assert "Birch" not in str(after)
