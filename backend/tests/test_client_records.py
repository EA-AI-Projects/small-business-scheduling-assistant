"""Client profiles, scoped notes, and owner-approved note retention."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import (
    ClientRecordService,
    HomeSize,
    RecordConflict,
    RecordNotFound,
    note_expired,
)
from scheduling.domain.owner_calendar import OwnerAction, OwnerCalendarCommand, OwnerCalendarService
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.owner_api import OwnerPrincipal, create_owner_app
from scheduling.workers.note_retention import purge_business_notes

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
START = datetime(2026, 9, 29, 16, tzinfo=UTC)
BUSINESS = "business-1"


def ready() -> tuple[InMemoryCalendarRepository, ClientRecordService]:
    repository = InMemoryCalendarRepository()
    OwnerPolicyService(repository, lambda: NOW).seed(BUSINESS, "owner-1", "seed")
    return repository, ClientRecordService(repository)


def save(service: ClientRecordService, client_id: str, phone: str,
         version: int = 0, duration: int = 60) -> object:
    return service.save_profile(BUSINESS, client_id, "Synthetic Client", phone,
                                "123 Test Street", HomeSize.SMALL, duration,
                                True, version, 180, NOW)


def test_phone_mapping_version_and_appointment_snapshot() -> None:
    repository, service = ready()
    profile = save(service, "client-1", "+14155550101")
    assert repository.read_verified_phone(BUSINESS, profile.phone_e164) is None
    verified = service.verify_phone(BUSINESS, "client-1", profile.phone_e164, NOW)
    assert repository.read_verified_phone(BUSINESS, profile.phone_e164) == verified
    with pytest.raises(RecordConflict, match="already assigned"):
        save(service, "client-2", profile.phone_e164)
    with pytest.raises(RecordConflict, match="version"):
        save(service, "client-1", profile.phone_e164)

    appointment = OwnerCalendarService(repository, lambda: NOW).apply(
        OwnerCalendarCommand(BUSINESS, "owner-1", "manual", OwnerAction.CREATE_APPOINTMENT,
                             repository.read_revision(BUSINESS), client_id="client-1",
                             start_at=START, duration_minutes=60)).appointment
    assert appointment is not None
    updated = save(service, "client-1", "+14155550102", version=2, duration=120)
    assert updated.phone_verified_at is None
    assert repository.read_verified_phone(BUSINESS, "+14155550101") is None
    assert repository.read_appointment(appointment.appointment_id).duration_minutes == 60


def test_note_scope_retention_and_access_code_exclusion() -> None:
    repository, service = ready()
    save(service, "client-1", "+14155550101")
    save(service, "client-2", "+14155550102")
    appointment = OwnerCalendarService(repository, lambda: NOW).apply(
        OwnerCalendarCommand(BUSINESS, "owner-1", "manual", OwnerAction.CREATE_APPOINTMENT,
                             repository.read_revision(BUSINESS), client_id="client-1",
                             start_at=START, duration_minutes=60)).appointment
    assert appointment is not None
    written = START + timedelta(hours=2)
    client_note = service.create_note(BUSINESS, "client-1", None, "Bring supplies", "owner-1",
                                      written, "note-1")
    appointment_note = service.create_note(BUSINESS, "client-1", appointment.appointment_id,
                                           "Use side entrance", "owner-1", written, "note-2")
    assert service.create_note(BUSINESS, "client-1", None, "Bring supplies", "owner-1",
                               written, "note-1") == client_note
    with pytest.raises(RecordConflict, match="idempotency"):
        service.create_note(BUSINESS, "client-1", None, "Different text", "owner-1",
                            written, "note-1")
    with pytest.raises(RecordNotFound, match="Appointment"):
        service.create_note(BUSINESS, "client-2", appointment.appointment_id,
                            "Wrong client", "owner-1", written)
    with pytest.raises(ValueError, match="access codes"):
        service.create_note(BUSINESS, "client-1", None, "Gate code 1234", "owner-1", written)
    anniversary = appointment.end_at.replace(year=appointment.end_at.year + 1)
    assert service.list_notes(BUSINESS, "client-1", anniversary - timedelta(seconds=1)) == (
        client_note, appointment_note)
    assert service.list_notes(BUSINESS, "client-1", anniversary) == ()
    assert service.purge_expired_notes(BUSINESS, "client-1", anniversary) == 2
    assert repository.list_notes(BUSINESS, "client-1") == ()
    with pytest.raises(ValueError, match="retention window"):
        service.create_note(BUSINESS, "client-1", None, "Late note", "owner-1",
                            anniversary + timedelta(days=1))


def test_only_ended_uncancelled_confirmed_appointment_counts_as_visit() -> None:
    repository, service = ready()
    save(service, "client-1", "+14155550101")
    appointment = OwnerCalendarService(repository, lambda: NOW).apply(
        OwnerCalendarCommand(BUSINESS, "owner-1", "manual", OwnerAction.CREATE_APPOINTMENT,
                             repository.read_revision(BUSINESS), client_id="client-1",
                             start_at=START, duration_minutes=60)).appointment
    assert appointment is not None
    assert repository.last_visit_end(BUSINESS, "client-1", appointment.end_at - timedelta(seconds=1)) is None
    assert repository.last_visit_end(BUSINESS, "client-1", appointment.end_at) == appointment.end_at
    repository._appointments[appointment.appointment_id] = replace(
        appointment, status=CalendarStatus.CANCELLED)
    assert repository.last_visit_end(BUSINESS, "client-1", appointment.end_at) is None


def test_never_visited_notes_expire_from_creation_and_legal_hold_survives() -> None:
    repository, service = ready()
    save(service, "client-1", "+14155550101")
    note = service.create_note(BUSINESS, "client-1", None, "Synthetic note", "owner-1", NOW)
    anniversary = NOW.replace(year=NOW.year + 1)
    assert service.list_notes(BUSINESS, "client-1", anniversary - timedelta(seconds=1)) == (note,)
    assert service.list_notes(BUSINESS, "client-1", anniversary) == ()
    held = service.change_note_hold(BUSINESS, "client-1", note.note_id, "documented case")
    assert not note_expired(held, None, anniversary + timedelta(days=365))
    assert purge_business_notes(repository, BUSINESS, anniversary) == 0
    assert len(repository.list_notes(BUSINESS, "client-1")) == 1
    with pytest.raises(RecordConflict, match="legal hold"):
        service.delete_note(BUSINESS, "client-1", note.note_id)
    service.change_note_hold(BUSINESS, "client-1", note.note_id, None)
    assert purge_business_notes(repository, BUSINESS, anniversary) == 1
    assert repository.list_notes(BUSINESS, "client-1") == ()


def test_owner_routes_require_verified_business_identity() -> None:
    repository, _ = ready()

    def verify(token: str) -> OwnerPrincipal:
        if token != "good":
            raise ValueError("bad token")
        return OwnerPrincipal("owner-1", BUSINESS)

    api = TestClient(create_owner_app(repository, verify, lambda: NOW))
    base = f"/v1/owner/businesses/{BUSINESS}/clients/client-1"
    body = {"expected_version": 0, "name": "Synthetic Client",
            "phone_e164": "+14155550101", "service_address": "123 Test Street",
            "home_size": "small", "default_duration_minutes": 60, "active": True}
    assert api.put(base, json=body, headers={"Idempotency-Key": "profile"}).status_code == 401
    assert api.put(base.replace(BUSINESS, "other"), json=body,
                   headers={"Authorization": "Bearer good",
                            "Idempotency-Key": "profile"}).status_code == 403
    response = api.put(base, json=body, headers={"Authorization": "Bearer good"})
    assert response.status_code == 200
    assert response.json()["phone_verified_at"] is None
    assert api.put(base, json=body, headers={"Authorization": "Bearer good"}).status_code == 409
    assert api.get(base, headers={"Authorization": "Bearer good"}).status_code == 200
    notes = f"{base}/notes"
    created = api.post(notes, json={"body": "Bring supplies"},
                       headers={"Authorization": "Bearer good", "Idempotency-Key": "note"})
    assert created.status_code == 200
    note_id = created.json()["note_id"]
    hold_url = f"{notes}/{note_id}/legal-hold"
    assert api.patch(hold_url, json={"reason": "documented case"}).status_code == 401
    assert api.patch(hold_url.replace(BUSINESS, "other"),
                     json={"reason": "documented case"},
                     headers={"Authorization": "Bearer good"}).status_code == 403
    held = api.patch(hold_url, json={"reason": "documented case"},
                     headers={"Authorization": "Bearer good"})
    assert held.status_code == 200
    assert held.json()["legal_hold_reason"] == "documented case"
    assert api.delete(f"{notes}/{note_id}",
                      headers={"Authorization": "Bearer good"}).status_code == 409
    assert api.patch(hold_url, json={"reason": None},
                     headers={"Authorization": "Bearer good"}).status_code == 200
    assert len(api.get(notes, headers={"Authorization": "Bearer good"}).json()) == 1
    assert api.get(notes, headers={"Authorization": "Bearer bad"}).status_code == 401
