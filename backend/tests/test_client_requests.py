"""Client appointment requests: identity-derived, revalidated, and the SMS hold path."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.client_api import add_client_session_route
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.expiry import ExpiryService
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.identity_links import IdentityLink, LinkRole, LinkState
from scheduling.owner_api import OwnerPrincipal, create_owner_app

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
START = datetime(2026, 9, 29, 16, tzinfo=UTC)
OWNER = {"Authorization": "Bearer owner"}
BASE = "/v1/owner/businesses/business-1"


class Repository(InMemoryCalendarRepository):
    def __init__(self) -> None:
        super().__init__()
        self.due: list[str] = []

    def due_hold_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
        return tuple(self.due[:limit])


def link(name: str) -> IdentityLink:
    return IdentityLink(f"sub-{name}", LinkRole.CLIENT, "business-1", f"client-{name}",
                        LinkState.ACTIVE, 1, NOW, "synthetic-admin", "test")


LINKS = {"token-a": link("a"), "token-b": link("b")}


def auth(name: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{name}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def setup(verified: bool = True) -> tuple[TestClient, Repository, list[datetime]]:
    repository = Repository()
    clock = [NOW]
    app = create_owner_app(repository, lambda _t: OwnerPrincipal("o", "business-1"),
                           lambda: clock[0])
    add_client_session_route(app, LINKS.__getitem__, repository, lambda: clock[0])
    for name, minutes in (("a", 60), ("b", 120)):
        phone = f"+1555010000{name == 'b' and 2 or 1}"
        repository.save_profile(ClientProfile(
            "business-1", f"client-{name}", "Synthetic Client", phone, "1 Test Street",
            HomeSize.MEDIUM, minutes, True, 1, NOW, NOW,
            NOW if verified else None), 0, None)
    return TestClient(app), repository, clock


def request(api: TestClient, who: str, key: str, start: datetime = START,
            **extra: object) -> object:
    return api.post("/v1/client/requests", headers=auth(who, key),
                    json={"start_at": start.isoformat(), **extra})


def test_request_is_pending_visible_to_owner_and_queues_existing_notices() -> None:
    api, repository, _ = setup()
    response = request(api, "a", "k1")
    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    body = response.json()  # type: ignore[attr-defined]
    assert body["status"] == "PENDING_APPROVAL"
    assert datetime.fromisoformat(body["start_at"]) == START
    assert body["duration_minutes"] == 60  # the profile's length, not caller input
    assert set(body) == {"appointment_id", "status", "start_at", "end_at",
                         "duration_minutes", "hold_expires_at", "requested_at"}
    pending = api.get(f"{BASE}/requests", headers=OWNER).json()
    assert [item["appointment_id"] for item in pending] == [body["appointment_id"]]
    assert pending[0]["client_id"] == "client-a"
    assert [(i.recipient, i.template) for i in repository.list_outbox_intents()] == [
        ("owner", "hold-request"), ("client", "hold-pending")]
    mine = api.get("/v1/client/bookings", headers=auth("a")).json()["bookings"]
    assert [item["appointment_id"] for item in mine] == [body["appointment_id"]]
    assert START.isoformat() not in {
        datetime.fromisoformat(v).isoformat() for v in api.get(
            "/v1/client/availability?day=2026-09-29", headers=auth("a")).json()["starts_at"]}


def test_retry_with_the_same_key_creates_one_request_and_other_command_is_rejected() -> None:
    api, repository, _ = setup()
    first = request(api, "a", "k1").json()  # type: ignore[attr-defined]
    again = request(api, "a", "k1").json()  # type: ignore[attr-defined]
    assert again == first
    assert len(api.get(f"{BASE}/requests", headers=OWNER).json()) == 1
    assert repository.read_revision("business-1") == 1
    reused = request(api, "a", "k1", START + timedelta(hours=3))
    assert reused.status_code == 409  # type: ignore[attr-defined]
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"  # type: ignore[attr-defined]
    assert len(api.get(f"{BASE}/requests", headers=OWNER).json()) == 1
    # Another client's identical key is a different actor, so it is a different command.
    assert request(api, "b", "k1", START + timedelta(hours=5)).status_code == 200  # type: ignore[attr-defined]


def test_stale_choice_returns_current_alternatives_and_no_second_hold() -> None:
    api, repository, _ = setup()
    assert request(api, "a", "k1").status_code == 200  # type: ignore[attr-defined]
    revision = repository.read_revision("business-1")
    stale = request(api, "b", "k2")
    assert stale.status_code == 409  # type: ignore[attr-defined]
    detail = stale.json()["detail"]  # type: ignore[attr-defined]
    assert detail["code"] == "SLOT_CONFLICT"
    alternatives = [datetime.fromisoformat(v) for v in detail["alternatives"]]
    assert 0 < len(alternatives) <= 5 and START not in alternatives
    offered = {datetime.fromisoformat(v) for v in api.get(
        "/v1/client/availability?day=2026-09-29", headers=auth("b")).json()["starts_at"]}
    assert set(alternatives) <= offered
    assert repository.read_revision("business-1") == revision
    assert len(api.get(f"{BASE}/requests", headers=OWNER).json()) == 1
    # Choosing an offered alternative works with a fresh key.
    assert request(api, "b", "k3", alternatives[0]).status_code == 200  # type: ignore[attr-defined]


def test_past_closed_and_off_hours_starts_conflict_without_a_hold() -> None:
    api, repository, _ = setup()
    for start in (NOW - timedelta(hours=1), datetime(2026, 10, 3, 17, tzinfo=UTC),
                  datetime(2026, 9, 29, 4, tzinfo=UTC)):
        assert request(api, "a", f"k-{start}", start).status_code == 409  # type: ignore[attr-defined]
    assert repository.read_revision("business-1") == 0


def test_identity_and_length_come_only_from_the_link_and_profile() -> None:
    api, _, _ = setup()
    for extra in ({"client_id": "client-b"}, {"business_id": "other"},
                  {"duration_minutes": 30}, {"actor_id": "x"}):
        assert request(api, "a", "k1", **extra).status_code == 422  # type: ignore[attr-defined]
    assert api.post("/v1/client/requests", json={"start_at": START.isoformat()},
                    ).status_code == 401
    assert api.post("/v1/client/requests", headers={"Authorization": "Bearer token-a"},
                    json={"start_at": START.isoformat()}).status_code == 422  # no key
    assert api.post("/v1/client/requests", headers=auth("a", "k"),
                    json={"start_at": "2026-09-29T09:00:00"}).status_code == 422  # naive
    assert request(api, "b", "k9", START).json()["duration_minutes"] == 120  # type: ignore[attr-defined]
    assert api.get(f"{BASE}/requests", headers=OWNER).json()[0]["client_id"] == "client-b"


def test_owner_roles_and_unusable_profiles_cannot_request() -> None:
    api, repository, _ = setup()
    LINKS["token-o"] = IdentityLink("sub-o", LinkRole.OWNER, "business-1", None,
                                    LinkState.ACTIVE, 1, NOW, "synthetic-admin", "test")
    try:
        assert request(api, "o", "k").status_code == 403  # type: ignore[attr-defined]
    finally:
        del LINKS["token-o"]
    stored = repository.read_profile("business-1", "client-a")
    assert stored is not None
    repository.save_profile(replace(stored, active=False, version=2), 1, stored.phone_e164)
    assert request(api, "a", "k").status_code == 503  # type: ignore[attr-defined]
    assert repository.read_revision("business-1") == 0


def test_unverified_phone_matches_the_text_path_and_holds_nothing() -> None:
    api, repository, _ = setup(verified=False)
    response = request(api, "a", "k")
    assert response.status_code == 409  # type: ignore[attr-defined]
    assert response.json()["detail"]["code"] == "PROFILE_INCOMPLETE"  # type: ignore[attr-defined]
    assert repository.read_revision("business-1") == 0


@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_owner_decision_moves_the_client_view_from_pending(decision: str) -> None:
    api, _, _ = setup()
    created = request(api, "a", "k1").json()  # type: ignore[attr-defined]
    done = api.post(f"{BASE}/requests/{created['appointment_id']}/{decision}",
                    json={"expected_version": 1},
                    headers={**OWNER, "Idempotency-Key": "d"})
    assert done.status_code == 200, done.text
    mine = api.get("/v1/client/bookings", headers=auth("a")).json()["bookings"]
    if decision == "approve":
        assert [item["status"] for item in mine] == ["CONFIRMED"]
    else:
        assert mine == []
        assert START.isoformat() in {datetime.fromisoformat(v).isoformat() for v in api.get(
            "/v1/client/availability?day=2026-09-29", headers=auth("a")).json()["starts_at"]}


def test_unanswered_request_expires_and_frees_the_time() -> None:
    api, repository, clock = setup()
    created = request(api, "a", "k1").json()  # type: ignore[attr-defined]
    repository.due.append(created["appointment_id"])
    due = datetime.fromisoformat(created["hold_expires_at"])
    assert due > NOW
    clock[0] = due
    result = ExpiryService(repository, LifecycleService(repository, lambda: due),
                           lambda: due).run_once()
    assert result.expired == 1
    appointment = repository.read_appointment(created["appointment_id"])
    assert appointment is not None and appointment.status == CalendarStatus.EXPIRED
    assert api.get("/v1/client/bookings", headers=auth("a")).json() == {"bookings": []}
    assert api.get(f"{BASE}/requests", headers=OWNER).json() == []
    clock[0] = due + timedelta(seconds=1)
    assert request(api, "b", "k2", START + timedelta(days=1)).status_code == 200  # type: ignore[attr-defined]


def test_concurrent_requests_for_one_time_commit_exactly_one() -> None:
    api, repository, _ = setup()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda who: request(api, who, f"race-{who}"), ("a", "b")))
    codes = sorted(item.status_code for item in results)  # type: ignore[attr-defined]
    assert codes == [200, 409]
    assert len(api.get(f"{BASE}/requests", headers=OWNER).json()) == 1
    assert repository.read_revision("business-1") == 1


def test_replay_reports_the_current_state_not_a_stale_pending() -> None:
    api, _, clock = setup()
    created = request(api, "a", "k1").json()  # type: ignore[attr-defined]
    api.post(f"{BASE}/requests/{created['appointment_id']}/decline", json={"expected_version": 1},
             headers={**OWNER, "Idempotency-Key": "d"})
    assert request(api, "a", "k1").json()["status"] == "DECLINED"  # type: ignore[attr-defined]
    other = request(api, "b", "k2", START + timedelta(hours=4)).json()  # type: ignore[attr-defined]
    api.post(f"{BASE}/requests/{other['appointment_id']}/approve", json={"expected_version": 1},
             headers={**OWNER, "Idempotency-Key": "a"})
    assert request(api, "b", "k2", START + timedelta(hours=4)).json()["status"] == "CONFIRMED"  # type: ignore[attr-defined]
    third = request(api, "a", "k3", START + timedelta(days=1)).json()  # type: ignore[attr-defined]
    clock[0] = datetime.fromisoformat(third["hold_expires_at"]) + timedelta(minutes=1)
    # Unswept but past its hold: still not pending approval.
    assert request(api, "a", "k3", START + timedelta(days=1)).json()["status"] == "EXPIRED"  # type: ignore[attr-defined]


def test_portal_keys_cannot_collide_with_the_same_clients_text_keys() -> None:
    api, repository, _ = setup()
    HoldService(repository).create(
        CreateHold("business-1", "client-a", "client-a", "provider-1", START, 60), NOW)
    # The same raw key from the portal is a different command, not a replay or a reuse error.
    response = request(api, "a", "provider-1", START + timedelta(hours=4))
    assert response.status_code == 200  # type: ignore[attr-defined]
    assert HoldService(repository).existing(
        CreateHold("business-1", "client-a", "client-a", "provider-1", START, 60)) is not None


def test_retry_whose_first_request_commits_mid_command_replays_not_conflicts() -> None:
    class Interleaved(Repository):
        """Commits the caller's first request after the retry's key check, before its calendar read."""

        def __init__(self) -> None:
            super().__init__()
            self.first: CreateHold | None = None

        def read_revision(self, business_id: str) -> int:
            if self.first is not None:
                command, self.first = self.first, None
                HoldService(self).create(command, NOW)
            return super().read_revision(business_id)

    _, repository, _ = setup()
    interleaved = Interleaved()
    app = create_owner_app(interleaved, lambda _t: OwnerPrincipal("o", "business-1"), lambda: NOW)
    add_client_session_route(app, LINKS.__getitem__, interleaved, lambda: NOW)
    interleaved.save_profile(repository.read_profile("business-1", "client-a"), 0, None)  # type: ignore[arg-type]
    web = TestClient(app)
    interleaved.first = CreateHold("business-1", "client-a", "client-a", "portal:k1", START, 60)
    response = request(web, "a", "k1")
    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert response.json()["status"] == "PENDING_APPROVAL"  # type: ignore[attr-defined]
    assert interleaved.read_revision("business-1") == 1


def test_busy_calendar_with_a_bad_policy_is_503_not_500() -> None:
    from scheduling.domain.holds import TooManyConflicts
    from scheduling.domain.owner_policy import PolicyNotConfigured

    class Busy(Repository):
        broken = False

        def read_policy(self, business_id: str):  # type: ignore[no-untyped-def]
            if self.broken:
                raise PolicyNotConfigured("gone")
            return super().read_policy(business_id)

    repository = Busy()
    app = create_owner_app(repository, lambda _t: OwnerPrincipal("o", "business-1"), lambda: NOW)
    add_client_session_route(app, LINKS.__getitem__, repository, lambda: NOW)

    def fail(self: HoldService, command: CreateHold, now: datetime) -> None:
        repository.broken = True
        raise TooManyConflicts("busy")

    repository.save_profile(setup()[1].read_profile("business-1", "client-a"), 0, None)  # type: ignore[arg-type]
    original = HoldService.create
    HoldService.create = fail  # type: ignore[method-assign,assignment]
    try:
        assert request(TestClient(app), "a", "k").status_code == 503  # type: ignore[attr-defined]
    finally:
        HoldService.create = original  # type: ignore[method-assign]
