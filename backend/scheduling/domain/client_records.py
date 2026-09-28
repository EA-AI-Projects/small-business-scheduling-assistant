"""Client identity and ordinary notes, separate from appointment snapshots."""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from scheduling.domain.appointments import Appointment

PHONE_PATTERN = re.compile(r"^\+[1-9]\d{1,14}$")
ACCESS_CODE_PATTERN = re.compile(
    r"\b(?:door|gate|entry|access|lockbox|alarm|keypad)\s*"
    r"(?:code|pin|password|combination)\b|"
    r"\b(?:code|pin|password|combination)\s*(?:for|to)\s*"
    r"(?:door|gate|entry|access|lockbox|alarm|keypad)\b",
    re.IGNORECASE,
)


class HomeSize(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class RecordConflict(Exception):
    """A client or note changed, or a phone belongs to another client."""


class RecordNotFound(Exception):
    """The target is absent from this business and client scope."""


@dataclass(frozen=True)
class ClientProfile:
    business_id: str
    client_id: str
    name: str
    phone_e164: str
    service_address: str
    home_size: HomeSize
    default_duration_minutes: int
    active: bool
    version: int
    created_at: datetime
    updated_at: datetime
    phone_verified_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.business_id or not self.client_id or not self.name.strip():
            raise ValueError("Business, client, and name are required")
        if len(self.name) > 200:
            raise ValueError("Client name is too long")
        if not PHONE_PATTERN.fullmatch(self.phone_e164):
            raise ValueError("Phone must use E.164 format")
        if not self.service_address.strip():
            raise ValueError("Service address is required")
        if len(self.service_address) > 500:
            raise ValueError("Service address is too long")
        if ACCESS_CODE_PATTERN.search(self.name) or ACCESS_CODE_PATTERN.search(self.service_address):
            raise ValueError("Entry/access codes are not allowed in client profiles")
        if self.default_duration_minutes <= 0 or self.version <= 0:
            raise ValueError("Duration and version must be positive")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Profile timestamps must be timezone-aware")
        if self.phone_verified_at is not None and self.phone_verified_at.tzinfo is None:
            raise ValueError("Phone verification time must be timezone-aware")


@dataclass(frozen=True)
class ClientNote:
    business_id: str
    client_id: str
    note_id: str
    appointment_id: str | None
    body: str
    created_by: str
    created_at: datetime
    legal_hold_reason: str | None = None

    def __post_init__(self) -> None:
        if not all((self.business_id, self.client_id, self.note_id, self.created_by)):
            raise ValueError("Note identity and author are required")
        if not self.body.strip() or ACCESS_CODE_PATTERN.search(self.body):
            raise ValueError("Empty notes and entry/access codes are not allowed")
        if len(self.body) > 2000:
            raise ValueError("Note is too long")
        if self.created_at.tzinfo is None:
            raise ValueError("Note timestamp must be timezone-aware")
        if self.legal_hold_reason is not None and not self.legal_hold_reason.strip():
            raise ValueError("Legal hold needs a documented reason")


class ClientRecordRepository(Protocol):
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...
    def list_profiles(self, business_id: str) -> tuple[ClientProfile, ...]: ...
    def read_verified_phone(self, business_id: str, phone_e164: str) -> ClientProfile | None: ...
    def save_profile(self, profile: ClientProfile, expected_version: int,
                     previous_phone: str | None) -> None: ...
    def read_appointment(self, appointment_id: str) -> Appointment | None: ...
    def read_note(self, business_id: str, client_id: str, note_id: str) -> ClientNote | None: ...
    def list_notes(self, business_id: str, client_id: str) -> tuple[ClientNote, ...]: ...
    def put_note(self, note: ClientNote) -> None: ...
    def delete_note(self, note: ClientNote) -> None: ...
    def last_visit_end(self, business_id: str, client_id: str,
                       now: datetime) -> datetime | None: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Current instant must be timezone-aware")
    return value.astimezone(UTC)


class ClientRecordService:
    def __init__(self, repository: ClientRecordRepository) -> None:
        self._repository = repository

    def save_profile(self, business_id: str, client_id: str, name: str, phone_e164: str,
                     service_address: str, home_size: HomeSize, default_duration_minutes: int,
                     active: bool, expected_version: int, maximum_visit_minutes: int,
                     now: datetime) -> ClientProfile:
        if expected_version < 0 or default_duration_minutes > maximum_visit_minutes:
            raise ValueError("Profile version or visit duration is invalid")
        current = self._repository.read_profile(business_id, client_id)
        if (current.version if current else 0) != expected_version:
            raise RecordConflict("Client profile version changed")
        instant = _utc(now)
        profile = ClientProfile(
            business_id, client_id, name.strip(), phone_e164, service_address.strip(),
            home_size, default_duration_minutes, active, expected_version + 1,
            current.created_at if current else instant, instant,
            current.phone_verified_at if current and current.phone_e164 == phone_e164 else None,
        )
        self._repository.save_profile(profile, expected_version,
                                      current.phone_e164 if current else None)
        return profile

    def verify_phone(self, business_id: str, client_id: str, phone_e164: str,
                     now: datetime) -> ClientProfile:
        """Trusted verified-webhook flow only; never exposed as an owner HTTP route."""
        current = self._repository.read_profile(business_id, client_id)
        if current is None or current.phone_e164 != phone_e164:
            raise RecordNotFound("Matching client phone was not found")
        updated = replace(current, version=current.version + 1,
                          updated_at=_utc(now), phone_verified_at=_utc(now))
        self._repository.save_profile(updated, current.version, current.phone_e164)
        return updated

    def create_note(self, business_id: str, client_id: str, appointment_id: str | None,
                    body: str, actor_id: str, now: datetime, note_id: str | None = None) -> ClientNote:
        if self._repository.read_profile(business_id, client_id) is None:
            raise RecordNotFound("Client was not found")
        if appointment_id is not None:
            appointment = self._repository.read_appointment(appointment_id)
            if (appointment is None or appointment.business_id != business_id
                    or appointment.client_id != client_id):
                raise RecordNotFound("Appointment was not found for this client")
        note = ClientNote(business_id, client_id, note_id or str(uuid4()), appointment_id,
                          body.strip(), actor_id, _utc(now))
        last_visit = self._repository.last_visit_end(business_id, client_id, note.created_at)
        if note_expired(note, last_visit, note.created_at):
            raise ValueError("The client's note retention window has elapsed")
        existing = self._repository.read_note(business_id, client_id, note.note_id)
        if existing is not None:
            if note_expired(existing, last_visit, note.created_at):
                raise ValueError("The client's note retention window has elapsed")
            if _same_note_request(existing, note):
                return existing
            raise RecordConflict("Note idempotency key was reused with different content")
        try:
            self._repository.put_note(note)
        except RecordConflict:
            existing = self._repository.read_note(business_id, client_id, note.note_id)
            if (existing is not None and _same_note_request(existing, note)
                    and not note_expired(existing, last_visit, note.created_at)):
                return existing
            raise
        return note

    def list_notes(self, business_id: str, client_id: str,
                   now: datetime) -> tuple[ClientNote, ...]:
        if self._repository.read_profile(business_id, client_id) is None:
            raise RecordNotFound("Client was not found")
        notes = self._repository.list_notes(business_id, client_id)
        if not notes:
            return ()
        instant = _utc(now)
        last_visit = self._repository.last_visit_end(business_id, client_id, instant)
        return tuple(note for note in notes
                     if not note_expired(note, last_visit, instant))

    def delete_note(self, business_id: str, client_id: str, note_id: str) -> None:
        note = self._repository.read_note(business_id, client_id, note_id)
        if note is None:
            raise RecordNotFound("Note was not found")
        self._repository.delete_note(note)

    def purge_expired_notes(self, business_id: str, client_id: str,
                            now: datetime) -> int:
        notes = self._repository.list_notes(business_id, client_id)
        if not notes:
            return 0
        instant = _utc(now)
        last_visit = self._repository.last_visit_end(business_id, client_id, instant)
        expired = [note for note in notes
                   if note_expired(note, last_visit, instant)]
        deleted = 0
        for note in expired:
            try:
                self._repository.delete_note(note)
            except RecordConflict:
                # A concurrent change or legal hold wins; the next run rechecks it.
                continue
            deleted += 1
        return deleted


def note_expired(note: ClientNote, last_visit_end: datetime | None,
                 now: datetime) -> bool:
    if note.legal_hold_reason is not None:
        return False
    anchor = last_visit_end if last_visit_end is not None else note.created_at
    try:
        anniversary = anchor.replace(year=anchor.year + 1)
    except ValueError:  # February 29
        anniversary = anchor.replace(year=anchor.year + 1, day=28)
    return now >= anniversary


def _same_note_request(existing: ClientNote, requested: ClientNote) -> bool:
    return (existing.appointment_id == requested.appointment_id
            and existing.body == requested.body
            and existing.created_by == requested.created_by)
