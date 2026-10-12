"""Owner-managed business profile: the trusted source of business facts (#336).

One versioned record per business holds the display name, short description,
support contact, the single bookable service, and its default booking duration.
Owner UI, client UI, backend, and future SMS tools read this record; a model
never authors these facts. A new business reads an unconfigured version-zero
default, and booking flows must not rely on a profile that is not configured.
"""

from dataclasses import dataclass
from typing import Protocol

MAX_DURATION_MINUTES = 24 * 60


def _text(value: str, label: str, limit: int) -> str:
    stripped = value.strip()
    if not stripped or len(stripped) > limit:
        raise ValueError(f"{label} must be 1 to {limit} characters")
    return stripped


@dataclass(frozen=True)
class BusinessProfile:
    display_name: str
    description: str
    support_contact: str
    service_name: str
    service_description: str
    default_duration_minutes: int

    def __post_init__(self) -> None:
        for field, label, limit in (("display_name", "Business name", 100),
                                    ("description", "Business description", 300),
                                    ("support_contact", "Support contact", 200),
                                    ("service_name", "Service name", 100),
                                    ("service_description", "Service description", 300)):
            object.__setattr__(self, field, _text(getattr(self, field), label, limit))
        if not 0 < self.default_duration_minutes <= MAX_DURATION_MINUTES:
            raise ValueError("Default booking duration must be 1 minute to 24 hours")


@dataclass(frozen=True)
class BusinessProfileRecord:
    profile: BusinessProfile | None
    version: int

    @property
    def configured(self) -> bool:
        return self.profile is not None


class ProfileExceedsPolicy(ValueError):
    """The default duration is longer than the scheduling policy allows."""


@dataclass(frozen=True)
class PolicyGuard:
    """The policy a profile edit was validated against.

    The repository checks it after the idempotent replay lookup and inside the
    save transaction, so a retry of a stored save never re-validates and a
    concurrent policy edit cannot slip a too-long default past the check.
    """

    version: int | None
    maximum_visit_minutes: int | None

    def check(self, profile: BusinessProfile) -> None:
        if (self.maximum_visit_minutes is not None
                and profile.default_duration_minutes > self.maximum_visit_minutes):
            raise ProfileExceedsPolicy(
                "Default duration exceeds the scheduling policy's visit maximum")


class BusinessProfileRepository(Protocol):
    def read_business_profile(self, business_id: str) -> BusinessProfileRecord:
        """Return the unconfigured version-zero default for a new business."""
        ...

    def save_business_profile(self, business_id: str, actor_id: str, key: str,
                              expected_version: int, profile: BusinessProfile,
                              policy_guard: PolicyGuard | None = None) -> BusinessProfileRecord:
        """Atomically compare version, save, audit, and replay a duplicate key.

        A duplicate key replays before ``policy_guard`` is checked.
        """
        ...


def update_business_profile(repository: BusinessProfileRepository, business_id: str,
                            actor_id: str, key: str, expected_version: int,
                            profile: BusinessProfile,
                            policy_guard: PolicyGuard | None = None) -> BusinessProfileRecord:
    if expected_version < 0:
        raise ValueError("Expected version must be nonnegative")
    if not business_id or not actor_id or not key:
        raise ValueError("Owner and request identity are required")
    return repository.save_business_profile(business_id, actor_id, key, expected_version,
                                            profile, policy_guard)
