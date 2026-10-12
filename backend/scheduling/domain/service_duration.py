"""The one saved service duration every booking path reads (#339).

SMS, the client portal, and owner-created requests take the visit length from
the owner's saved business profile and check it against the current scheduling
policy at read time. A client record never carries a duration. Existing
appointments and holds keep the duration they were created with.
"""

from typing import Protocol

from scheduling.domain.availability import AvailabilityPolicy, InvalidDuration
from scheduling.domain.business_profile import BusinessProfileRecord


class ServiceNotConfigured(Exception):
    """The owner has not saved the business profile, so no default duration exists."""


class ServiceDurationChanged(Exception):
    """The saved default duration changed after the caller read it."""


class ServiceDurationSource(Protocol):
    def read_business_profile(self, business_id: str) -> BusinessProfileRecord: ...


def service_duration(source: ServiceDurationSource, business_id: str,
                     policy: AvailabilityPolicy) -> int:
    """The saved default duration, re-checked against the current policy maximum."""
    record = source.read_business_profile(business_id)
    if record.profile is None:
        raise ServiceNotConfigured("Save the business profile before booking")
    minutes = record.profile.default_duration_minutes
    if minutes > policy.maximum_visit_minutes:
        raise InvalidDuration("The saved default duration exceeds the policy's visit maximum")
    return minutes
