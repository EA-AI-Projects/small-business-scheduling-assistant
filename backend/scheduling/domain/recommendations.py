"""Shared recommendation ranking for offered starts (#379).

Takes starts the availability rules already allow, inside the window the client asked
for, and marks the ones the owner prefers. It cannot add, remove or move a start, so
hours, date exceptions, holidays, horizon, duration, buffers and reservations stay with
``availability`` and the transactional recheck. The portal flags with it today; SMS
offer selection reuses it later.
"""

from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from scheduling.domain.scheduling_preferences import SchedulingPreferences, applicable_rules


def recommended_starts(preferences: SchedulingPreferences | None, timezone: str,
                       candidates: Iterable[datetime],
                       client_id: str | None = None) -> frozenset[datetime]:
    """The candidates that match a rule applicable to this client, in the business timezone.

    Only the client's own rules and the business-wide rules they do not override count (see
    ``applicable_rules``); other clients' rules never influence the result.

    No active preferences, or preferences reviewed for another timezone, recommend
    nothing, so ordinary availability is unchanged.
    """
    rules = applicable_rules(preferences, timezone, client_id)
    if not rules:
        return frozenset()
    zone = ZoneInfo(timezone)
    flagged = set()
    for start in candidates:
        local = start.astimezone(zone)
        if any(rule.matches(local.weekday(), local.time()) for rule in rules):
            flagged.add(start)
    return frozenset(flagged)
