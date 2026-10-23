"""Shared visit reminder settings and SMS eligibility, independent of delivery."""
from dataclasses import dataclass
from datetime import time
from typing import Protocol

from scheduling.domain.client_records import ClientProfile
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


class ReminderRepository(Protocol):
    def read_reminders(self, business_id: str) -> ReminderRecord: ...
    def save_reminders(self, business_id: str, actor_id: str, key: str,
                       expected_version: int, settings: ReminderSettings) -> ReminderRecord: ...


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
        evidence = consent.read_consent(profile.business_id, profile.phone_e164)
        if evidence is None or evidence.client_id != profile.client_id:
            blockers.append("consent_missing")
        if consent.is_opted_out(profile.business_id, profile.phone_e164):
            blockers.append("sms_opted_out")
    return tuple(blockers)


def reminder_preference_view(profile: ClientProfile, consent: SmsIngressStore | None) -> dict[str, object]:
    return {"enabled": profile.reminders_enabled, "version": profile.version,
            "blockers": reminder_blockers(profile, consent)}
