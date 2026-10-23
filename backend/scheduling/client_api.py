"""Client routes, scoped entirely by a verified server-side identity link.

No route accepts a client or business identifier from the caller; both come from
the active link of the verified token subject.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Protocol, cast
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
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
from scheduling.domain.client_records import (
    ClientProfile,
    ClientRecordRepository,
    ClientRecordService,
    RecordConflict,
    RecordNotFound,
)
from scheduling.domain.holds import (
    LOOKUP_MINUTES,
    CreateHold,
    HoldRepository,
    HoldService,
    IdempotencyKeyReused,
    InvalidReplacement,
    PendingHold,
    ReplacementPending,
    SlotConflict,
    TooManyConflicts,
)
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    InvalidTransition,
    LifecycleRepository,
    LifecycleService,
    StaleVersion,
)
from scheduling.domain.owner_policy import PolicyNotConfigured
from scheduling.domain.recommendations import recommended_starts
from scheduling.domain.reminders import reminder_preference_view
from scheduling.domain.scheduling_preferences import SchedulingPreferencesRecord
from scheduling.domain.service_duration import (
    ServiceDurationChanged,
    ServiceDurationSource,
    ServiceNotConfigured,
    service_duration,
)
from scheduling.domain.sms_ingress import SmsIngressStore
from scheduling.identity_links import IdentityLink, LinkRole


class ClientSession(BaseModel):
    role: str = "client"
    business_id: str
    client_id: str
    # Business IANA time zone for rendering times and choosing a day; None when unconfigured.
    timezone: str | None = None
    # Days ahead the business takes bookings, so the app can stop offering times past it.
    booking_horizon_days: int | None = None
    # The saved business and service names (#348); None until the owner saves the profile.
    business_name: str | None = None
    service_name: str | None = None


class ClientReminderPreference(BaseModel):
    enabled: bool
    version: int
    blockers: list[str]


class ClientReminderPreferenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(strict=True)
    expected_version: int = Field(gt=0, strict=True)


class ClientAvailabilityQuery(BaseModel):
    """Only the day; the visit length is the business's saved default duration."""

    model_config = ConfigDict(extra="forbid")

    day: date


class ClientAvailability(BaseModel):
    day: date
    duration_minutes: int
    starts_at: list[datetime]
    # A subset of starts_at the business recommends; every start stays selectable. Neutral by
    # design: no owner text, reason, or other client's information.
    recommended_starts_at: list[datetime] = Field(default_factory=list)


class ClientBooking(BaseModel):
    """Portal-safe view: no client, note, version, or owner-only fields."""

    appointment_id: str
    status: CalendarStatus
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    hold_expires_at: datetime | None
    requested_at: datetime | None
    # The state the client saw; a cancel or reschedule must quote it, so a stale view is refused.
    version: int
    # Set on a pending replacement: the confirmed visit it would swap for on owner approval.
    replaces_appointment_id: str | None = None


class ClientBookingsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientBookings(BaseModel):
    bookings: list[ClientBooking]


class ClientRequest(BaseModel):
    """The only caller input: a start the client chose from the offered times.

    Client, business, and actor are never accepted; they come from the link. The visit
    length is the business's saved default. A client may echo the length the offered
    times were quoted at, and a request is refused when the owner has changed it since.
    """

    model_config = ConfigDict(extra="forbid")

    start_at: datetime
    quoted_duration_minutes: int | None = Field(default=None, gt=0)

    def validated(self) -> datetime:
        if self.start_at.tzinfo is None:
            raise ValueError("Start must include a UTC offset")
        return self.start_at


