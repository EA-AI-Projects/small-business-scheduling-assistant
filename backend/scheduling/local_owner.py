"""Synthetic, in-memory owner API for local frontend development.

Not for deployment. Every record is fictional and lives only in this process;
nothing reaches DynamoDB, Cognito, Twilio or any other cloud service, and no
Twilio SMS routes are mounted. A local text simulator page at ``/local/texts``
shares this calendar (see ``scheduling.local_texts``); it calls OpenAI only when
``OPENAI_API_KEY`` is set. Bearer auth accepts only the token in the
``LOCAL_OWNER_TOKEN`` environment variable (at least 16 characters). Browser
access is allowed only from the local Next.js dev origins on port 3000. Bind it to
127.0.0.1 as shown; nothing here prevents ``--host 0.0.0.0`` from exposing it to the
local network (still behind the token).

Run from the repository root::

    LOCAL_OWNER_TOKEN=<random 16+ chars> backend/.venv/bin/uvicorn \\
        scheduling.local_owner:app --app-dir backend --host 127.0.0.1 --port 8000
"""

import hmac
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.domain.availability import AvailabilityService
from scheduling.domain.client_records import ClientRecordService, HomeSize
from scheduling.domain.conversation import MessageInterpreter
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.owner_calendar import (
    OwnerAction,
    OwnerCalendarCommand,
    OwnerCalendarService,
)
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.local_texts import (
    LocalConsent,
    OfflineInterpreter,
    TextSimulator,
    mount_text_simulator,
)
from scheduling.owner_api import OwnerPrincipal, create_owner_app

BUSINESS_ID = "pilot"
OWNER_ACTOR = "local-owner"
TOKEN_ENV = "LOCAL_OWNER_TOKEN"
MIN_TOKEN_LENGTH = 16
LOCAL_APP_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")
SYNTHETIC_CLIENTS = (
    ("client-1", "Avery Example", "+14155550101", "101 Fictional Lane", HomeSize.SMALL, 90),
    ("client-2", "Blake Sample", "+14155550102", "202 Placeholder Ave", HomeSize.MEDIUM, 120),
    ("client-3", "Casey Demo", "+14155550103", "303 Synthetic Court", HomeSize.LARGE, 180),
)
# Casey stays unverified to show that production ignores texts without verification.
VERIFIED_CLIENTS = ("client-1", "client-2")


class LocalTokenVerifier:
    """Accept exactly one configured bearer token, compared in constant time."""

    def __init__(self, token: str | None) -> None:
        if token is None or len(token) < MIN_TOKEN_LENGTH:
            raise RuntimeError(
                f"Set {TOKEN_ENV} to a random token of at least {MIN_TOKEN_LENGTH} "
                "characters before starting the local owner API."
            )
        self._token = token.encode()

    def __call__(self, token: str) -> OwnerPrincipal:
        if not hmac.compare_digest(token.encode(), self._token):
            raise ValueError("Invalid local owner token")
        return OwnerPrincipal(OWNER_ACTOR, BUSINESS_ID)


def _open_starts(availability: AvailabilityService, first_day: date, duration: int,
                 now: datetime, count: int) -> list[datetime]:
    """Return the first open start on each of the next ``count`` bookable days."""
    starts: list[datetime] = []
    for offset in range(14):
        day_starts = availability.find_starts(
            BUSINESS_ID, first_day + timedelta(days=offset), duration, now)
        if day_starts:
            starts.append(day_starts[0])
        if len(starts) == count:
            break
    return starts


def seed_synthetic_data(repository: InMemoryCalendarRepository, now: datetime) -> None:
    """Seed the pilot policy and a few fictional records within the next few days."""
    clock: Callable[[], datetime] = lambda: now
    policy = OwnerPolicyService(repository, clock).seed(BUSINESS_ID, OWNER_ACTOR, "local-seed")
    maximum = policy.record.policy.maximum_visit_minutes
    clients = ClientRecordService(repository)
    for client_id, name, phone, address, size, minutes in SYNTHETIC_CLIENTS:
        clients.save_profile(BUSINESS_ID, client_id, name, phone, address, size,
                             minutes, True, 0, maximum, now)
        if client_id in VERIFIED_CLIENTS:
            # Stands in for the trusted verification step; never an owner route.
            clients.verify_phone(BUSINESS_ID, client_id, phone, now)

    zone = ZoneInfo(policy.record.policy.timezone)
    tomorrow = now.astimezone(zone).date() + timedelta(days=1)
    availability = AvailabilityService(repository)
    holds = HoldService(repository)
    lifecycle = LifecycleService(repository, clock)

    starts = _open_starts(availability, tomorrow, 90, now, 3)
    if len(starts) < 3:
        return
    confirmed = holds.create(CreateHold(
        BUSINESS_ID, OWNER_ACTOR, "client-1", "local-confirmed", starts[0], 90), now)
    lifecycle.apply(AppointmentCommand(
        BUSINESS_ID, confirmed.hold_id, OWNER_ACTOR, ActorRole.OWNER, Action.APPROVE,
        "local-approve", 1))
    holds.create(CreateHold(
        BUSINESS_ID, OWNER_ACTOR, "client-2", "local-pending", starts[1], 120), now)
    block_start = starts[2]
    OwnerCalendarService(repository, clock).apply(OwnerCalendarCommand(
        BUSINESS_ID, OWNER_ACTOR, "local-block", OwnerAction.CREATE_BLOCK,
        repository.read_revision(BUSINESS_ID), start_at=block_start,
        end_at=block_start + timedelta(hours=1)))
    clients.create_note(BUSINESS_ID, "client-1", confirmed.hold_id,
                        "Synthetic note: prefers the side entrance.", OWNER_ACTOR, now,
                        "local-note-1")


def create_local_owner_app(token: str | None, now: datetime | None = None,
                           interpreter: MessageInterpreter | None = None,
                           clock: Callable[[], datetime] | None = None) -> FastAPI:
    """Build the synthetic owner API; raises before serving if the token is weak."""
    verifier = LocalTokenVerifier(token)
    repository = InMemoryCalendarRepository()
    current = clock or (lambda: datetime.now(UTC))
    seed_synthetic_data(repository, now or current())
    app = create_owner_app(repository, verifier, clock, invitation_consent=LocalConsent(repository),
                           manual_invitation_enabled=lambda: True,
                           cors_origins=LOCAL_APP_ORIGINS,
                           allow_loopback_http=True)
    simulator = TextSimulator(repository, interpreter or OfflineInterpreter(), BUSINESS_ID,
                              current)
    mount_text_simulator(app, simulator, verifier)
    return app


def __getattr__(name: str) -> FastAPI:
    # Build lazily so importing this module (e.g. in tests) needs no environment.
    if name == "app":
        key = os.environ.get("OPENAI_API_KEY")
        return create_local_owner_app(
            os.environ.get(TOKEN_ENV),
            interpreter=OpenAIMessageInterpreter(key) if key else None)
    raise AttributeError(name)
