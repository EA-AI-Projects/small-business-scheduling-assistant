"""Client-scoped reads expose bookable starts and the caller's own bookings only."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.client_api import add_client_portal_routes
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.holds import CreateHold, HoldService, PendingHold
from scheduling.identity_links import IdentityLink, LinkRole, LinkState

NOW = datetime(2026, 10, 12, 15, tzinfo=UTC)  # Monday 8:00 in the pilot timezone
DAY = date(2026, 10, 13)
BOOKED = datetime(2026, 10, 13, 17, tzinfo=UTC)  # 10:00 local
URL = "/v1/client/availability"
BOOKINGS = "/v1/client/bookings"


def profile(client_id: str, phone: str, *, active: bool = True) -> ClientProfile:
    return ClientProfile("pilot", client_id, f"Synthetic {client_id}", phone,
                         "1 Synthetic Way", HomeSize.MEDIUM, 60, active, 1, NOW, NOW)


def link(subject: str, role: LinkRole, client_id: str | None) -> IdentityLink:
    return IdentityLink(subject, role, "pilot", client_id, LinkState.ACTIVE, 1, NOW,
                        "synthetic-admin", "test")


class Setup:
    def __init__(self) -> None:
        self.repository = InMemoryCalendarRepository()
        self.now = NOW
        self.links = {
            "sub-1": link("sub-1", LinkRole.CLIENT, "client-1"),
            "sub-2": link("sub-2", LinkRole.CLIENT, "client-2"),
            "sub-gone": link("sub-gone", LinkRole.CLIENT, "client-gone"),
            "sub-off": link("sub-off", LinkRole.CLIENT, "client-off"),
            "sub-owner": link("sub-owner", LinkRole.OWNER, None),
        }
        for client_id, phone, active in (("client-1", "+15555550101", True),
                                         ("client-2", "+15555550102", True),
                                         ("client-off", "+15555550109", False)):
            self.repository.save_profile(profile(client_id, phone, active=active), 0, None)
        app = FastAPI()

        def verify(token: str) -> IdentityLink:
            return self.links[token]

        add_client_portal_routes(app, verify, self.repository, lambda: self.now)
        self.api = TestClient(app)

    def hold(self, client_id: str, key: str, start: datetime = BOOKED) -> PendingHold:
        return HoldService(self.repository).create(
            CreateHold("pilot", client_id, client_id, key, start, 60), self.now)


def bearer(subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {subject}"}


def starts(setup: Setup, subject: str = "sub-1") -> set[datetime]:
    response = setup.api.get(URL, params={"date": DAY.isoformat()}, headers=bearer(subject))
    assert response.status_code == 200
    return {datetime.fromisoformat(value) for value in response.json()["starts"]}


def test_availability_uses_profile_duration_and_exposes_only_starts() -> None:
    setup = Setup()
    response = setup.api.get(URL, params={"date": DAY.isoformat()}, headers=bearer("sub-1"))

    assert response.json().keys() == {"date", "timezone", "duration_minutes", "starts"}
    assert response.json()["duration_minutes"] == 60
    assert BOOKED in starts(setup)
    assert datetime(2026, 10, 13, 15, tzinfo=UTC) in starts(setup)  # 08:00 local


def test_held_and_cancelled_times_change_availability_without_leaking_why() -> None:
    setup = Setup()
    hold = setup.hold("client-2", "hold-1")

    after_hold = starts(setup)
    assert BOOKED not in after_hold
    assert BOOKED - timedelta(minutes=30) not in after_hold  # inside the visit gap
    # The unavailable window is the same for the holder and any other client.
    assert starts(setup, "sub-2") == after_hold

    setup.now = hold.hold_expires_at + timedelta(minutes=1)
    # Expired hold frees the time again, and the next hold reuses it.
    assert BOOKED in starts(setup)


def test_bookings_return_only_the_callers_own_with_expiry_derived() -> None:
    setup = Setup()
    own = setup.hold("client-1", "own-1")
    other = setup.hold("client-2", "other-1", BOOKED + timedelta(hours=3))

    body = setup.api.get(BOOKINGS, headers=bearer("sub-1")).json()
    assert [item["appointment_id"] for item in body["bookings"]] == [own.hold_id]
    assert body["bookings"][0]["status"] == "PENDING_APPROVAL"
    assert body["bookings"][0].keys() == {
        "appointment_id", "start_at", "end_at", "status", "hold_expires_at",
        "replaces_appointment_id", "duration_minutes"}
    assert other.hold_id not in setup.api.get(BOOKINGS, headers=bearer("sub-1")).text

    setup.now = own.hold_expires_at + timedelta(seconds=1)
    expired = setup.api.get(BOOKINGS, headers=bearer("sub-1")).json()["bookings"]
    assert expired[0]["status"] == CalendarStatus.EXPIRED.value


def test_caller_supplied_client_ids_are_ignored() -> None:
    setup = Setup()
    setup.hold("client-2", "other-1")

    response = setup.api.get(BOOKINGS, params={"client_id": "client-2"}, headers=bearer("sub-1"))
    assert response.json() == {"bookings": []}
    assert setup.api.get("/v1/client/clients/client-2/bookings", headers=bearer("sub-1")
                         ).status_code == 404


@pytest.mark.parametrize("path", [URL + "?date=2026-10-13", BOOKINGS])
def test_unauthenticated_unlinked_inactive_and_owner_callers_are_denied(path: str) -> None:
    setup = Setup()

    assert setup.api.get(path).status_code == 401
    assert setup.api.get(path, headers=bearer("unknown")).status_code == 401
    assert setup.api.get(path, headers=bearer("sub-owner")).status_code == 403
    assert setup.api.get(path, headers=bearer("sub-gone")).status_code == 403
    assert setup.api.get(path, headers=bearer("sub-off")).status_code == 403


def test_invalid_dates_and_out_of_horizon_days_return_no_starts() -> None:
    setup = Setup()

    assert setup.api.get(URL, headers=bearer("sub-1")).status_code == 422
    assert setup.api.get(URL, params={"date": "nope"}, headers=bearer("sub-1")).status_code == 422
    far = setup.api.get(URL, params={"date": "2027-01-04"}, headers=bearer("sub-1"))
    assert far.json()["starts"] == []
