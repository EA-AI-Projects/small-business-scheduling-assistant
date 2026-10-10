"""Client routes, scoped entirely by a verified server-side identity link.

No route accepts a client or business identifier from the caller; both come from
the active link of the verified token subject.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Protocol, cast
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
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
from scheduling.domain.holds import (
    CreateHold,
    HoldRepository,
    HoldService,
    IdempotencyKeyReused,
    PendingHold,
    SlotConflict,
    TooManyConflicts,
)
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


class ClientRequest(BaseModel):
    """The only caller input: a start the client chose from the offered times.

    Client, business, visit length, and actor are never accepted; they come from the link
    and the owner-set profile.
    """

    model_config = ConfigDict(extra="forbid")

    start_at: datetime

    def validated(self) -> datetime:
        if self.start_at.tzinfo is None:
            raise ValueError("Start must include a UTC offset")
        return self.start_at


class ClientBookingRepository(AvailabilityRepository, Protocol):
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...

    def read_client_bookings(self, business_id: str, client_id: str,
                             now: datetime) -> tuple[Appointment, ...]: ...


def _booking(hold: PendingHold) -> ClientBooking:
    return ClientBooking(
        appointment_id=hold.hold_id, status=CalendarStatus.PENDING_APPROVAL,
        start_at=hold.start_at, end_at=hold.end_at, duration_minutes=hold.duration_minutes,
        hold_expires_at=hold.hold_expires_at, requested_at=hold.created_at)


def _conflict(code: str, message: str, alternatives: tuple[datetime, ...]) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": code, "message": message,
        "alternatives": [value.isoformat() for value in alternatives]})


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
    hold_store = cast(HoldRepository, repository) if repository is not None else None

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


    assert hold_store is not None
    holds = HoldService(hold_store)

    @app.post("/v1/client/requests", response_model=ClientBooking)
    def client_request(
        session: Annotated[ClientSession, Depends(linked_client)],
        body: ClientRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    ) -> ClientBooking:
        """Ask the owner for a visit: the SMS path's atomic hold, so it stays pending."""
        profile = store.read_profile(session.business_id, session.client_id)
        # The SMS path also needs a verified phone, because the hold queues a client text.
        if profile is None or not profile.active:
            raise HTTPException(status_code=503, detail="Booking is unavailable")
        if profile.phone_verified_at is None:
            raise HTTPException(status_code=409, detail={
                "code": "PROFILE_INCOMPLETE",
                "message": "Please contact the owner to complete your client profile.",
                "alternatives": []})
        try:
            start = body.validated()
            hold = holds.create(CreateHold(
                session.business_id, session.client_id, session.client_id, idempotency_key,
                start, profile.default_duration_minutes), now())
        except SlotConflict as exc:
            raise _conflict("SLOT_CONFLICT", "That time is no longer open.",
                            exc.alternatives) from exc
        except TooManyConflicts as exc:
            # The calendar kept changing; nothing was held. Offer what is open right now.
            day = start.astimezone(ZoneInfo(store.read_policy(session.business_id).timezone)).date()
            raise _conflict(
                "CALENDAR_BUSY", "The calendar changed while requesting. Please try again.",
                tuple(availability_service.find_starts(
                    session.business_id, day, profile.default_duration_minutes, now())[:5])
            ) from exc
        except IdempotencyKeyReused as exc:
            raise HTTPException(status_code=409, detail={
                "code": "IDEMPOTENCY_KEY_REUSED", "message": str(exc),
                "alternatives": []}) from exc
        except (InvalidDuration, InvalidPolicy, PolicyNotConfigured) as exc:
            raise HTTPException(status_code=503, detail="Booking is unavailable") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _booking(hold)
