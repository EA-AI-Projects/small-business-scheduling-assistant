"""Client routes, scoped entirely by a verified server-side identity link.

No route accepts a client or business identifier from the caller; both come from
the active link of the verified token subject.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Protocol, cast

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import (
    AvailabilityRepository,
    AvailabilityService,
    InvalidDuration,
    InvalidPolicy,
)
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.owner_policy import PolicyNotConfigured
from scheduling.identity_links import IdentityLink, LinkRole


class ClientSession(BaseModel):
    role: str = "client"
    business_id: str
    client_id: str
    # Business IANA time zone for rendering times and choosing a day; None when unconfigured.
    timezone: str | None = None


class ClientAvailabilityQuery(BaseModel):
    """Only the day; the visit length comes from the linked client's owner-set profile."""

    model_config = ConfigDict(extra="forbid")

    day: date


class ClientAvailability(BaseModel):
    day: date
    duration_minutes: int
    starts_at: list[datetime]


class ClientBooking(BaseModel):
    """Portal-safe view: no client, note, version, or owner-only fields."""

    appointment_id: str
    status: CalendarStatus
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    hold_expires_at: datetime | None
    requested_at: datetime | None


class ClientBookingsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientBookings(BaseModel):
    bookings: list[ClientBooking]


class ClientBookingRepository(AvailabilityRepository, Protocol):
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...

    def read_client_bookings(self, business_id: str, client_id: str,
                             now: datetime) -> tuple[Appointment, ...]: ...


def add_client_session_route(
    app: FastAPI, verify_token: Callable[[str], IdentityLink],
    repository: object | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Expose the caller's linked identifiers and, with a repository, own bookings."""
    security = HTTPBearer(auto_error=False)
    now = clock or (lambda: datetime.now(UTC))

    def linked_client(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> ClientSession:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Client authentication is required")
        try:
            link = verify_token(credentials.credentials)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid client credentials") from exc
        if link.role != LinkRole.CLIENT or not link.client_id:
            raise HTTPException(status_code=403, detail="Client access is required")
        return ClientSession(business_id=link.business_id, client_id=link.client_id)

    store = cast(ClientBookingRepository, repository) if repository is not None else None

    @app.get("/v1/client/session", response_model=ClientSession)
    def client_session(
        session: Annotated[ClientSession, Depends(linked_client)],
    ) -> ClientSession:
        if store is None:
            return session
        try:
            zone: str | None = store.read_policy(session.business_id).timezone
        except (InvalidPolicy, PolicyNotConfigured):
            zone = None
        return session.model_copy(update={"timezone": zone})

    if store is None:
        return
    availability_service = AvailabilityService(store)

    @app.get("/v1/client/availability", response_model=ClientAvailability)
    def client_availability(
        session: Annotated[ClientSession, Depends(linked_client)],
        query: Annotated[ClientAvailabilityQuery, Query()],
    ) -> ClientAvailability:
        profile = store.read_profile(session.business_id, session.client_id)
        if profile is None or not profile.active:
            raise HTTPException(status_code=503, detail="Booking is unavailable")
        duration = profile.default_duration_minutes
        try:
            starts = availability_service.find_starts(
                session.business_id, query.day, duration, now())
        except (InvalidDuration, InvalidPolicy, PolicyNotConfigured) as exc:
            # A stored duration the policy rejects is the owner's to fix, not the client's.
            raise HTTPException(status_code=503, detail="Booking is unavailable") from exc
        return ClientAvailability(
            day=query.day, duration_minutes=duration, starts_at=list(starts))

    @app.get("/v1/client/bookings", response_model=ClientBookings)
    def client_bookings(
        session: Annotated[ClientSession, Depends(linked_client)],
        query: Annotated[ClientBookingsQuery, Query()],
    ) -> ClientBookings:
        del query
        return ClientBookings(bookings=[
            ClientBooking(
                appointment_id=item.appointment_id, status=item.status,
                start_at=item.start_at, end_at=item.end_at,
                duration_minutes=item.duration_minutes,
                hold_expires_at=item.hold_expires_at, requested_at=item.created_at)
            for item in store.read_client_bookings(
                session.business_id, session.client_id, now())])
