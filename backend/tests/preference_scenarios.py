"""Shared synthetic scenarios for client-specific scheduling preferences (#381).

One owner text, the interpretation a scripted model returns for it, the synthetic client
profiles, and what each client must be recommended on which weekday. The owner API, portal
and SMS offer tests (#382/#383) all use them, so both channels are held to the same policy
through ``scheduling.domain.recommendations.recommended_starts``. They prove deterministic resolution and precedence, not the
quality of real natural-language interpretation (see ``backend/evals``).

Business hours in the test calendar are 08:00-17:00 on weekdays, 15-minute starts, so a
window ``[08:00, 12:00)`` recommends start hours 8, 9, 10 and 11.
"""

from dataclasses import dataclass
from datetime import time
from typing import Any

from scheduling.domain.scheduling_preferences import PreferenceRule, PreferenceWindow

TUESDAY, WEDNESDAY, THURSDAY, FRIDAY = "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"
MORNING = {"start": "08:00", "end": "12:00"}
THURSDAY_AFTERNOON = {"start": "13:00", "end": "16:00"}
MORNING_HOURS = frozenset({8, 9, 10, 11})
ALL_DAY_HOURS = frozenset(range(8, 17))


@dataclass(frozen=True)
class Scenario:
    name: str
    text: str
    answer: dict[str, Any]  # what the scripted interpreter returns for ``text``
    # (client id, day, start hours that must be recommended)
    expectations: tuple[tuple[str, str, frozenset[int]], ...]


# client-1 "Client Alder" has a Thursday-afternoon exception to a general weekday-morning rule;
# client-2 "Client Birch" has no client-specific guidance.
CLIENT_NAMES = {"client-1": "Client Alder", "client-2": "Client Birch", "client-3": "Client Cedar"}

OVERRIDE = Scenario(
    "client override keeps compatible general guidance",
    "Prefer weekday mornings. Client Alder prefers Thursday afternoons.",
    {"rules": [
        {"client_name": "", "weekdays": [0, 1, 2, 3, 4], "windows": [MORNING]},
        {"client_name": "Client Alder", "weekdays": [3], "windows": [THURSDAY_AFTERNOON]}],
     "problems": []},
    (("client-1", TUESDAY, MORNING_HOURS),           # compatible general guidance remains
     ("client-1", THURSDAY, frozenset({13, 14, 15})),  # exception overrides general Thursday
     ("client-2", TUESDAY, MORNING_HOURS),
     ("client-2", THURSDAY, MORNING_HOURS)))          # other client unaffected

ANY_TIME_EXCEPTION = Scenario(
    "client any-time day replaces that weekday only",
    "Prefer weekday mornings. Client Cedar prefers Fridays, any time.",
    {"rules": [
        {"client_name": "", "weekdays": [0, 1, 2, 3, 4], "windows": [MORNING]},
        {"client_name": "Client Cedar", "weekdays": [4], "windows": []}],
     "problems": []},
    (("client-3", THURSDAY, MORNING_HOURS),
     ("client-3", FRIDAY, ALL_DAY_HOURS),
     ("client-2", FRIDAY, MORNING_HOURS)))

CLIENT_ONLY = Scenario(
    "client guidance alone leaves everyone else unrecommended",
    "Client Birch prefers Wednesday mornings.",
    {"rules": [{"client_name": "Client Birch", "weekdays": [2], "windows": [MORNING]}],
     "problems": []},
    (("client-2", WEDNESDAY, MORNING_HOURS),
     ("client-2", TUESDAY, frozenset()),
     ("client-1", WEDNESDAY, frozenset())))

SCENARIOS = (OVERRIDE, ANY_TIME_EXCEPTION, CLIENT_ONLY)


def _window(start: str, end: str) -> PreferenceWindow:
    return PreferenceWindow(time.fromisoformat(start), time.fromisoformat(end))


# Business-wide rule sets for SMS offer tests (#382). Thursday afternoon (13:00-15:00 has
# eight valid starts for a 120-minute visit), Thursday late (three), and a weekday with no
# day in the requested range.
THURSDAY_AFTERNOON_RULES = (PreferenceRule((3,), (_window("13:00", "15:00"),)),)
THURSDAY_LATE_RULES = (PreferenceRule((3,), (_window("14:30", "15:30"),)),)
SATURDAY_ONLY_RULES = (PreferenceRule((5,)),)
