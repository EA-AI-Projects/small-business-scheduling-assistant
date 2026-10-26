"""Shared visit reminder settings and SMS eligibility, independent of delivery."""
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import AvailabilityPolicy
from scheduling.domain.booking_invitations import _local_instant
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.owner_policy import PolicyRecord
from scheduling.domain.sms_ingress import SmsIngressStore


@dataclass(frozen=True)
class ReminderSettings:
    days_before: int = 1
    local_time: time = time(10)

    def __post_init__(self) -> None:
        if not 1 <= self.days_before <= 7:
            raise ValueError("Reminder days before must be 1 through 7")
        if (self.local_time.tzinfo is not None or self.local_time.second
                or self.local_time.microsecond or not time(8) <= self.local_time <= time(18)):
            raise ValueError("Choose a local hour and minute from 08:00 through 18:00")


@dataclass(frozen=True)
class ReminderRecord:
    settings: ReminderSettings
    version: int
    updated_at: datetime | None = None


class ReminderRepository(Protocol):
    def read_reminders(self, business_id: str) -> ReminderRecord: ...
    def save_reminders(self, business_id: str, actor_id: str, key: str,
                       expected_version: int, settings: ReminderSettings, now: datetime) -> ReminderRecord: ...


def reminder_blockers(profile: ClientProfile, consent: SmsIngressStore | None) -> tuple[str, ...]:
    """Current restrictions, separate from the saved reminder preference."""
    blockers = []
    if not profile.active:
        blockers.append("inactive_profile")
    if profile.phone_verified_at is None:
        blockers.append("phone_unverified")
    if consent is None:
        blockers.append("consent_unavailable")
    else:
        try:
            evidence = consent.read_consent(profile.business_id, profile.phone_e164)
            opted_out = consent.is_opted_out(profile.business_id, profile.phone_e164)
        except Exception:  # Provider/store diagnostics never reach authorized UI explanations.
            blockers.append("consent_unavailable")
        else:
            if (evidence is None or evidence.client_id != profile.client_id
                    or evidence.business_id != profile.business_id or evidence.method != "in_person"
                    or evidence.agreed_at != profile.phone_verified_at):
                blockers.append("consent_missing")
            if opted_out:
                blockers.append("sms_opted_out")
    return tuple(blockers)


def reminder_preference_view(profile: ClientProfile, consent: SmsIngressStore | None) -> dict[str, object]:
    return {"enabled": profile.reminders_enabled, "version": profile.version,
            "blockers": reminder_blockers(profile, consent)}


REMINDER_TEMPLATE = "visit_reminder"


def reminder_outbox_id(appointment_id: str) -> str:
    """Stable per-visit identity survives settings edits and queue redelivery."""
    return f"visit-reminder#{appointment_id}"


def reminder_run_at(appointment: Appointment, settings: ReminderSettings, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    day = appointment.start_at.astimezone(zone).date() - timedelta(days=settings.days_before)
    return _local_instant(datetime.combine(day, settings.local_time), zone)


class ReminderEligibilityRepository(Protocol):
    def read_reminders(self, business_id: str) -> ReminderRecord: ...
    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...
    def read_policy_record(self, business_id: str) -> PolicyRecord | None: ...


class ReminderDeliveryRepository(ReminderEligibilityRepository, Protocol):
    def list_profiles(self, business_id: str) -> tuple[ClientProfile, ...]: ...
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...
    def read_appointment(self, appointment_id: str) -> Appointment | None: ...
    def read_confirmed_for_client(self, business_id: str, client_id: str,
                                 run_at: datetime, end_at: datetime) -> tuple[int, tuple[Appointment, ...]]: ...
    def reserve_reminder(self, appointment: Appointment, profile: ClientProfile,
                         settings_version: int, calendar_revision: int,
                         run_at: datetime, now: datetime) -> bool: ...


def reminder_problem(appointment: Appointment, profile: ClientProfile,
                     records: ReminderEligibilityRepository, consent: SmsIngressStore,
                     now: datetime) -> str | None:
    if appointment.status != CalendarStatus.CONFIRMED:
        return "NOT_CONFIRMED"
    if not profile.reminders_enabled:
        return "NOT_ENROLLED"
    blockers = reminder_blockers(profile, consent)
    if blockers:
        return blockers[0].upper()
    settings = records.read_reminders(appointment.business_id)
    run_at = reminder_run_at(appointment, settings.settings,
                              records.read_policy(appointment.business_id).timezone)
    if not run_at <= now < run_at + timedelta(minutes=1):
        return "WINDOW_EXPIRED"
    policy = records.read_policy_record(appointment.business_id)
    timezone_changed = getattr(policy, "timezone_updated_at", None)
    if timezone_changed is not None and timezone_changed > run_at:
        return "LATE_TIMEZONE_CHANGE"
    if settings.updated_at is not None and settings.updated_at > run_at:
        return "LATE_SETTINGS_CHANGE"
    if ((profile.reminders_updated_at is not None and profile.reminders_updated_at > run_at)
            or (appointment.confirmed_at is not None and appointment.confirmed_at > run_at)):
        return "LATE_ENROLLMENT_OR_CONFIRMATION"
    return None


@dataclass(frozen=True)
class ReminderSelectionReport:
    examined: int
    queued: int


class ReminderSelector:
    def __init__(self, records: ReminderDeliveryRepository, consent: SmsIngressStore) -> None:
        self._records = records
        self._consent = consent

    def run(self, business_id: str, now: datetime) -> ReminderSelectionReport:
        if now.tzinfo is None:
            raise ValueError("Reminder clock must be timezone-aware")
        policy = self._records.read_policy(business_id)
        zone = ZoneInfo(policy.timezone)
        settings = self._records.read_reminders(business_id)
        local = now.astimezone(zone)
        if (local.hour, local.minute) != (settings.settings.local_time.hour, settings.settings.local_time.minute):
            return ReminderSelectionReport(0, 0)
        day = local.date() + timedelta(days=settings.settings.days_before)
        start = datetime.combine(day, time(), zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
        examined = queued = 0
        for profile in self._records.list_profiles(business_id):
            if not profile.reminders_enabled:
                continue
            revision, visits = self._records.read_confirmed_for_client(business_id, profile.client_id, start, end)
            for visit in visits:
                examined += 1
                if visit.start_at.astimezone(zone).date() != day:
                    continue
                if reminder_problem(visit, profile, self._records, self._consent, now) is not None:
                    continue
                run_at = reminder_run_at(visit, settings.settings, policy.timezone)
                if self._records.reserve_reminder(visit, profile, settings.version, revision, run_at, now):
                    queued += 1
        return ReminderSelectionReport(examined, queued)
