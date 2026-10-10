"""Client routes, scoped entirely by a verified server-side identity link.

No route accepts a client or business identifier from the caller; both come from
the active link of the verified token subject.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Protocol, cast

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import (
    AvailabilityRepository,
    AvailabilityService,
    InvalidDuration,
    InvalidPolicy,
)
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.owner_policy import PolicyNotConfigured
from scheduling.identity_links import IdentityLink, LinkRole


class ClientSession(BaseModel):
    role: str = "client"
    business_id: str
    client_id: str


class ClientAvailabilityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date
    duration_minutes: int = Field(gt=0)


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


class ClientBookings(BaseModel):
    bookings: list[ClientBooking]


class ClientBookingRepository(AvailabilityRepository, Protocol):
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

    @app.get("/v1/client/session", response_model=ClientSession)
    def client_session(
        session: Annotated[ClientSession, Depends(linked_client)],
    ) -> ClientSession:
        return session

    if repository is None:
        return
    store = cast(ClientBookingRepository, repository)
    availability_service = AvailabilityService(store)

    @app.get("/v1/client/availability", response_model=ClientAvailability)
    def client_availability(
        session: Annotated[ClientSession, Depends(linked_client)],
        query: Annotated[ClientAvailabilityQuery, Query()],
    ) -> ClientAvailability:
        try:
            starts = availability_service.find_starts(
                session.business_id, query.day, query.duration_minutes, now())
        except InvalidDuration as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (InvalidPolicy, PolicyNotConfigured) as exc:
            raise HTTPException(status_code=503, detail="Booking is unavailable") from exc
        return ClientAvailability(
            day=query.day, duration_minutes=query.duration_minutes, starts_at=list(starts))

    @app.get("/v1/client/bookings", response_model=ClientBookings)
    def client_bookings(
        session: Annotated[ClientSession, Depends(linked_client)],
    ) -> ClientBookings:
        return ClientBookings(bookings=[
            ClientBooking(
                appointment_id=item.appointment_id, status=item.status,
                start_at=item.start_at, end_at=item.end_at,
                duration_minutes=item.duration_minutes,
                hold_expires_at=item.hold_expires_at, requested_at=item.created_at)
            for item in store.read_client_bookings(
                session.business_id, session.client_id, now())])
