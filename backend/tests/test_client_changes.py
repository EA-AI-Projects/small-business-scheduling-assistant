"""Client cancel and reschedule: the text path's transitions, scoped to the signed-in client."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from test_client_requests import BASE, OWNER, START, auth, request, setup

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.expiry import ExpiryService
from scheduling.domain.lifecycle import LifecycleService

LATER = START + timedelta(days=1)


def confirmed(api: TestClient, who: str = "a", start: datetime = START, key: str = "c") -> dict:  # type: ignore[type-arg]
    created = request(api, who, key, start).json()  # type: ignore[attr-defined]
    done = api.post(f"{BASE}/requests/{created['appointment_id']}/approve",
                    json={"expected_version": 1}, headers={**OWNER, "Idempotency-Key": f"ap-{key}"})
    assert done.status_code == 200, done.text
    return next(item for item in api.get("/v1/client/bookings", headers=auth(who)).json()["bookings"]
                if item["appointment_id"] == created["appointment_id"])


def cancel(api: TestClient, who: str, booking: dict, key: str = "x",  # type: ignore[type-arg]
           version: int | None = None):  # type: ignore[no-untyped-def]
    return api.post(f"/v1/client/bookings/{booking['appointment_id']}/cancel",
                    json={"expected_version": version or booking["version"]}, headers=auth(who, key))


def move(api: TestClient, who: str, booking: dict, start: datetime = LATER, key: str = "m",  # type: ignore[type-arg]
         version: int | None = None):  # type: ignore[no-untyped-def]
    return api.post(f"/v1/client/bookings/{booking['appointment_id']}/reschedule",
                    json={"start_at": start.isoformat(),
                          "expected_version": version or booking["version"]},
                    headers=auth(who, key))


def mine(api: TestClient, who: str = "a") -> list[dict]:  # type: ignore[type-arg]
    return api.get("/v1/client/bookings", headers=auth(who)).json()["bookings"]  # type: ignore[no-any-return]


def free_starts(api: TestClient, day: str = "2026-09-29") -> set[datetime]:
    return {datetime.fromisoformat(v) for v in api.get(
        f"/v1/client/availability?day={day}", headers=auth("a")).json()["starts_at"]}


def test_cancel_releases_the_time_and_queues_owner_and_client_notices() -> None:
    api, repository, _ = setup()
    booking = confirmed(api)
    assert START not in free_starts(api)
    response = cancel(api, "a", booking)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"
    assert mine(api) == []
    assert START in free_starts(api)
    assert [(i.recipient, i.template) for i in repository.list_outbox_intents()][-2:] == [
        ("client", "cancel"), ("owner", "cancel")]
    # A retried press with the same key replays and queues nothing more.
    revision = repository.read_revision("business-1")
    assert cancel(api, "a", booking).json() == response.json()
    assert repository.read_revision("business-1") == revision


def test_cancel_is_denied_to_other_clients_and_unknown_ids_without_a_difference() -> None:
    api, repository, _ = setup()
    booking = confirmed(api)
    revision = repository.read_revision("business-1")
    other = cancel(api, "b", booking)
    unknown = cancel(api, "b", {"appointment_id": "no-such-id", "version": 2})
    assert other.status_code == unknown.status_code == 404
    assert other.json() == unknown.json()
    assert api.post(f"/v1/client/bookings/{booking['appointment_id']}/cancel",
                    json={"expected_version": 2}).status_code == 401
    assert move(api, "b", booking).status_code == 404
    assert repository.read_revision("business-1") == revision
    assert [item["status"] for item in mine(api)] == ["CONFIRMED"]


def test_cancel_with_a_stale_or_ended_booking_changes_nothing() -> None:
    api, repository, clock = setup()
    booking = confirmed(api)
    revision = repository.read_revision("business-1")
    stale = cancel(api, "a", booking, "s", version=booking["version"] + 1)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "STALE_BOOKING"
    # A body naming anything else is refused outright.
    assert api.post(f"/v1/client/bookings/{booking['appointment_id']}/cancel",
                    json={"expected_version": 2, "client_id": "client-b"},
                    headers=auth("a", "z")).status_code == 422
    assert repository.read_revision("business-1") == revision
    assert cancel(api, "a", booking, "ok").status_code == 200
    again = cancel(api, "a", booking, "other-key")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "STALE_BOOKING"
    other = confirmed(api, "a", LATER, "c2")
    clock[0] = other["end_at"] and datetime.fromisoformat(other["end_at"])
    ended = cancel(api, "a", other, "late")
    assert ended.status_code == 409 and ended.json()["detail"]["code"] == "BOOKING_NOT_ACTIVE"


def test_cancelling_a_pending_request_follows_the_text_path() -> None:
    api, _, _ = setup()
    created = request(api, "a", "k1").json()  # type: ignore[attr-defined]
    response = cancel(api, "a", created)
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
    assert api.get(f"{BASE}/requests", headers=OWNER).json() == []
    assert START in free_starts(api)


def test_reschedule_keeps_the_original_until_owner_approval_then_swaps_atomically() -> None:
    api, repository, _ = setup()
    original = confirmed(api)
    response = move(api, "a", original)
    assert response.status_code == 200, response.text
    replacement = response.json()
    assert replacement["status"] == "PENDING_APPROVAL"
    assert replacement["replaces_appointment_id"] == original["appointment_id"]
    assert datetime.fromisoformat(replacement["start_at"]) == LATER
    listed = {item["appointment_id"]: item for item in mine(api)}
    assert listed[original["appointment_id"]]["status"] == "CONFIRMED"
    assert listed[replacement["appointment_id"]]["status"] == "PENDING_APPROVAL"
    assert [(i.recipient, i.template) for i in repository.list_outbox_intents()][-2:] == [
        ("owner", "hold-request"), ("client", "hold-pending")]
    pending = api.get(f"{BASE}/requests", headers=OWNER).json()
    assert [item["appointment_id"] for item in pending] == [replacement["appointment_id"]]
    # While it waits the original cannot be cancelled out from under the request.
    blocked = cancel(api, "a", original, "blocked")
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "REPLACEMENT_PENDING"
    second = move(api, "a", original, LATER + timedelta(hours=3), "m2")
    assert second.status_code == 409 and second.json()["detail"]["code"] == "REPLACEMENT_PENDING"
    approved = api.post(f"{BASE}/requests/{replacement['appointment_id']}/approve",
                        json={"expected_version": 1}, headers={**OWNER, "Idempotency-Key": "ap"})
    assert approved.status_code == 200, approved.text
    after = {item["appointment_id"]: item["status"] for item in mine(api)}
    assert after == {replacement["appointment_id"]: "CONFIRMED"}
    old = repository.read_appointment(original["appointment_id"])
    assert old is not None and old.status == CalendarStatus.CANCELLED
    assert START in free_starts(api)
    assert ("client", "replacement-approved") in [
        (i.recipient, i.template) for i in repository.list_outbox_intents()]


def test_declined_replacement_leaves_the_original_intact() -> None:
    api, _, _ = setup()
    original = confirmed(api)
    replacement = move(api, "a", original).json()
    done = api.post(f"{BASE}/requests/{replacement['appointment_id']}/decline",
                    json={"expected_version": 1}, headers={**OWNER, "Idempotency-Key": "d"})
    assert done.status_code == 200, done.text
    assert [(item["appointment_id"], item["status"]) for item in mine(api)] == [
        (original["appointment_id"], "CONFIRMED")]
    assert LATER in free_starts(api, "2026-09-30")
    # The guard is cleared, so another move can be asked for.
    assert move(api, "a", original, LATER + timedelta(hours=3), "m2").status_code == 200


def test_expired_replacement_leaves_the_original_intact() -> None:
    api, repository, clock = setup()
    original = confirmed(api)
    replacement = move(api, "a", original).json()
    repository.due.append(replacement["appointment_id"])
    due = datetime.fromisoformat(replacement["hold_expires_at"])
    clock[0] = due
    result = ExpiryService(repository, LifecycleService(repository, lambda: due),
                           lambda: due).run_once()
    assert result.expired == 1
    assert [(item["appointment_id"], item["status"]) for item in mine(api)] == [
        (original["appointment_id"], "CONFIRMED")]
    # Unswept but past its hold, the replacement no longer blocks cancelling or moving again.
    assert move(api, "a", original, LATER + timedelta(hours=3), "m2").status_code == 200


def test_withdrawing_the_replacement_keeps_the_original_and_cancelling_the_original_then_works() -> None:
    api, repository, _ = setup()
    original = confirmed(api)
    replacement = move(api, "a", original).json()
    withdrawn = cancel(api, "a", replacement, "w")
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "CANCELLED"
    assert ("owner", "replacement-original-retained") in [
        (i.recipient, i.template) for i in repository.list_outbox_intents()]
    assert [item["status"] for item in mine(api)] == ["CONFIRMED"]
    assert cancel(api, "a", original, "orig").status_code == 200


def test_reschedule_rejects_stale_pending_and_taken_times_and_retries_replay() -> None:
    api, repository, _ = setup()
    original = confirmed(api)
    revision = repository.read_revision("business-1")
    stale = move(api, "a", original, key="s", version=original["version"] + 1)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "STALE_BOOKING"
    pending = request(api, "a", "p", START + timedelta(days=2)).json()  # type: ignore[attr-defined]
    not_confirmed = move(api, "a", pending, key="np")
    assert not_confirmed.status_code == 409
    assert not_confirmed.json()["detail"]["code"] == "BOOKING_NOT_RESCHEDULABLE"
    # Another client takes the wanted time first: nothing is held, alternatives come back.
    assert request(api, "b", "b1", LATER).status_code == 200  # type: ignore[attr-defined]
    taken = move(api, "a", original, key="t")
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "SLOT_CONFLICT"
    assert taken.json()["detail"]["alternatives"]
    assert repository.read_revision("business-1") == revision + 2  # only the two requests
    ok = move(api, "a", original, LATER + timedelta(hours=4), "good")
    assert ok.status_code == 200
    assert move(api, "a", original, LATER + timedelta(hours=4), "good").json() == ok.json()
    reused = move(api, "a", original, LATER + timedelta(hours=6), "good")
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    naive = api.post(f"/v1/client/bookings/{original['appointment_id']}/reschedule",
                     json={"start_at": "2026-09-30T09:00:00", "expected_version": 2},
                     headers=auth("a", "n"))
    assert naive.status_code == 422


def test_owner_edit_after_the_client_saw_the_visit_makes_the_move_stale() -> None:
    api, _, _ = setup()
    original = confirmed(api)
    edited = api.patch(f"{BASE}/appointments/{original['appointment_id']}",
                      json={"expected_version": original["version"],
                            "start_at": (START + timedelta(hours=3)).isoformat()},
                      headers={**OWNER, "Idempotency-Key": "e"})
    assert edited.status_code == 200, edited.text
    stale = move(api, "a", original, key="s")
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "STALE_BOOKING"
    assert cancel(api, "a", original, "sc").json()["detail"]["code"] == "STALE_BOOKING"  # type: ignore[attr-defined]


def test_two_clients_racing_for_one_replacement_time_commit_exactly_one() -> None:
    api, _, _ = setup()
    one = confirmed(api, "a", START, "c1")
    two = confirmed(api, "b", START + timedelta(hours=4), "c2")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda args: move(api, *args), (("a", one), ("b", two))))
    assert sorted(item.status_code for item in results) == [200, 409]
    assert len(api.get(f"{BASE}/requests", headers=OWNER).json()) == 1


def test_owner_approval_racing_the_clients_cancel_of_the_original_never_loses_the_visit() -> None:
    api, _, _ = setup()
    original = confirmed(api)
    replacement = move(api, "a", original).json()

    def approve(_: int) -> int:
        return api.post(f"{BASE}/requests/{replacement['appointment_id']}/approve",
                        json={"expected_version": 1},
                        headers={**OWNER, "Idempotency-Key": "ap"}).status_code

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(approve, 0)
        second = pool.submit(lambda: cancel(api, "a", original, "race").status_code)
        codes = (first.result(), second.result())
    assert codes == (200, 409)  # the active replacement refuses the cancel, in either order
    assert [item["status"] for item in mine(api)] == ["CONFIRMED"]
