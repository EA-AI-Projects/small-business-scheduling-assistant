"""Client availability and booking routes: identity-scoped, isolated, rule-reusing."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.client_api import add_client_session_route
from scheduling.domain.availability import AvailabilityPolicy
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.owner_policy import PolicyNotConfigured
from scheduling.identity_links import IdentityLink, LinkRole, LinkState
from scheduling.owner_api import OwnerPrincipal, create_owner_app

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
START = datetime(2026, 9, 29, 16, tzinfo=UTC)
DAY = "2026-09-29"
LINKS = {
    "token-a": IdentityLink("sub-a", LinkRole.CLIENT, "business-1", "client-a",
                            LinkState.ACTIVE, 1, NOW, "synthetic-admin", "test"),
    "token-b": IdentityLink("sub-b", LinkRole.CLIENT, "business-1", "client-b",
                            LinkState.ACTIVE, 1, NOW, "synthetic-admin", "test"),
    "token-owner": IdentityLink("sub-o", LinkRole.OWNER, "business-1", None,
                                LinkState.ACTIVE, 1, NOW, "synthetic-admin", "test"),
}


def verify(token: str) -> IdentityLink:
    return LINKS[token]


def auth(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer token-{name}"}


def setup() -> tuple[TestClient, InMemoryCalendarRepository]:
    repository = InMemoryCalendarRepository()
    app = create_owner_app(repository, lambda _t: OwnerPrincipal("o", "business-1"),
                           lambda: NOW)
    add_client_session_route(app, verify, repository, lambda: NOW)
    return TestClient(app), repository


def hold(repository: InMemoryCalendarRepository, client: str, key: str,
         start: datetime = START, business: str = "business-1") -> str:
    return HoldService(repository).create(
        CreateHold(business, client, client, key, start, 60), NOW).hold_id


def starts(api: TestClient, who: str = "a", duration: int = 60) -> list[str]:
    response = api.get(f"/v1/client/availability?day={DAY}&duration_minutes={duration}",
                       headers=auth(who))
    assert response.status_code == 200
    return list(response.json()["starts_at"])


def test_requests_without_valid_client_link_are_denied() -> None:
    api, _ = setup()
    for url in ("/v1/client/availability?day=2026-09-29&duration_minutes=60",
                "/v1/client/bookings"):
        assert api.get(url).status_code == 401
        assert api.get(url, headers=auth("owner")).status_code == 403

    def reject(_token: str) -> IdentityLink:
        raise ValueError("unlinked")

    app = create_owner_app(InMemoryCalendarRepository(),
                           lambda _t: OwnerPrincipal("o", "business-1"), lambda: NOW)
    add_client_session_route(app, reject, InMemoryCalendarRepository(), lambda: NOW)
    assert TestClient(app).get("/v1/client/bookings", headers=auth("a")).status_code == 401


def test_availability_reuses_rules_and_ignores_caller_scope() -> None:
    api, repository = setup()
    assert datetime(2026, 9, 29, 16, tzinfo=UTC).isoformat().replace("+00:00", "Z") in [
        value.replace("+00:00", "Z") for value in starts(api)]
    bad = api.get(f"/v1/client/availability?day={DAY}&duration_minutes=600", headers=auth("a"))
    assert bad.status_code == 422
    # Caller-supplied identity or business scope is rejected, not honored.
    for extra in ("client_id=client-b", "business_id=other"):
        assert api.get(f"/v1/client/availability?day={DAY}&duration_minutes=60&{extra}",
                       headers=auth("a")).status_code == 422
    # Outside the horizon and on a weekend there is nothing to book.
    far = (NOW + timedelta(days=60)).date().isoformat()
    assert api.get(f"/v1/client/availability?day={far}&duration_minutes=60",
                   headers=auth("a")).json()["starts_at"] == []
    assert api.get("/v1/client/availability?day=2026-10-03&duration_minutes=60",
                   headers=auth("a")).json()["starts_at"] == []
    assert repository.read_revision("business-1") == 0


def test_availability_drops_after_hold_and_returns_after_cancellation() -> None:
    api, repository = setup()
    before = starts(api, "b")
    hold_id = hold(repository, "client-a", "hold-a")
    during = starts(api, "b")
    assert set(before) - set(during)
    assert START.isoformat() in {value.replace("Z", "+00:00") for value in before}
    assert START.isoformat() not in {value.replace("Z", "+00:00") for value in during}

    LifecycleService(repository, lambda: NOW).apply(AppointmentCommand(
        "business-1", hold_id, "client-a", ActorRole.CLIENT, Action.CANCEL, "cancel", 1))
    assert starts(api, "b") == before


def test_availability_hides_owner_blocks_without_exposing_them() -> None:
    api, repository = setup()
    baseline = starts(api)
    owner_api = TestClient(api.app)
    snapshot = repository.read_calendar("business-1")
    created = owner_api.post(
        "/v1/owner/businesses/business-1/blocks",
        headers={"Authorization": "Bearer owner", "Idempotency-Key": "block-1"},
        json={"start_at": START.isoformat(), "end_at": (START + timedelta(hours=2)).isoformat(),
              "expected_revision": snapshot.revision})
    assert created.status_code == 200
    after = starts(api)
    assert set(baseline) - set(after)
    response = api.get(f"/v1/client/availability?day={DAY}&duration_minutes=60",
                       headers=auth("a"))
    assert set(response.json()) == {"day", "duration_minutes", "starts_at"}


def test_bookings_list_only_the_callers_pending_and_confirmed_visits() -> None:
    api, repository = setup()
    pending = hold(repository, "client-a", "a-1")
    confirmed = hold(repository, "client-a", "a-2", START + timedelta(hours=3))
    declined = hold(repository, "client-a", "a-3", START + timedelta(hours=6))
    other = hold(repository, "client-b", "b-1", START + timedelta(days=1))
    other_business = hold(repository, "client-a", "x-1", START + timedelta(days=2), "business-2")
    lifecycle = LifecycleService(repository, lambda: NOW)

    def owner(appointment: str, action: Action, key: str) -> None:
        lifecycle.apply(AppointmentCommand("business-1", appointment, "owner-1",
                                           ActorRole.OWNER, action, key, 1))

    owner(confirmed, Action.APPROVE, "approve")
    owner(declined, Action.DECLINE, "decline")

    mine = api.get("/v1/client/bookings", headers=auth("a")).json()["bookings"]
    assert [(item["appointment_id"], item["status"]) for item in mine] == [
        (pending, CalendarStatus.PENDING_APPROVAL.value),
        (confirmed, CalendarStatus.CONFIRMED.value)]
    assert set(mine[0]) == {"appointment_id", "status", "start_at", "end_at",
                            "duration_minutes", "hold_expires_at", "requested_at"}
    assert mine[0]["hold_expires_at"] is not None
    assert mine[1]["duration_minutes"] == 60
    ids = {item["appointment_id"] for item in mine}
    assert not ids & {other, other_business, declined}

    assert api.get("/v1/client/bookings?client_id=client-a",
                   headers=auth("b")).status_code == 422
    theirs = api.get("/v1/client/bookings", headers=auth("b")).json()
    assert [item["appointment_id"] for item in theirs["bookings"]] == [other]


@pytest.mark.parametrize("closing", ["cancel", "expire"])
def test_bookings_drop_cancelled_and_expired_visits(closing: str) -> None:
    api, repository = setup()
    appointment = hold(repository, "client-a", "a-1")
    assert len(api.get("/v1/client/bookings", headers=auth("a")).json()["bookings"]) == 1
    if closing == "cancel":
        LifecycleService(repository, lambda: NOW).apply(AppointmentCommand(
            "business-1", appointment, "client-a", ActorRole.CLIENT, Action.CANCEL, "c", 1))
    else:
        late = NOW + timedelta(days=2)
        api2 = create_owner_app(repository, lambda _t: OwnerPrincipal("o", "business-1"),
                                lambda: late)
        add_client_session_route(api2, verify, repository, lambda: late)
        assert TestClient(api2).get("/v1/client/bookings", headers=auth("a")).json() == {
            "bookings": []}
        return
    assert api.get("/v1/client/bookings", headers=auth("a")).json() == {"bookings": []}


def test_missing_policy_returns_503_without_detail() -> None:
    class NoPolicy(InMemoryCalendarRepository):
        def read_policy(self, business_id: str) -> AvailabilityPolicy:
            raise PolicyNotConfigured("Persist the pilot policy before booking")

    repository = NoPolicy()
    app = create_owner_app(repository, lambda _t: OwnerPrincipal("o", "business-1"),
                           lambda: NOW)
    add_client_session_route(app, verify, repository, lambda: NOW)
    response = TestClient(app).get(
        f"/v1/client/availability?day={DAY}&duration_minutes=60", headers=auth("a"))
    assert response.status_code == 503
    assert response.json() == {"detail": "Booking is unavailable"}