class ClientCancel(BaseModel):
    """Only the version of the booking the client confirmed cancelling."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(gt=0)


class ClientReschedule(BaseModel):
    """The new start the client chose and the version of the confirmed visit it moves."""

    model_config = ConfigDict(extra="forbid")

    start_at: datetime
    expected_version: int = Field(gt=0)
    quoted_duration_minutes: int | None = Field(default=None, gt=0)

    def validated(self) -> datetime:
        if self.start_at.tzinfo is None:
            raise ValueError("Start must include a UTC offset")
        return self.start_at


class ClientBookingRepository(AvailabilityRepository, ServiceDurationSource, Protocol):
    def read_appointment(self, appointment_id: str) -> Appointment | None: ...

    def read_scheduling_preferences(self, business_id: str) -> SchedulingPreferencesRecord: ...

    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...

    def read_client_bookings(self, business_id: str, client_id: str,
                             now: datetime) -> tuple[Appointment, ...]: ...


def _view(item: Appointment) -> ClientBooking:
    return ClientBooking(
        appointment_id=item.appointment_id, status=item.status,
        start_at=item.start_at, end_at=item.end_at, duration_minutes=item.duration_minutes,
        hold_expires_at=item.hold_expires_at, requested_at=item.created_at,
        version=item.version, replaces_appointment_id=item.replaces_appointment_id)


def _booking(hold: PendingHold, current: Appointment | None, at: datetime) -> ClientBooking:
    """The request's state now: a replayed key must not report a stale pending hold."""
    status = CalendarStatus.PENDING_APPROVAL
    if current is not None:
        status = current.status
        if status == CalendarStatus.PENDING_APPROVAL and (
                current.hold_expires_at is None or current.hold_expires_at <= at):
            status = CalendarStatus.EXPIRED
    return ClientBooking(
        appointment_id=hold.hold_id, status=status,
        start_at=hold.start_at, end_at=hold.end_at, duration_minutes=hold.duration_minutes,
        hold_expires_at=hold.hold_expires_at, requested_at=hold.created_at,
        version=current.version if current is not None else 1,
        replaces_appointment_id=hold.replaces_appointment_id)


def _conflict(code: str, message: str, alternatives: tuple[datetime, ...]) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": code, "message": message,
        "alternatives": [value.isoformat() for value in alternatives]})


