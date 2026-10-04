"""Authenticated owner HTTP adapter for the persisted scheduling services.

The caller supplies a token verifier. In production it must validate the Cognito
access token (issuer, signature, expiry, token_use and client ID) before returning
an owner principal. No request field or HTTP header can assert an actor role.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from hashlib import sha256
from typing import Annotated, Protocol, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scheduling.adapters.dynamodb import DynamoClient, DynamoDBCalendarRepository
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import AvailabilityPolicy, HolidayCalendar, LocalWindow
from scheduling.domain.client_records import (
    ClientRecordRepository,
    ClientRecordService,
    HomeSize,
    RecordConflict,
    RecordNotFound,
)
from scheduling.domain.holds import (
    CreateHold,
    HoldRepository,
    HoldService,
    IdempotencyKeyReused,
    InvalidReplacement,
    ReplacementPending,
    RevisionConflict,
    SlotConflict,
    TooManyConflicts,
)
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    HoldExpired,
    InvalidTransition,
    LifecycleRepository,
    LifecycleService,
    StaleVersion,
)
from scheduling.domain.owner_calendar import (
    BlockNotFound,
    OwnerAction,
    OwnerCalendarCommand,
    OwnerCalendarRepository,
    OwnerCalendarService,
    UnavailableBlock,
)
from scheduling.domain.owner_policy import (
    OwnerPolicyService,
    PolicyCommand,
    PolicyConflict,
    PolicyNotConfigured,
    PolicyRecord,
    PolicyRepository,
)
from scheduling.domain.sms_ingress import SmsIngressStore, record_in_person_consent


@dataclass(frozen=True)
class OwnerPrincipal:
    actor_id: str
    business_id: str


class OwnerTokenVerifier(Protocol):
    def __call__(self, token: str) -> OwnerPrincipal: ...


class OwnerRepository(Protocol):
    """The owner adapter needs the calendar, hold and lifecycle repository methods."""

    def read_revision(self, business_id: str) -> int: ...
    def read_calendar(self, business_id: str) -> object: ...
    def read_pending_requests(self, business_id: str, now: datetime) -> tuple[Appointment, ...]: ...
    def read_appointment(self, appointment_id: str) -> Appointment | None: ...
    def read_block(self, business_id: str, block_id: str) -> UnavailableBlock | None: ...
    def read_policy_record(self, business_id: str) -> PolicyRecord | None: ...


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WindowBody(StrictModel):
    opens: time
    closes: time


class PolicyBody(StrictModel):
    timezone: str
    weekly_windows: dict[int, list[WindowBody]]
    booking_horizon_days: int = Field(gt=0)
    slot_increment_minutes: int = Field(gt=0)
    maximum_visit_minutes: int = Field(gt=0)
    minimum_visit_gap_minutes: int = Field(ge=0)
    opening_buffer_minutes: int = Field(ge=0)
    closing_buffer_minutes: int = Field(ge=0)
    hold_minutes: int = Field(gt=0)
    maximum_buffer_minutes: int = Field(ge=0)
    date_exceptions: dict[date, list[WindowBody]] = Field(default_factory=dict)
    holiday_calendar: HolidayCalendar | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown business timezone") from exc
        return value

    def policy(self) -> AvailabilityPolicy:
        def windows(items: list[WindowBody]) -> tuple[LocalWindow, ...]:
            return tuple(LocalWindow(item.opens, item.closes) for item in items)

        return AvailabilityPolicy(
            timezone=self.timezone,
            weekly_windows={day: windows(items) for day, items in self.weekly_windows.items()},
            booking_horizon_days=self.booking_horizon_days,
            slot_increment_minutes=self.slot_increment_minutes,
            maximum_visit_minutes=self.maximum_visit_minutes,
            minimum_visit_gap_minutes=self.minimum_visit_gap_minutes,
            opening_buffer_minutes=self.opening_buffer_minutes,
            closing_buffer_minutes=self.closing_buffer_minutes,
            hold_minutes=self.hold_minutes,
            maximum_buffer_minutes=self.maximum_buffer_minutes,
            date_exceptions={day: windows(items) for day, items in self.date_exceptions.items()},
            holiday_calendar=self.holiday_calendar,
        )


class PolicyEditBody(StrictModel):
    expected_revision: int = Field(ge=0)
    expected_version: int = Field(gt=0)
    policy: PolicyBody


class HoldBody(StrictModel):
    client_id: str = Field(min_length=1)
    start_at: datetime
    duration_minutes: int = Field(gt=0)
    replaces_appointment_id: str | None = None


class DecisionBody(StrictModel):
    expected_version: int = Field(gt=0)


class EditAppointmentBody(DecisionBody):
    start_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_change(self) -> "EditAppointmentBody":
        if self.start_at is None and self.duration_minutes is None:
            raise ValueError("An appointment edit needs a start or duration")
        return self


class BlockBody(StrictModel):
    expected_revision: int = Field(ge=0)
    start_at: datetime
    end_at: datetime


class MoveBlockBody(BlockBody):
    expected_version: int = Field(gt=0)


class RemoveBlockBody(StrictModel):
    expected_revision: int = Field(ge=0)
    expected_version: int = Field(gt=0)


class ManualAppointmentBody(StrictModel):
    expected_revision: int = Field(ge=0)
    client_id: str = Field(min_length=1)
    start_at: datetime
    duration_minutes: int = Field(gt=0)


class ClientProfileBody(StrictModel):
    expected_version: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=200)
    phone_e164: str = Field(min_length=2, max_length=16)
    service_address: str = Field(min_length=1, max_length=500)
    home_size: HomeSize
    default_duration_minutes: int = Field(gt=0)
    active: bool = True


class ClientNoteBody(StrictModel):
    appointment_id: str | None = None
    body: str = Field(min_length=1, max_length=2000)


class LegalHoldBody(StrictModel):
    reason: str | None


class InPersonConsentBody(StrictModel):
    phone_e164: str = Field(min_length=2, max_length=16)
    participant_name: str = Field(min_length=1, max_length=200)
    script_version: str = Field(min_length=1, max_length=80)
    clear_yes: bool


def _error(code: str, message: str, status: int, current: object | None = None) -> HTTPException:
    detail: dict[str, object] = {"error": {"code": code, "message": message}}
    if current is not None:
        detail["current"] = current
    return HTTPException(status_code=status, detail=detail)


CORS_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
CORS_HEADERS = ("Authorization", "Content-Type", "Idempotency-Key")
CORS_MAX_AGE_SECONDS = 600
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


def validate_cors_origin(origin: str, allow_loopback_http: bool = False) -> str:
    """Accept only an exact browser origin: scheme and host, with an optional port."""
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Invalid CORS origin: {origin!r}") from exc
    host = parts.hostname or ""
    if (not host or "*" in origin or parts.path or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None
            or origin != f"{parts.scheme}://{parts.netloc}"
            or parts.netloc != (host if port is None else f"{host}:{port}")):
        raise ValueError(f"CORS origin must be scheme://host[:port] only: {origin!r}")
    if (parts.scheme, port) in {("https", 443), ("http", 80)}:
        # Browsers omit a default port from Origin, so this value could never match.
        raise ValueError(f"CORS origin must omit the default port: {origin!r}")
    if parts.scheme == "https":
        return origin
    if parts.scheme == "http" and allow_loopback_http and host in _LOOPBACK_HOSTS:
        return origin
    raise ValueError(f"CORS origin must use HTTPS: {origin!r}")


def create_owner_app(
    repository: object,
    verify_token: OwnerTokenVerifier,
    clock: Callable[[], datetime] | None = None,
    sms_store: SmsIngressStore | None = None,
    *,
    cors_origins: tuple[str, ...] = (),
    allow_loopback_http: bool = False,
) -> FastAPI:
    """Mount only authenticated owner routes; local synthetic API stays separate.

    ``cors_origins`` lists the exact browser origins (the owner app) allowed to
    call the API cross-origin. Plain-HTTP loopback origins require
    ``allow_loopback_http`` and are meant only for the local development server.
    """
    origins = tuple(validate_cors_origin(origin, allow_loopback_http) for origin in cors_origins)
    app = FastAPI(title="Scheduling owner API", version="0.1.0")
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=False,
            allow_methods=list(CORS_METHODS),
            allow_headers=list(CORS_HEADERS),
            max_age=CORS_MAX_AGE_SECONDS,
        )
    now = clock or (lambda: datetime.now(UTC))
    security = HTTPBearer(auto_error=False)

    @app.exception_handler(HTTPException)
    def owner_http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    store = cast(OwnerRepository, repository)
    holds = HoldService(cast(HoldRepository, repository))
    lifecycle = LifecycleService(cast(LifecycleRepository, repository), now)
    calendar = OwnerCalendarService(cast(OwnerCalendarRepository, repository), now)
    policy = OwnerPolicyService(cast(PolicyRepository, repository), now)
    clients = ClientRecordService(cast(ClientRecordRepository, repository))

    def principal(
        business_id: str,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> OwnerPrincipal:
        if credentials is None:
            raise _error("UNAUTHORIZED", "Owner authentication is required", 401)
        try:
            owner = verify_token(credentials.credentials)
        except Exception as exc:
            raise _error("UNAUTHORIZED", "Invalid owner credentials", 401) from exc
        if not owner.actor_id or not owner.business_id:
            raise _error("UNAUTHORIZED", "Invalid owner identity", 401)
        if owner.business_id != business_id:
            raise _error("FORBIDDEN", "Owner cannot access this business", 403)
        return owner

    def key(idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)]) -> str:
        return idempotency_key

    def run(operation: Callable[[], object], current: Callable[[], object | None] | None = None) -> object:
        try:
            return operation()
        except IdempotencyKeyReused as exc:
            raise _error("IDEMPOTENCY_KEY_REUSED", str(exc), 409) from exc
        except (RevisionConflict, StaleVersion, TooManyConflicts) as exc:
            raise _error("STALE_VERSION", str(exc), 409, current() if current else None) from exc
        except ReplacementPending as exc:
            raise _error("REPLACEMENT_PENDING", str(exc), 409, current() if current else None) from exc
        except (SlotConflict, PolicyConflict) as exc:
            raise _error("SLOT_CONFLICT", str(exc), 409, current() if current else None) from exc
        except HoldExpired as exc:
            raise _error("HOLD_EXPIRED", str(exc), 409, current() if current else None) from exc
        except (InvalidTransition, InvalidReplacement, BlockNotFound) as exc:
            raise _error("INVALID_TARGET", str(exc), 409, current() if current else None) from exc
        except PolicyNotConfigured as exc:
            raise _error("POLICY_NOT_CONFIGURED", str(exc), 409) from exc
        except RecordConflict as exc:
            raise _error("RECORD_CONFLICT", str(exc), 409) from exc
        except RecordNotFound as exc:
            raise _error("NOT_FOUND", str(exc), 404) from exc
        except ValueError as exc:
            raise _error("INVALID_REQUEST", str(exc), 422) from exc

    def appointment_state(appointment_id: str, business_id: str) -> dict[str, object] | None:
        target = store.read_appointment(appointment_id)
        if target is None or target.business_id != business_id:
            return None
        return {"appointment_id": target.appointment_id, "status": target.status.value,
                "start_at": target.start_at.isoformat(), "duration_minutes": target.duration_minutes,
                "version": target.version}

    def block_state(block_id: str, business_id: str) -> dict[str, object] | None:
        target = store.read_block(business_id, block_id)
        if target is None:
            return None
        return {"block_id": target.block_id, "start_at": target.start_at.isoformat(),
                "end_at": target.end_at.isoformat(), "version": target.version}

    def policy_state(business_id: str) -> dict[str, object] | None:
        record = store.read_policy_record(business_id)
        if record is None:
            return None
        return {"version": record.version,
                "calendar_revision": store.read_revision(business_id)}

    @app.get("/v1/owner/businesses/{business_id}/clients")
    def list_clients(business_id: str,
                     owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        return cast(ClientRecordRepository, repository).list_profiles(business_id)

    @app.get("/v1/owner/businesses/{business_id}/clients/{client_id}")
    def get_client(business_id: str, client_id: str,
                   owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        profile = cast(ClientRecordRepository, repository).read_profile(business_id, client_id)
        if profile is None:
            raise _error("NOT_FOUND", "Client was not found", 404)
        return profile

    @app.delete("/v1/owner/businesses/{business_id}/clients/{client_id}", status_code=204)
    def erase_client(business_id: str, client_id: str,
                     owner: Annotated[OwnerPrincipal, Depends(principal)]) -> None:
        del owner
        return cast(DynamoDBCalendarRepository, repository).erase_client(business_id, client_id)

    @app.put("/v1/owner/businesses/{business_id}/clients/{client_id}")
    def save_client(business_id: str, client_id: str, body: ClientProfileBody,
                    owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        record = store.read_policy_record(business_id)
        if record is None:
            raise _error("POLICY_NOT_CONFIGURED", "Persist the pilot policy first", 409)
        return run(lambda: clients.save_profile(
            business_id, client_id, body.name, body.phone_e164, body.service_address,
            body.home_size, body.default_duration_minutes, body.active,
            body.expected_version, record.policy.maximum_visit_minutes, now()))

    if sms_store is not None:
        @app.post("/v1/owner/businesses/{business_id}/clients/{client_id}/sms-consent")
        def capture_sms_consent(business_id: str, client_id: str, body: InPersonConsentBody,
                                owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
            del owner
            if not body.clear_yes:
                raise _error("CONSENT_NOT_GIVEN", "Record consent only after a clear yes", 422)
            try:
                return record_in_person_consent(
                    sms_store, cast(ClientRecordRepository, repository), business_id,
                    client_id, body.phone_e164, body.participant_name,
                    body.script_version, now(),
                )
            except RecordConflict as exc:
                raise _error("RECORD_CONFLICT", str(exc), 409) from exc
            except ValueError as exc:
                raise _error("INVALID_CONSENT", str(exc), 422) from exc

        @app.get("/v1/owner/businesses/{business_id}/sms-delivery-failures")
        def sms_delivery_failures(business_id: str,
                                  owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
            del owner
            return sms_store.list_delivery_failures(business_id)

    @app.get("/v1/owner/businesses/{business_id}/clients/{client_id}/notes")
    def list_client_notes(business_id: str, client_id: str,
                          owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        return run(lambda: clients.list_notes(business_id, client_id, now()))

    @app.post("/v1/owner/businesses/{business_id}/clients/{client_id}/notes")
    def create_client_note(business_id: str, client_id: str, body: ClientNoteBody,
                           owner: Annotated[OwnerPrincipal, Depends(principal)],
                           request_key: Annotated[str, Depends(key)]) -> object:
        note_identity = json.dumps([business_id, client_id, owner.actor_id, request_key],
                                   separators=(",", ":"))
        note_id = sha256(note_identity.encode()).hexdigest()
        return run(lambda: clients.create_note(
            business_id, client_id, body.appointment_id, body.body,
            owner.actor_id, now(), note_id))

    @app.delete("/v1/owner/businesses/{business_id}/clients/{client_id}/notes/{note_id}")
    def delete_client_note(business_id: str, client_id: str, note_id: str,
                           owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        return run(lambda: clients.delete_note(business_id, client_id, note_id))

    @app.patch("/v1/owner/businesses/{business_id}/clients/{client_id}/notes/{note_id}/legal-hold")
    def change_client_note_hold(business_id: str, client_id: str, note_id: str,
                                body: LegalHoldBody,
                                owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        return run(lambda: clients.change_note_hold(business_id, client_id, note_id,
                                                     body.reason))

    @app.get("/v1/owner/businesses/{business_id}/calendar")
    def owner_calendar(business_id: str, owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        snapshot = store.read_calendar(business_id)
        return snapshot

    @app.get("/v1/owner/businesses/{business_id}/appointments/{appointment_id}")
    def get_owner_appointment(business_id: str, appointment_id: str,
                              owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        target = store.read_appointment(appointment_id)
        if target is None or target.business_id != business_id:
            raise _error("NOT_FOUND", "Appointment was not found", 404)
        return target

    @app.get("/v1/owner/businesses/{business_id}/blocks/{block_id}")
    def get_owner_block(business_id: str, block_id: str,
                        owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        target = store.read_block(business_id, block_id)
        if target is None:
            raise _error("NOT_FOUND", "Block was not found", 404)
        return target

    @app.get("/v1/owner/businesses/{business_id}/requests")
    def pending_requests(business_id: str, owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        return store.read_pending_requests(business_id, now())

    @app.get("/v1/owner/businesses/{business_id}/policy")
    def get_policy(business_id: str, owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        record = store.read_policy_record(business_id)
        if record is None:
            raise _error("POLICY_NOT_CONFIGURED", "Persist the pilot policy first", 404)
        return {"record": record, "calendar_revision": store.read_revision(business_id)}

    @app.get("/v1/owner/businesses/{business_id}/local-time")
    def resolve_local_time(business_id: str, value: str,
                           owner: Annotated[OwnerPrincipal, Depends(principal)]) -> object:
        del owner
        record = store.read_policy_record(business_id)
        if record is None:
            raise _error("POLICY_NOT_CONFIGURED", "Persist the pilot policy first", 409)
        try:
            wall = datetime.fromisoformat(value)
        except ValueError as exc:
            raise _error("INVALID_REQUEST", "Use a local date and time", 422) from exc
        if wall.tzinfo is not None:
            raise _error("INVALID_REQUEST", "Local time must have no UTC offset", 422)
        zone = ZoneInfo(record.policy.timezone)
        choices: set[datetime] = set()
        for fold in (0, 1):
            candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) == wall:
                choices.add(candidate)
        if len(choices) != 1:
            raise _error("INVALID_REQUEST", "Local time is missing or ambiguous at DST change", 422)
        return {"instant": next(iter(choices)).isoformat()}

    @app.post("/v1/owner/businesses/{business_id}/policy/seed")
    def seed_policy(business_id: str, owner: Annotated[OwnerPrincipal, Depends(principal)],
                    request_key: Annotated[str, Depends(key)]) -> object:
        return run(lambda: policy.seed(business_id, owner.actor_id, request_key))

    @app.put("/v1/owner/businesses/{business_id}/policy")
    def edit_policy(business_id: str, body: PolicyEditBody,
                    owner: Annotated[OwnerPrincipal, Depends(principal)],
                    request_key: Annotated[str, Depends(key)]) -> object:
        return run(lambda: policy.apply(PolicyCommand(
            business_id, owner.actor_id, request_key, body.expected_revision,
            body.expected_version, body.policy.policy())), lambda: policy_state(business_id))

    @app.post("/v1/owner/businesses/{business_id}/requests")
    def create_request(business_id: str, body: HoldBody,
                       owner: Annotated[OwnerPrincipal, Depends(principal)],
                       request_key: Annotated[str, Depends(key)]) -> object:
        return run(lambda: holds.create(CreateHold(
            business_id, owner.actor_id, body.client_id, request_key,
            body.start_at, body.duration_minutes, body.replaces_appointment_id), now()))

    def transition(business_id: str, appointment_id: str, body: DecisionBody,
                   owner: OwnerPrincipal, request_key: str, action: Action,
                   start_at: datetime | None = None,
                   duration_minutes: int | None = None) -> object:
        return run(lambda: lifecycle.apply(AppointmentCommand(
            business_id, appointment_id, owner.actor_id, ActorRole.OWNER, action,
            request_key, body.expected_version, start_at, duration_minutes)),
            lambda: appointment_state(appointment_id, business_id))

    @app.post("/v1/owner/businesses/{business_id}/requests/{appointment_id}/approve")
    def approve(business_id: str, appointment_id: str, body: DecisionBody,
                owner: Annotated[OwnerPrincipal, Depends(principal)],
                request_key: Annotated[str, Depends(key)]) -> object:
        return transition(business_id, appointment_id, body, owner, request_key, Action.APPROVE)

    @app.post("/v1/owner/businesses/{business_id}/requests/{appointment_id}/decline")
    def decline(business_id: str, appointment_id: str, body: DecisionBody,
                owner: Annotated[OwnerPrincipal, Depends(principal)],
                request_key: Annotated[str, Depends(key)]) -> object:
        return transition(business_id, appointment_id, body, owner, request_key, Action.DECLINE)

    @app.post("/v1/owner/businesses/{business_id}/appointments/{appointment_id}/cancel")
    def cancel(business_id: str, appointment_id: str, body: DecisionBody,
               owner: Annotated[OwnerPrincipal, Depends(principal)],
               request_key: Annotated[str, Depends(key)]) -> object:
        return transition(business_id, appointment_id, body, owner, request_key, Action.CANCEL)

    @app.patch("/v1/owner/businesses/{business_id}/appointments/{appointment_id}")
    def edit_appointment(business_id: str, appointment_id: str, body: EditAppointmentBody,
                         owner: Annotated[OwnerPrincipal, Depends(principal)],
                         request_key: Annotated[str, Depends(key)]) -> object:
        return transition(business_id, appointment_id, body, owner, request_key, Action.EDIT,
                          body.start_at, body.duration_minutes)

    def owner_write(command: Callable[[], OwnerCalendarCommand],
                    current: Callable[[], object | None] | None = None) -> object:
        return run(lambda: calendar.apply(command()), current)

    @app.post("/v1/owner/businesses/{business_id}/blocks")
    def create_block(business_id: str, body: BlockBody,
                     owner: Annotated[OwnerPrincipal, Depends(principal)],
                     request_key: Annotated[str, Depends(key)]) -> object:
        return owner_write(lambda: OwnerCalendarCommand(
            business_id, owner.actor_id, request_key, OwnerAction.CREATE_BLOCK,
            body.expected_revision, start_at=body.start_at, end_at=body.end_at))

    @app.put("/v1/owner/businesses/{business_id}/blocks/{block_id}")
    def move_block(business_id: str, block_id: str, body: MoveBlockBody,
                   owner: Annotated[OwnerPrincipal, Depends(principal)],
                   request_key: Annotated[str, Depends(key)]) -> object:
        return owner_write(lambda: OwnerCalendarCommand(
            business_id, owner.actor_id, request_key, OwnerAction.MOVE_BLOCK,
            body.expected_revision, block_id=block_id, expected_version=body.expected_version,
            start_at=body.start_at, end_at=body.end_at),
            lambda: block_state(block_id, business_id))

    @app.delete("/v1/owner/businesses/{business_id}/blocks/{block_id}")
    def remove_block(business_id: str, block_id: str, body: RemoveBlockBody,
                     owner: Annotated[OwnerPrincipal, Depends(principal)],
                     request_key: Annotated[str, Depends(key)]) -> object:
        return owner_write(lambda: OwnerCalendarCommand(
            business_id, owner.actor_id, request_key, OwnerAction.REMOVE_BLOCK,
            body.expected_revision, block_id=block_id, expected_version=body.expected_version),
            lambda: block_state(block_id, business_id))

    @app.post("/v1/owner/businesses/{business_id}/appointments")
    def create_appointment(business_id: str, body: ManualAppointmentBody,
                           owner: Annotated[OwnerPrincipal, Depends(principal)],
                           request_key: Annotated[str, Depends(key)]) -> object:
        return owner_write(lambda: OwnerCalendarCommand(
            business_id, owner.actor_id, request_key, OwnerAction.CREATE_APPOINTMENT,
            body.expected_revision, client_id=body.client_id, start_at=body.start_at,
            duration_minutes=body.duration_minutes))

    return app


def create_persisted_owner_app(client: DynamoClient, table_name: str,
                               verify_token: OwnerTokenVerifier,
                               clock: Callable[[], datetime] | None = None,
                               *, cors_origins: tuple[str, ...] = ()) -> FastAPI:
    """Build the non-local owner API with strongly read DynamoDB state."""
    return create_owner_app(DynamoDBCalendarRepository(client, table_name), verify_token,
                            clock, DynamoSmsIngressStore(client, table_name),
                            cors_origins=cors_origins)
