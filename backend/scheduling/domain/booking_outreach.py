"""Owner configuration for future scheduled booking invitations.

Saving this configuration does not authorize or send SMS. A future selection
worker reads the same record through ``OutreachRepository``.
"""

from dataclasses import dataclass
from datetime import time
from typing import Protocol


@dataclass(frozen=True)
class OutreachSettings:
    enabled: bool = False
    weekday: int | None = None  # Monday = 0, in the business timezone
    local_time: time | None = None
    lookahead_weeks: int | None = None

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