def _refused(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": code, "message": message, "alternatives": []})


def add_client_session_route(
    app: FastAPI, verify_token: Callable[[str], IdentityLink],
    repository: object | None = None,
    clock: Callable[[], datetime] | None = None,
    *, sms_store: SmsIngressStore | None = None,
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
            policy = store.read_policy(session.business_id)
            zone: str | None = policy.timezone
            horizon: int | None = policy.booking_horizon_days
        except (InvalidPolicy, PolicyNotConfigured):
            zone = None
            horizon = None
        try:
            saved = store.read_business_profile(session.business_id).profile
        except (OSError, ValueError, KeyError, RuntimeError):
            saved = None
        return session.model_copy(update={
            "timezone": zone, "booking_horizon_days": horizon,
            "business_name": saved.display_name if saved is not None else None,
            "service_name": saved.service_name if saved is not None else None})

    if store is None:
        return
    clients = ClientRecordService(cast(ClientRecordRepository, repository))

    def preference(session: ClientSession) -> ClientReminderPreference:
        profile = store.read_profile(session.business_id, session.client_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Client profile not found")
        return ClientReminderPreference.model_validate(reminder_preference_view(profile, sms_store))

    @app.get("/v1/client/reminder-preference", response_model=ClientReminderPreference)
    def get_client_reminder_preference(
        session: Annotated[ClientSession, Depends(linked_client)],
        query: Annotated[ClientBookingsQuery, Query()],
    ) -> ClientReminderPreference:
        del query
        return preference(session)

    @app.put("/v1/client/reminder-preference", response_model=ClientReminderPreference)
    def set_client_reminder_preference(
        session: Annotated[ClientSession, Depends(linked_client)],
        body: ClientReminderPreferenceBody,
    ) -> ClientReminderPreference:
        try:
            profile = clients.set_reminder_preference(
                session.business_id, session.client_id, body.enabled, body.expected_version, now())
        except RecordNotFound as exc:
            raise HTTPException(status_code=404, detail="Client profile not found") from exc
        except RecordConflict as exc:
            raise _refused("RECORD_CONFLICT", "Your profile changed. Reload your saved preference.") from exc
        return ClientReminderPreference.model_validate(reminder_preference_view(profile, sms_store))

    availability_service = AvailabilityService(store)

    def current_duration(business_id: str) -> int:
        """The saved service duration, checked against the current policy."""
        return service_duration(store, business_id, store.read_policy(business_id))

    @app.get("/v1/client/availability", response_model=ClientAvailability)
    def client_availability(
        session: Annotated[ClientSession, Depends(linked_client)],
        query: Annotated[ClientAvailabilityQuery, Query()],
    ) -> ClientAvailability:
        profile = store.read_profile(session.business_id, session.client_id)
        if profile is None or not profile.active:
            raise HTTPException(status_code=503, detail="Booking is unavailable")
        try:
            duration = current_duration(session.business_id)
            starts = availability_service.find_starts(
                session.business_id, query.day, duration, now())
        except (InvalidDuration, InvalidPolicy, PolicyNotConfigured,
                ServiceNotConfigured) as exc:
            # A missing or policy-rejected duration is the owner's to fix, not the client's.
            raise HTTPException(status_code=503, detail="Booking is unavailable") from exc
        try:
            preferences = store.read_scheduling_preferences(session.business_id).preferences
            flagged = recommended_starts(
                preferences, store.read_policy(session.business_id).timezone, starts,
                session.client_id)
        except Exception:  # noqa: BLE001 - optional guidance must not hide availability
            # Log the failure type only: never owner text or client data.
            logging.getLogger(__name__).warning(
                "Scheduling preferences unavailable; no recommendations", exc_info=False)
            flagged = frozenset()
        return ClientAvailability(
            day=query.day, duration_minutes=duration, starts_at=list(starts),
            recommended_starts_at=[start for start in starts if start in flagged])

    @app.get("/v1/client/bookings", response_model=ClientBookings)
    def client_bookings(
        session: Annotated[ClientSession, Depends(linked_client)],
        query: Annotated[ClientBookingsQuery, Query()],
    ) -> ClientBookings:
        del query
        return ClientBookings(bookings=[
            _view(item) for item in store.read_client_bookings(
                session.business_id, session.client_id, now())])

    assert hold_store is not None
    holds = HoldService(hold_store)
    lifecycle = LifecycleService(cast(LifecycleRepository, repository), now)

    def valid_start(body: ClientRequest | ClientReschedule) -> datetime:
        try:
            return body.validated()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def usable_profile(session: ClientSession) -> ClientProfile:
        profile = store.read_profile(session.business_id, session.client_id)
        # The SMS path also needs a verified phone, because the hold queues a client text.
        if profile is None or not profile.active:
            raise HTTPException(status_code=503, detail="Booking is unavailable")
        if profile.phone_verified_at is None:
            raise HTTPException(status_code=409, detail={
                "code": "PROFILE_INCOMPLETE",
                "message": "Please contact the owner to complete your client profile.",
                "alternatives": []})
        return profile

    def booking_duration(business_id: str, quoted: int | None) -> int:
        """The saved default now; a quoted length the owner has since changed is refused."""
        try:
            minutes = current_duration(business_id)
        except (InvalidDuration, InvalidPolicy, PolicyNotConfigured,
                ServiceNotConfigured) as exc:
            raise HTTPException(status_code=503, detail="Booking is unavailable") from exc
        if quoted is not None and quoted != minutes:
            raise _conflict("DURATION_CHANGED",
                            "The visit length changed. Please choose from the new times.", ())
        return minutes

    def stored_hold(lookup: CreateHold) -> PendingHold | None:
        try:
            return holds.existing(lookup)
        except IdempotencyKeyReused as exc:
            raise _refused("IDEMPOTENCY_KEY_REUSED", str(exc)) from exc

    def create_hold(command: CreateHold, start: datetime) -> PendingHold:
        """The SMS path's atomic hold with the portal's error mapping."""
        try:
            return holds.create(command, now())
        except SlotConflict as exc:
            raise _conflict("SLOT_CONFLICT", "That time is no longer open.",
                            exc.alternatives) from exc
        except TooManyConflicts as exc:
            # The calendar kept changing; nothing was held. Offer what is open right now.
            try:
                day = start.astimezone(
                    ZoneInfo(store.read_policy(command.business_id).timezone)).date()
                current = tuple(availability_service.find_starts(
                    command.business_id, day, command.duration_minutes, now())[:5])
            except (InvalidDuration, InvalidPolicy, PolicyNotConfigured) as inner:
                raise HTTPException(
                    status_code=503, detail="Booking is unavailable") from inner
            raise _conflict(
                "CALENDAR_BUSY", "The calendar changed while requesting. Please try again.",
                current) from exc
        except IdempotencyKeyReused as exc:
            raise HTTPException(status_code=409, detail={
                "code": "IDEMPOTENCY_KEY_REUSED", "message": str(exc),
                "alternatives": []}) from exc
        except ReplacementPending as exc:
            raise _refused("REPLACEMENT_PENDING",
                           "A replacement for this visit is already waiting for the owner.") from exc
        except InvalidReplacement as exc:
            raise _refused("STALE_BOOKING",
                           "This visit changed. Please review it before trying again.") from exc
        except ServiceDurationChanged as exc:
            raise _conflict("DURATION_CHANGED",
                            "The visit length changed. Please choose from the new times.",
                            ()) from exc
        except (InvalidDuration, InvalidPolicy, PolicyNotConfigured,
                ServiceNotConfigured) as exc:
            raise HTTPException(status_code=503, detail="Booking is unavailable") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/client/requests", response_model=ClientBooking)
    def client_request(
        session: Annotated[ClientSession, Depends(linked_client)],
        body: ClientRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    ) -> ClientBooking:
        """Ask the owner for a visit: the SMS path's atomic hold, so it stays pending."""
        usable_profile(session)
        start = valid_start(body)

        def command(minutes: int) -> CreateHold:
            return CreateHold(
                session.business_id, session.client_id, session.client_id,
                # Namespaced so a caller-chosen key can never meet the SMS path's provider IDs.
                f"portal:{idempotency_key}", start, minutes, service_duration_minutes=minutes)

        # A retry of a saved request reports it even if the default changed since: the
        # stored result is found by key before the current default is read.
        stored = stored_hold(command(LOOKUP_MINUTES))
        if stored is not None:
            return _booking(stored, store.read_appointment(stored.hold_id), now())
        minutes = booking_duration(session.business_id, body.quoted_duration_minutes)
        hold = create_hold(command(minutes), start)
        return _booking(hold, store.read_appointment(hold.hold_id), now())

    def own_booking(session: ClientSession, appointment_id: str) -> Appointment:
        """One response for absent and not-yours, so ids cannot be probed across clients."""
        found = store.read_appointment(appointment_id)
        if (found is None or found.business_id != session.business_id
                or found.client_id != session.client_id):
            raise HTTPException(status_code=404, detail="Appointment not found")
        return found

    @app.post("/v1/client/bookings/{appointment_id}/cancel", response_model=ClientBooking)
    def client_cancel(
        session: Annotated[ClientSession, Depends(linked_client)],
        appointment_id: str,
        body: ClientCancel,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    ) -> ClientBooking:
        """Cancel one of the caller's own live bookings: the SMS path's CANCEL transition.

        The client's confirmation step names the version it was shown; the lifecycle
        refuses a changed booking, and its notices go to the owner and the client.
        """
        target = own_booking(session, appointment_id)
        at = now()
        live = target.status in (CalendarStatus.CONFIRMED, CalendarStatus.PENDING_APPROVAL)
        if live and (target.end_at <= at or not target.occupies_time(at)):
            # The text path never lists a visit that ended or a hold that lapsed.
            raise _refused("BOOKING_NOT_ACTIVE", "This appointment is no longer active.")
        try:
            result = lifecycle.apply(AppointmentCommand(
                session.business_id, appointment_id, session.client_id, ActorRole.CLIENT,
                Action.CANCEL, f"portal:{idempotency_key}", body.expected_version))
        except StaleVersion as exc:
            raise _refused("STALE_BOOKING",
                           "This appointment changed. Please review it before trying again.",
                           ) from exc
        except ReplacementPending as exc:
            raise _refused("REPLACEMENT_PENDING",
                           "A replacement request is waiting. Withdraw it first.") from exc
        except InvalidTransition as exc:
            raise _refused("BOOKING_NOT_ACTIVE", "This appointment is no longer active.") from exc
        except TooManyConflicts as exc:
            raise _refused("CALENDAR_BUSY",
                           "The calendar changed while cancelling. Please try again.") from exc
        except IdempotencyKeyReused as exc:
            raise _refused("IDEMPOTENCY_KEY_REUSED", str(exc)) from exc
        return _view(result.appointment)

    @app.post("/v1/client/bookings/{appointment_id}/reschedule", response_model=ClientBooking)
    def client_reschedule(
        session: Annotated[ClientSession, Depends(linked_client)],
        appointment_id: str,
        body: ClientReschedule,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    ) -> ClientBooking:
        """Request a replacement time; the confirmed original stays until the owner approves.

        This is the text path's reschedule: a pending hold that names the original, so the
        owner's approval swaps the two atomically and a decline or expiry keeps the original.
        """
        usable_profile(session)
        start = valid_start(body)

        def command(minutes: int) -> CreateHold:
            return CreateHold(
                session.business_id, session.client_id, session.client_id,
                f"portal:{idempotency_key}", start, minutes,
                appointment_id, replaces_confirmed_version=body.expected_version,
                service_duration_minutes=minutes)

        original = own_booking(session, appointment_id)
        # A retry reports the replacement's state, found by key before the default is read.
        existing = stored_hold(command(LOOKUP_MINUTES))
        if existing is not None:
            return _booking(existing, store.read_appointment(existing.hold_id), now())
        if original.status != CalendarStatus.CONFIRMED or original.end_at <= now():
            raise _refused("BOOKING_NOT_RESCHEDULABLE",
                           "Only an upcoming confirmed appointment can be rescheduled.")
        if original.version != body.expected_version:
            raise _refused("STALE_BOOKING",
                           "This appointment changed. Please review it before trying again.")
        hold = create_hold(command(booking_duration(
            session.business_id, body.quoted_duration_minutes)), start)
        return _booking(hold, store.read_appointment(hold.hold_id), now())
