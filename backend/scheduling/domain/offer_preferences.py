"""Business-wide scheduling preferences applied to SMS offer selection (#382).

The SMS paths hand this module the valid starts inside the window the client asked for.
It only chooses which of them to offer first: preferred starts take the slots, other valid
starts fill what is left. It never adds, removes or moves a start, so hours, buffers,
duration, reservations, acceptance and the transactional recheck are unchanged, and a
preference change after an offer is sent cannot affect that stored offer.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from scheduling.domain.conversation_state import MAX_OPTIONS, spread
from scheduling.domain.recommendations import recommended_starts

LOGGER = logging.getLogger(__name__)

Preferred = Callable[[tuple[datetime, ...]], frozenset[datetime]]


def no_preferences(starts: tuple[datetime, ...]) -> frozenset[datetime]:
    del starts
    return frozenset()


def preferred_reader(repository: object, business_id: str, timezone: str) -> Preferred:
    """Read the active preferences on first use; any failure means ordinary offers.

    Logs the failure type only, never owner text or client data.
    """
    def preferred(starts: tuple[datetime, ...]) -> frozenset[datetime]:
        try:
            reader = repository.read_scheduling_preferences  # type: ignore[attr-defined]
            return recommended_starts(reader(business_id).preferences, timezone, starts)
        except Exception as exc:  # noqa: BLE001 - guidance must never break SMS
            LOGGER.warning("Scheduling preferences unavailable for SMS offer (%s)",
                           type(exc).__name__)
            return frozenset()
    return preferred


def pick_offer(starts: tuple[datetime, ...], zone: ZoneInfo, preferred: Preferred,
               limit: int = MAX_OPTIONS) -> tuple[datetime, ...]:
    """Up to ``limit`` starts: preferred first, the rest fill remaining slots, in time order."""
    flagged = preferred(starts)
    if not flagged or len(starts) <= limit:
        return spread(starts, zone, limit)
    first = spread(tuple(start for start in starts if start in flagged), zone, limit)
    rest = tuple(start for start in starts if start not in flagged)
    room = limit - len(first)
    fill = spread(rest, zone, room) if room > 0 and rest else ()
    return tuple(sorted(set(first) | set(fill)))
