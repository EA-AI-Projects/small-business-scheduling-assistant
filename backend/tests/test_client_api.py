"""Client availability and booking routes: identity-scoped, isolated, rule-reusing."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.client_api import add_client_session_route
from scheduling.domain.availability import AvailabilityPolicy
from scheduling.domain.business_profile import BusinessProfileRecord
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile
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
    for client in ("client-a", "client-b"):
        save_profile(repository, client, 60)
    repository.set_business_profile_for_test("business-1", 60)
    return TestClient(app), repository


def save_profile(repository: InMemoryCalendarRepository, client: str, minutes: int,
                 active: bool = True) -> None:
    repository.save_profile(ClientProfile(
        "business-1", client, "Synthetic Client", "+15550100001" if client == "client-a"
        else "+15550100002", "1 Test Street", active, 1, NOW, NOW),
        0, None)


def hold(repository: InMemoryCalendarRepository, client: str, key: str,
         start: datetime = START, business: str = "business-1") -> str:
    return HoldService(repository).create(
        CreateHold(business, client, client, key, start, 60), NOW).hold_id


def starts(api: TestClient, who: str = "a") -> list[str]:
    response = api.get(f"/v1/client/availability?day={DAY}",
                       headers=auth(who))
    assert response.status_code == 200
    return list(response.json()["starts_at"])


def test_requests_without_valid_client_link_are_denied() -> None:
    api, _ = setup()
    for url in ("/v1/client/availability?day=2026-09-29",
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
    bad = api.get(f"/v1/client/availability?day={DAY}0", headers=auth("a"))
    assert bad.status_code == 422
    # Caller-supplied identity or business scope is rejected, not honored.
    for extra in ("client_id=client-b", "business_id=other"):
        assert api.get(f"/v1/client/availability?day={DAY}&{extra}",
                       headers=auth("a")).status_code == 422
    # Outside the horizon and on a weekend there is nothing to book.
    far = (NOW + timedelta(days=60)).date().isoformat()
    assert api.get(f"/v1/client/availability?day={far}",
                   headers=auth("a")).json()["starts_at"] == []
    assert api.get("/v1/client/availability?day=2026-10-03",
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
    response = api.get(f"/v1/client/availability?day={DAY}",
                       headers=auth("a"))
    assert set(response.json()) == {"day", "duration_minutes", "starts_at",
                                    "recommended_starts_at"}


def test_availability_uses_the_business_default_duration_for_every_client() -> None:
    api, repository = setup()
    for who in ("a", "b"):
        assert api.get(f"/v1/client/availability?day={DAY}",
                       headers=auth(who)).json()["duration_minutes"] == 60
    short_day = starts(api, "a")
    repository.set_business_profile_for_test("business-1", 180)
    for who in ("a", "b"):
        long_day = api.get(f"/v1/client/availability?day={DAY}", headers=auth(who)).json()
        assert long_day["duration_minutes"] == 180
        assert len(long_day["starts_at"]) < len(short_day)


def test_availability_is_unavailable_until_the_business_profile_is_saved() -> None:
    _, repository = setup()
    unconfigured = InMemoryCalendarRepository()
    unconfigured.save_profile(repository.read_profile(  # type: ignore[arg-type]
        "business-1", "client-a"), 0, None)
    app = create_owner_app(unconfigured, lambda _t: OwnerPrincipal("o", "business-1"),
                           lambda: NOW)
    add_client_session_route(app, verify, unconfigured, lambda: NOW)
    response = TestClient(app).get(f"/v1/client/availability?day={DAY}", headers=auth("a"))
    assert response.status_code == 503
    assert response.json() == {"detail": "Booking is unavailable"}


@pytest.mark.parametrize("state", ["missing", "inactive"])
def test_availability_unavailable_without_a_usable_profile(state: str) -> None:
    api, repository = setup()
    if state == "missing":
        client = TestClient(api.app)
        link = IdentityLink("sub-c", LinkRole.CLIENT, "business-1", "client-c",
                            LinkState.ACTIVE, 1, NOW, "synthetic-admin", "test")
        LINKS["token-c"] = link
        try:
            response = client.get(f"/v1/client/availability?day={DAY}", headers=auth("c"))
        finally:
            del LINKS["token-c"]
    else:
        repository.save_profile(ClientProfile(
            "business-1", "client-a", "Synthetic Client", "+15550100001", "1 Test Street",
            False, 2, NOW, NOW), 1, "+15550100001")
        response = api.get(f"/v1/client/availability?day={DAY}", headers=auth("a"))
    assert response.status_code == 503
    assert response.json() == {"detail": "Booking is unavailable"}


def test_session_reports_the_business_time_zone() -> None:
    api, _ = setup()
    body = api.get("/v1/client/session", headers=auth("a")).json()
    assert body["timezone"] == "America/Los_Angeles"
    assert body["booking_horizon_days"] == 14
    assert body["client_id"] == "client-a"


def test_session_reports_the_saved_business_and_service_names() -> None:
    api, repository = setup()
    repository.set_business_profile_for_test("business-1", 60, "Example Pet Care", "Dog grooming")
    body = api.get("/v1/client/session", headers=auth("a")).json()
    assert (body["business_name"], body["service_name"]) == ("Example Pet Care", "Dog grooming")
    repository._profiles["business-1"] = BusinessProfileRecord(None, 0)
    unsaved = api.get("/v1/client/session", headers=auth("a")).json()
    assert (unsaved["business_name"], unsaved["service_name"]) == (None, None)


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
                            "duration_minutes", "hold_expires_at", "requested_at", "version",
                         "replaces_appointment_id"}
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
        f"/v1/client/availability?day={DAY}", headers=auth("a"))
    assert response.status_code == 503
    assert response.json() == {"detail": "Booking is unavailable"}


def test_portal_reminder_preference_is_shared_versioned_and_identity_scoped() -> None:
    api, _ = setup()
    path = "/v1/client/reminder-preference"
    owner_path = "/v1/owner/businesses/business-1/clients/client-a/reminder-preference"
    state = api.get(path, headers=auth("a"))
    assert state.status_code == 200
    assert state.json()["enabled"] is False
    assert api.get(path).status_code == 401
    assert api.get(path, headers=auth("owner")).status_code == 403
    assert api.get(path + "?client_id=client-b", headers=auth("a")).status_code == 422
    for extra in ({"client_id": "client-b"}, {"business_id": "other"}):
        assert api.put(path, json={"enabled": True, "expected_version": 1, **extra},
                       headers=auth("a")).status_code == 422
    saved = api.put(path, json={"enabled": True, "expected_version": 1}, headers=auth("a"))
    assert saved.status_code == 200, saved.text
    assert saved.json()["enabled"] is True
    assert "consent_unavailable" in saved.json()["blockers"]
    assert api.get(owner_path, headers=auth("owner")).json() == saved.json()
    assert api.get(path, headers=auth("b")).json()["enabled"] is False
    assert api.put(path, json={"enabled": False, "expected_version": 1},
                   headers=auth("a")).status_code == 409
    off = api.put(owner_path, json={"enabled": False, "expected_version": 2},
                  headers=auth("owner"))
    assert off.status_code == 200
    assert api.get(path, headers=auth("a")).json() == off.json()


def test_portal_enrollment_preserves_phone_consent_and_stop_restrictions() -> None:
    from scheduling.local_texts import LocalConsent

    api, repository = setup()
    app = create_owner_app(repository, lambda _t: OwnerPrincipal("o", "business-1"), lambda: NOW)

    class OptedOutConsent(LocalConsent):
        def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
            return True

    add_client_session_route(app, verify, repository, lambda: NOW,
                             sms_store=OptedOutConsent(repository))  # type: ignore[arg-type]
    api = TestClient(app)
    path = "/v1/client/reminder-preference"
    enrolled = api.put(path, json={"enabled": True, "expected_version": 1}, headers=auth("a"))
    assert enrolled.status_code == 200
    assert enrolled.json()["blockers"] == ["phone_unverified", "consent_missing", "sms_opted_out"]
    profile = api.get("/v1/owner/businesses/business-1/clients/client-a", headers=auth("owner")).json()
    assert profile["phone_verified_at"] is None
    off = api.put(path, json={"enabled": False, "expected_version": 2}, headers=auth("a"))
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    assert off.json()["blockers"] == enrolled.json()["blockers"]
    assert api.put(path, json={"enabled": "true", "expected_version": 3}, headers=auth("a")).status_code == 422
    assert api.delete("/v1/owner/businesses/business-1/clients/client-a", headers=auth("owner")).status_code == 204
    assert api.get(path, headers=auth("a")).status_code == 404
    assert api.put(path, json={"enabled": True, "expected_version": 3}, headers=auth("a")).status_code == 404


def test_portal_timing_is_separate_validated_partial_and_identity_scoped() -> None:
    api, _ = setup()
    path = "/v1/client/reminder-preference"
    owner_path = "/v1/owner/businesses/business-1/clients/client-a/reminder-preference"
    first = api.get(path, headers=auth("a")).json()
    assert (first["days_before"], first["local_time"], first["enabled"]) == (1, "10:00:00", False)
    assert first["timezone"]
    timing = api.put(path, headers=auth("a"), json={
        "expected_version": first["version"], "days_before": 3, "local_time": "17:59"})
    assert timing.status_code == 200, timing.text
    assert (timing.json()["enabled"], timing.json()["days_before"],
            timing.json()["local_time"]) == (False, 3, "17:59:00")
    version = timing.json()["version"]
    on = api.put(path, headers=auth("a"), json={"expected_version": version, "enabled": True})
    assert (on.json()["days_before"], on.json()["local_time"]) == (3, "17:59:00")
    part = api.put(path, headers=auth("a"), json={
        "expected_version": on.json()["version"], "local_time": "18:00"})
    assert part.json()["enabled"] is True and part.json()["days_before"] == 3
    off = api.put(path, headers=auth("a"), json={
        "expected_version": part.json()["version"], "enabled": False})
    assert off.json()["local_time"] == "18:00:00" and off.json()["days_before"] == 3
    assert api.get(owner_path, headers=auth("owner")).json() == off.json()
    stale = api.put(path, headers=auth("a"), json={"expected_version": version, "days_before": 5})
    assert stale.status_code == 409
    assert api.get(path, headers=auth("a")).json() == off.json()
    current = off.json()["version"]
    for bad in ({"days_before": 0}, {"days_before": 8}, {"days_before": "2"}, {"days_before": True},
                {"local_time": "07:59"}, {"local_time": "18:01"}, {"local_time": "10:00:30"},
                {"client_id": "client-b"}):
        refused = api.put(path, headers=auth("a"), json={"expected_version": current, **bad})
        assert refused.status_code == 422, bad
    assert api.get(path, headers=auth("a")).json() == off.json()
    untouched = api.get(path, headers=auth("b")).json()
    assert (untouched["days_before"], untouched["local_time"]) == (1, "10:00:00")
