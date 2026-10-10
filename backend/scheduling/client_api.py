"""Minimal client route, scoped entirely by a verified server-side identity link."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import (
    AvailabilityPolicy,
    InvalidDuration,
    InvalidPolicy,
    available_starts,
)
from scheduling.domain.calendar import CalendarSnapshot, CalendarStatus
from scheduling.domain.client_records import ClientProfile
from scheduling.identity_links import IdentityLink, LinkRole


class ClientSession(BaseModel):
    role: str = "client"
    business_id: str
    client_id: str


class ClientAvailability(BaseModel):
    """Bookable starts only; no reason is given for any time that is missing."""

    date: date
    timezone: str
    duration_minutes: int
    starts: list[datetime]


class ClientBooking(BaseModel):
    appointment_id: str
    start_at: datetime
    end_at: datetime
    status: CalendarStatus
    hold_expires_at: datetime | None
    replaces_appointment_id: str | None
    duration_minutes: int


class ClientBookings(BaseModel):
    bookings: list[ClientBooking]


class ClientPortalRepository(Protocol):
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...
    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...
    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...
    def list_client_appointments(
        self, business_id: str, client_id: str) -> tuple[Appointment, ...]: ...


def _client_link(
    verify_token: Callable[[str], IdentityLink],
    credentials: HTTPAuthorizationCredentials | None,
) -> IdentityLink:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Client authentication is required")
    try:
        link = verify_token(credentials.credentials)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid client credentials") from exc
    if link.role != LinkRole.CLIENT or not link.client_id:
        raise HTTPException(status_code=403, detail="Client access is required")
    return link


def add_client_session_route(
    app: FastAPI, verify_token: Callable[[str], IdentityLink]
) -> None:
    """Expose only the caller's linked identifiers; no browser-supplied scope."""
    security = HTTPBearer(auto_error=False)

    @app.get("/v1/client/session", response_model=ClientSession)
    def client_session(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> ClientSession:
        link = _client_link(verify_token, credentials)
        assert link.client_id is not None
        return ClientSession(business_id=link.business_id, client_id=link.client_id)


def add_client_portal_routes(
    app: FastAPI,
    verify_token: Callable[[str], IdentityLink],
    repository: ClientPortalRepository,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Client reads. Business and client come only from the verified identity link."""
    security = HTTPBearer(auto_error=False)
    now = clock or (lambda: datetime.now(UTC))

    def active_profile(link: IdentityLink) -> ClientProfile:
        assert link.client_id is not None
        profile = repository.read_profile(link.business_id, link.client_id)
        if profile is None or not profile.active:
            raise HTTPException(status_code=403, detail="Client access is required")
        return profile

    @app.get("/v1/client/availability", response_model=ClientAvailability)
    def client_availability(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
        day: Annotated[date, Query(alias="date")],
    ) -> ClientAvailability:
        link = _client_link(verify_token, credentials)
        profile = active_profile(link)
        policy = repository.read_policy(link.business_id)
        snapshot = repository.read_calendar(link.business_id)
        try:
            starts = available_starts(
                policy, day, profile.default_duration_minutes, snapshot.events, now())
        except (InvalidDuration, InvalidPolicy) as exc:
            raise HTTPException(status_code=409, detail="Availability is unavailable") from exc
        return ClientAvailability(
            date=day, timezone=policy.timezone,
            duration_minutes=profile.default_duration_minutes, starts=list(starts))

    @app.get("/v1/client/bookings", response_model=ClientBookings)
    def client_bookings(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> ClientBookings:
        link = _client_link(verify_token, credentials)
        active_profile(link)
        assert link.client_id is not None
        current = now()
        bookings = []
        for item in repository.list_client_appointments(link.business_id, link.client_id):
            # Defense in depth: never trust the adapter to have filtered by owner.
            if item.business_id != link.business_id or item.client_id != link.client_id:
                continue
            status = item.status
            if (status == CalendarStatus.PENDING_APPROVAL and item.hold_expires_at is not None
                    and item.hold_expires_at <= current):
                status = CalendarStatus.EXPIRED
            bookings.append(ClientBooking(
                appointment_id=item.appointment_id, start_at=item.start_at,
                end_at=item.end_at, status=status, hold_expires_at=item.hold_expires_at,
                replaces_appointment_id=item.replaces_appointment_id,
                duration_minutes=item.duration_minutes))
        return ClientBookings(bookings=bookings)
