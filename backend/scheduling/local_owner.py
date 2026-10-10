"""Synthetic, in-memory owner API for local frontend development.

Not for deployment. Every record is fictional and lives only in this process;
nothing reaches DynamoDB, Cognito, Twilio or any other cloud service, and no
Twilio SMS routes are mounted. A local text simulator page at ``/local/texts``
shares this calendar (see ``scheduling.local_texts``); it calls OpenAI only when
``OPENAI_API_KEY`` is set. Bearer auth accepts only the token in the
``LOCAL_OWNER_TOKEN`` environment variable (at least 16 characters). Browser
access is allowed only from the local Next.js dev origins on port 3000. The real client
routes (``scheduling.client_api``) are mounted too, for the verified synthetic clients only.
Each client signs in with its own random token, generated at startup and printed to the
console; a client token is never accepted by an owner route, nor the owner token by a client
route. Bind it to
127.0.0.1 as shown; nothing here prevents ``--host 0.0.0.0`` from exposing it to the
local network (still behind the token).

Run from the repository root::

    LOCAL_OWNER_TOKEN=<random 16+ chars> backend/.venv/bin/uvicorn \\
        scheduling.local_owner:app --app-dir backend --host 127.0.0.1 --port 8000
"""

import hmac
import os
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.client_api import add_client_session_route
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
from scheduling.identity_links import IdentityLink, LinkRole, LinkState
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


class LocalClientTokenVerifier:
    """Map each generated local token to exactly one verified synthetic client link."""

    def __init__(self, tokens: Mapping[str, str], now: Callable[[], datetime]) -> None:
        if set(tokens) != set(VERIFIED_CLIENTS):
            raise RuntimeError("Local client tokens must cover exactly the verified clients.")
        values = list(tokens.values())
        if len(set(values)) != len(values) or any(len(v) < MIN_TOKEN_LENGTH for v in values):
            raise RuntimeError(
                f"Local client tokens must be distinct and at least {MIN_TOKEN_LENGTH} characters.")
        self._tokens = {client_id: token.encode() for client_id, token in tokens.items()}
        self._now = now

    def __call__(self, token: str) -> IdentityLink:
        match: str | None = None
        for client_id, expected in self._tokens.items():  # Compare all: no early exit.
            if hmac.compare_digest(token.encode(), expected):
                match = client_id
        if match is None:
            raise ValueError("Invalid local client token")
        return IdentityLink(
            f"local-client:{match}", LinkRole.CLIENT, BUSINESS_ID, match, LinkState.ACTIVE, 1,
            self._now(), "local-harness", "local-client-token")


def generate_client_tokens() -> dict[str, str]:
    """A fresh random token per verified synthetic client, for this process only."""
    return {client_id: secrets.token_hex(16) for client_id in VERIFIED_CLIENTS}


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
                           clock: Callable[[], datetime] | None = None,
                           client_tokens: Mapping[str, str] | None = None) -> FastAPI:
    """Build the synthetic owner and client API; raises before serving if a token is weak.

    ``client_tokens`` maps each verified synthetic client id to its sign-in token; the
    caller learns them from it (the module-level ``app`` prints generated ones).
    """
    verifier = LocalTokenVerifier(token)
    client_verifier = LocalClientTokenVerifier(
        generate_client_tokens() if client_tokens is None else client_tokens,
        clock or (lambda: datetime.now(UTC)))
    if token in {value for value in (client_tokens or {}).values()}:
        raise RuntimeError("The owner token must differ from every client token.")
    repository = InMemoryCalendarRepository()
    current = clock or (lambda: datetime.now(UTC))
    seed_synthetic_data(repository, now or current())
    app = create_owner_app(repository, verifier, clock, invitation_consent=LocalConsent(repository),
                           manual_invitation_enabled=lambda: True,
                           cors_origins=LOCAL_APP_ORIGINS,
                           allow_loopback_http=True)
    # The production client routes, on the same repository and clock as the owner routes.
    add_client_session_route(app, client_verifier, repository, clock)
    simulator = TextSimulator(repository, interpreter or OfflineInterpreter(), BUSINESS_ID,
                              current)
    mount_text_simulator(app, simulator, verifier)
    return app


_built: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    # Build lazily so importing this module (e.g. in tests) needs no environment. Build once:
    # a second build would mint client tokens different from the ones already printed.
    global _built
    if name == "app":
        if _built is None:
            _built = _build_from_environment()
        return _built
    raise AttributeError(name)


def _build_from_environment() -> FastAPI:
    key = os.environ.get("OPENAI_API_KEY")
    client_tokens = generate_client_tokens()
    built = create_local_owner_app(
        os.environ.get(TOKEN_ENV), client_tokens=client_tokens,
        interpreter=OpenAIMessageInterpreter(key) if key else None)
    names = {client_id: person for client_id, person, *_ in SYNTHETIC_CLIENTS}
    print("Local client sign-in tokens (synthetic; valid until this process stops):",
          flush=True)
    for client_id, value in client_tokens.items():
        print(f"  {names[client_id]} ({client_id}): {value}", flush=True)
    return built
