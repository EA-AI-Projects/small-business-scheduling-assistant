"""Selection of future booking invitations; this module never sends text."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import AvailabilityPolicy
from scheduling.domain.booking_outreach import OutreachRecord
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.sms_ingress import ConsentEvidence


@dataclass(frozen=True)
class InvitationIntent:
    business_id: str
    client_id: str
    intent_id: str
    run_at: datetime
    window_end_at: datetime
    repeat_cutoff_at: datetime
    phone_hash: str
    verified_at: datetime
    lookahead_weeks: int


@dataclass(frozen=True)
class StoredInvitation:
    intent: InvitationIntent
    state: str
    provider_id: str | None = None


INVITATION_TEMPLATE = "booking_invitation"


def invitation_outbox_id(intent_id: str) -> str:
    return f"booking-invitation#{intent_id}"


@dataclass(frozen=True)
class SelectionReport:
    scheduled: bool
    examined: int
    queued: int
    reasons: dict[str, int]


class InvitationRepository(Protocol):
    def read_outreach(self, business_id: str) -> OutreachRecord: ...
    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...
    def list_profiles(self, business_id: str) -> tuple[ClientProfile, ...]: ...
    def read_confirmed_for_client(self, business_id: str, client_id: str,
                                  run_at: datetime, end_at: datetime) -> tuple[int, tuple[Appointment, ...]]: ...
    def reserve_invitation(self, intent: InvitationIntent,
                           settings_version: int, calendar_revision: int,
                           profile: ClientProfile) -> bool: ...
    def complete_invitation(self, intent: InvitationIntent,
                            handed_off_at: datetime | None,
                            provider_id: str | None = None) -> bool: ...
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...
    def read_last_invitation_sent(self, business_id: str, client_id: str) -> datetime | None: ...
    def queued_invitations(self, business_id: str, limit: int = 100) -> tuple[StoredInvitation, ...]: ...
    def promote_invitation(self, intent: InvitationIntent,
                           settings_version: int, now: datetime) -> bool: ...


class ConsentRepository(Protocol):
    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None: ...
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool: ...


class InvitationEligibilityRepository(Protocol):
    def read_outreach(self, business_id: str) -> OutreachRecord: ...
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...
    def read_confirmed_for_client(self, business_id: str, client_id: str,
                                  run_at: datetime, end_at: datetime) -> tuple[int, tuple[Appointment, ...]]: ...
    def read_last_invitation_sent(self, business_id: str, client_id: str) -> datetime | None: ...


def invitation_problem(intent: InvitationIntent, records: InvitationEligibilityRepository,
                       consent: ConsentRepository, now: datetime) -> str | None:
    """Safe reason code from authoritative state immediately before handoff."""
    if not records.read_outreach(intent.business_id).settings.enabled:
        return "DISABLED"
    if now.tzinfo is None or now.astimezone(UTC) > intent.window_end_at:
        return "WINDOW_EXPIRED"
    profile = records.read_profile(intent.business_id, intent.client_id)
    if profile is None or not profile.active:
        return "CLIENT_UNAVAILABLE"
    if (profile.phone_verified_at != intent.verified_at
            or sha256(profile.phone_e164.encode()).hexdigest() != intent.phone_hash):
        return "PHONE_CHANGED"
    evidence = consent.read_consent(intent.business_id, profile.phone_e164)
    if (evidence is None or evidence.business_id != intent.business_id
            or evidence.client_id != intent.client_id or evidence.method != "in_person"
            or evidence.agreed_at != intent.verified_at):
        return "CONSENT_REQUIRED"
    if consent.is_opted_out(intent.business_id, profile.phone_e164):
        return "OPTED_OUT"
    _, confirmed = records.read_confirmed_for_client(
        intent.business_id, intent.client_id, intent.run_at, intent.window_end_at)
    if confirmed:
        return "CONFIRMED_BOOKING"
    last_sent = records.read_last_invitation_sent(intent.business_id, intent.client_id)
    if last_sent is not None and last_sent > intent.repeat_cutoff_at:
        return "REPEAT_LIMIT"
    return None


class InvitationPromoter:
    """Move eligible dormant intents into the existing outbox, without sending."""

    def __init__(self, records: InvitationRepository, consent: ConsentRepository) -> None:
        self._records = records
        self._consent = consent

    def run(self, business_id: str, now: datetime) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for stored in self._records.queued_invitations(business_id):
            intent = stored.intent
            reason = invitation_problem(intent, self._records, self._consent, now)
            if reason is not None:
                if self._records.complete_invitation(intent, None):
                    counts[f"suppressed_{reason}"] += 1
                continue
            settings_version = self._records.read_outreach(business_id).version
            if self._records.promote_invitation(intent, settings_version, now):
                counts["promoted"] += 1
            else:
                counts["raced"] += 1
        return dict(counts)


def _local_instant(wall: datetime, zone: ZoneInfo) -> datetime:
    """Resolve first fold, or first valid local minute after a spring gap."""
    for offset in range(181):
        candidate_wall = wall + timedelta(minutes=offset)
        candidate = candidate_wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) == candidate_wall:
            return candidate
    raise ValueError("Local time gap exceeds three hours")


def scheduled_window(record: OutreachRecord, zone: ZoneInfo,
                     now: datetime) -> tuple[datetime, datetime] | None:
    settings = record.settings
    if not settings.enabled or settings.weekday is None or settings.local_time is None \
            or settings.lookahead_weeks is None:
        return None
    if now.tzinfo is None:
        raise ValueError("Run instant must be timezone-aware")
    now = now.astimezone(UTC)
    local = now.astimezone(zone)
    if local.weekday() != settings.weekday:
        return None
    wall = datetime.combine(local.date(), settings.local_time)
    run_at = _local_instant(wall, zone)
    # A missing configured time is skipped, not moved. A repeated time runs at
    # its first occurrence only. No later minute backfills a missed run.
    if run_at.astimezone(zone).replace(tzinfo=None) != wall:
        return None
    if not run_at <= now < run_at + timedelta(minutes=1):
        return None
    end_wall = wall + timedelta(days=7 * settings.lookahead_weeks)
    return run_at, _local_instant(end_wall, zone)


class InvitationSelector:
    def __init__(self, records: InvitationRepository, consent: ConsentRepository) -> None:
        self._records = records
        self._consent = consent

    def run(self, business_id: str, now: datetime) -> SelectionReport:
        if not business_id:
            raise ValueError("Business ID is required")
        record = self._records.read_outreach(business_id)
        if not record.settings.enabled:
            return SelectionReport(False, 0, 0, {"disabled": 1})
        policy = self._records.read_policy(business_id)
        zone = ZoneInfo(policy.timezone)
        window = scheduled_window(record, zone, now)
        if window is None:
            return SelectionReport(False, 0, 0, {"not_scheduled": 1})
        run_at, end_at = window
        reasons: Counter[str] = Counter()
        queued = examined = 0
        for profile in self._records.list_profiles(business_id):
            examined += 1
            if not profile.active:
                reasons["inactive"] += 1
                continue
            if profile.phone_verified_at is None:
                reasons["unverified"] += 1
                continue
            evidence = self._consent.read_consent(business_id, profile.phone_e164)
            if (evidence is None or evidence.business_id != business_id
                    or evidence.client_id != profile.client_id
                    or evidence.method != "in_person"
                    or evidence.agreed_at != profile.phone_verified_at):
                reasons["consent"] += 1
                continue
            if self._consent.is_opted_out(business_id, profile.phone_e164):
                reasons["opted_out"] += 1
                continue
            revision, confirmed = self._records.read_confirmed_for_client(
                business_id, profile.client_id, run_at, end_at
            )
            if confirmed:
                reasons["confirmed"] += 1
                continue
            identity = f"{business_id}\0{profile.client_id}\0{run_at.isoformat()}"
            local_run = run_at.astimezone(zone)
            previous_wall = datetime.combine(
                local_run.date() - timedelta(days=7 * (record.settings.lookahead_weeks or 1)),
                local_run.timetz().replace(tzinfo=None),
            )
            intent = InvitationIntent(
                business_id, profile.client_id, sha256(identity.encode()).hexdigest(),
                run_at, end_at, _local_instant(previous_wall, zone),
                sha256(profile.phone_e164.encode()).hexdigest(),
                profile.phone_verified_at, record.settings.lookahead_weeks or 1,
            )
            # Close the read-to-reservation gap for local workflows. DynamoDB also
            # checks these records in the same transaction as the durable guard.
            current_consent = self._consent.read_consent(business_id, profile.phone_e164)
            if current_consent != evidence:
                reasons["consent_changed"] += 1
                continue
            if self._consent.is_opted_out(business_id, profile.phone_e164):
                reasons["opted_out"] += 1
                continue
            if self._records.reserve_invitation(intent, record.version, revision, profile):
                queued += 1
            else:
                reasons["duplicate_or_changed"] += 1
        return SelectionReport(True, examined, queued, dict(reasons))
