"""Owner configuration for future scheduled booking invitations.

Saving this configuration does not authorize or send SMS. A future selection
worker reads the same record through ``OutreachRepository``.
"""

from dataclasses import dataclass
from datetime import time
from typing import Protocol

from scheduling.domain.business_profile import BusinessProfile


@dataclass(frozen=True)
class OutreachSettings:
    enabled: bool = False
    weekday: int | None = None  # Monday = 0, in the business timezone
    local_time: time | None = None
    lookahead_weeks: int | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.weekday is not None and not 0 <= self.weekday <= 6:
            raise ValueError("Run day must be Monday through Sunday")
        if self.local_time is not None and (
            self.local_time.tzinfo is not None or self.local_time.second or self.local_time.microsecond
        ):
            raise ValueError("Run time must be a local hour and minute")
        if self.lookahead_weeks is not None and self.lookahead_weeks not in (1, 2):
            raise ValueError("Lookahead must be one or two weeks")
        if self.enabled and (self.weekday is None or self.local_time is None
                             or self.lookahead_weeks is None):
            raise ValueError("Choose a run day, time, and lookahead before enabling")
        if self.message is not None and (not self.message.strip() or len(self.message) > 500):
            raise ValueError("Invitation message must be 1 to 500 characters")


# The registered program name stays the prefix so the text is generic across businesses and
# matches the A2P-approved sample; the saved business and service names appear in the body.
PROGRAM_NAME = "Smart Scheduling Assistant"


def approved_invitation_message(lookahead_weeks: int, business: BusinessProfile | None) -> str:
    """The default invitation, naming the saved service and business (#348).

    A custom message is the owner's own text. The default needs the saved profile, so with
    none it cannot be built and booking stays blocked.
    """
    if business is None:
        raise ValueError("Save the business profile before inviting clients to book")
    window = "week" if lookahead_weeks == 1 else "two weeks"
    return (f"{PROGRAM_NAME}: Would you like to book a visit for {business.service_name} with "
            f"{business.display_name} in the next {window}? Reply with a day and time that works for you, "
            "or STOP to opt out.")


# The owner-approved default text before #348. Owner Settings used to store it as a
# custom message, so a stored copy is treated as "no custom message" and the current
# default names the saved business and service instead.
LEGACY_DEFAULT_MESSAGES = frozenset(
    "Smart Scheduling Assistant: Would you like to book a cleaning visit in the next "
    f"{window}? Reply with a day and time that works for you, or STOP to opt out."
    for window in ("one week", "two weeks"))


def invitation_message(settings: OutreachSettings, business: BusinessProfile | None) -> str:
    if settings.message and settings.message not in LEGACY_DEFAULT_MESSAGES:
        return settings.message
    return approved_invitation_message(settings.lookahead_weeks or 1, business)


@dataclass(frozen=True)
class OutreachRecord:
    settings: OutreachSettings
    version: int


class OutreachRepository(Protocol):
    def read_outreach(self, business_id: str) -> OutreachRecord:
        """Return the disabled version-zero default for a new business."""
        ...

    def save_outreach(self, business_id: str, actor_id: str, key: str,
                      expected_version: int, settings: OutreachSettings) -> OutreachRecord:
        """Atomically compare version, save settings, and replay a duplicate key."""
        ...


def update_outreach(repository: OutreachRepository, business_id: str, actor_id: str,
                    key: str, expected_version: int,
                    settings: OutreachSettings) -> OutreachRecord:
    if expected_version < 0:
        raise ValueError("Expected version must be nonnegative")
    if not business_id or not actor_id or not key:
        raise ValueError("Owner and request identity are required")
    return repository.save_outreach(business_id, actor_id, key, expected_version, settings)
