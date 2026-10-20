"""Owner scheduling preferences: reviewed, validated, business-wide start guidance (#379, #380).

The owner writes natural language; a model proposes an interpretation; this module
validates it deterministically. The only supported vocabulary is preferred weekdays and
visit start-time windows in the business timezone. Preferences are soft guidance: they
never create availability, so they live apart from the hours, buffers and reservations
that decide which starts exist (see ``recommendations``).

A review is bound to the exact text, interpretation and timezone through a token, so
activation can store only what the owner saw. Anything unsupported, unclear,
contradictory, client-specific (this slice is business-wide only) or aimed at booking
constraints blocks activation; a subset is never activated silently.
"""

import json
from dataclasses import dataclass
from datetime import time
from hashlib import sha256
from itertools import pairwise
from typing import Protocol

MAX_TEXT_CHARACTERS = 1000
MAX_RULES = 8
MAX_WINDOWS_PER_RULE = 4
PROBLEM_KINDS = ("unclear", "contradictory", "unsupported", "client_specific",
                 "constraint_override")


class PreferenceInterpretationFailed(RuntimeError):
    """The model was unreachable or returned an unusable answer; nothing was reviewed."""


@dataclass(frozen=True)
class PreferenceWindow:
    """A start-time window in business-local wall time; a visit may start in [start, end)."""

    start: time
    end: time

    def __post_init__(self) -> None:
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("Preference times must be local wall times")
        if self.end <= self.start:
            raise ValueError("A start-time window must end after it starts")

    def contains(self, wall: time) -> bool:
        return self.start <= wall < self.end


@dataclass(frozen=True)
class PreferenceRule:
    """Preferred weekdays (0 Monday to 6 Sunday), optionally limited to start windows.

    No windows means any start time on those weekdays.
    """

    weekdays: tuple[int, ...]
    windows: tuple[PreferenceWindow, ...] = ()

    def __post_init__(self) -> None:
        if (not self.weekdays or any(day not in range(7) for day in self.weekdays)
                or tuple(sorted(set(self.weekdays))) != self.weekdays):
            raise ValueError("Weekdays must be a sorted, nonempty set of 0 (Monday) to 6 (Sunday)")
        if len(self.windows) > MAX_WINDOWS_PER_RULE:
            raise ValueError(f"A rule can have at most {MAX_WINDOWS_PER_RULE} time windows")
        ordered = sorted(self.windows, key=lambda window: window.start)
        if tuple(ordered) != self.windows or any(
                later.start < earlier.end for earlier, later in pairwise(ordered)):
            raise ValueError("Time windows must be sorted and must not overlap")
        if len(self.weekdays) == 7 and not self.windows:
            raise ValueError("Every day and any time is not a preference")

    def matches(self, weekday: int, wall: time) -> bool:
        return weekday in self.weekdays and (
            not self.windows or any(window.contains(wall) for window in self.windows))


@dataclass(frozen=True)
class PreferenceProblem:
    kind: str
    message: str


@dataclass(frozen=True)
class SchedulingPreferences:
    """What an activation stores: the owner's text and the interpretation they reviewed."""

    text: str
    rules: tuple[PreferenceRule, ...]
    timezone: str
    review_token: str

    def __post_init__(self) -> None:
        if self.review_token != review_token(self.text, self.rules, self.timezone):
            raise ValueError("Preferences do not match their review")


@dataclass(frozen=True)
class SchedulingPreferencesRecord:
    preferences: SchedulingPreferences | None
    version: int


@dataclass(frozen=True)
class PreferenceReview:
    """``ready`` carries a token and no problems; otherwise problems explain the correction."""

    rules: tuple[PreferenceRule, ...]
    problems: tuple[PreferenceProblem, ...]
    timezone: str
    token: str | None

    @property
    def ready(self) -> bool:
        return self.token is not None


class ReviewMismatch(ValueError):
    """The submitted text or interpretation is not the one the review covered."""


class PreferenceInterpreter(Protocol):
    def interpret_scheduling_preferences(self, text: str, timezone: str) -> dict[str, object]:
        """Return ``{"rules": [...], "problems": [...]}``; may raise on failure."""
        ...


class SchedulingPreferencesRepository(Protocol):
    def read_scheduling_preferences(self, business_id: str) -> SchedulingPreferencesRecord:
        """Return the empty version-zero record for a business that never saved one."""
        ...

    def save_scheduling_preferences(self, business_id: str, actor_id: str, key: str,
                                    expected_version: int,
                                    preferences: SchedulingPreferences | None,
                                    ) -> SchedulingPreferencesRecord:
        """Atomically compare version, replace (or clear with None), audit, replay a key."""
        ...


def clean_text(text: str) -> str:
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_TEXT_CHARACTERS:
        raise ValueError(f"Preferences must be 1 to {MAX_TEXT_CHARACTERS} characters")
    return stripped


def rules_payload(rules: tuple[PreferenceRule, ...]) -> list[dict[str, object]]:
    return [{"weekdays": list(rule.weekdays),
             "windows": [{"start": window.start.strftime("%H:%M"),
                          "end": window.end.strftime("%H:%M")} for window in rule.windows]}
            for rule in rules]


def rules_from_payload(payload: object) -> tuple[PreferenceRule, ...]:
    """Strictly parse and validate rules; ValueError for anything off-schema."""
    if not isinstance(payload, list) or len(payload) > MAX_RULES:
        raise ValueError(f"Use at most {MAX_RULES} preference rules")
    rules = []
    for item in payload:
        if (not isinstance(item, dict) or set(item) != {"weekdays", "windows"}
                or not isinstance(item["weekdays"], list)
                or not isinstance(item["windows"], list)):
            raise ValueError("A preference rule needs weekdays and windows")
        windows = []
        for window in item["windows"]:
            if not isinstance(window, dict) or set(window) != {"start", "end"}:
                raise ValueError("A time window needs a start and an end")
            windows.append(PreferenceWindow(_wall(window["start"]), _wall(window["end"])))
        days = item["weekdays"]
        if any(type(day) is not int for day in days):
            raise ValueError("Weekdays must be whole numbers")
        rules.append(PreferenceRule(tuple(days), tuple(windows)))
    return tuple(rules)


def _wall(value: object) -> time:
    if (not isinstance(value, str) or len(value) != 5 or value[2] != ":"
            or not (value[:2] + value[3:]).isdigit()):
        raise ValueError("Times must be HH:MM")
    return time.fromisoformat(value)


def review_token(text: str, rules: tuple[PreferenceRule, ...], timezone: str) -> str:
    """Binds an activation to the exact text, interpretation and timezone that were shown."""
    canonical = json.dumps({"text": text, "rules": rules_payload(rules), "timezone": timezone},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(canonical.encode()).hexdigest()


def review_preferences(interpreter: PreferenceInterpreter, text: str,
                       timezone: str) -> PreferenceReview:
    """Interpret and validate. Writes nothing; activation is a separate, explicit step."""
    owner_text = clean_text(text)
    raw = interpreter.interpret_scheduling_preferences(owner_text, timezone)
    if (not isinstance(raw, dict) or set(raw) != {"rules", "problems"}
            or not isinstance(raw["problems"], list)):
        raise PreferenceInterpretationFailed("The interpretation was not usable")
    problems: list[PreferenceProblem] = []
    for item in raw["problems"]:
        if (not isinstance(item, dict) or set(item) != {"kind", "message"}
                or item["kind"] not in PROBLEM_KINDS or not isinstance(item["message"], str)):
            raise PreferenceInterpretationFailed("The interpretation was not usable")
        problems.append(PreferenceProblem(item["kind"], item["message"].strip()[:300]
                                          or "This instruction needs correction."))
    rules: tuple[PreferenceRule, ...] = ()
    try:
        rules = rules_from_payload(raw["rules"])
    except ValueError as exc:
        problems.append(PreferenceProblem("unclear", f"The interpretation was not usable: {exc}."))
    if not problems and not rules:
        problems.append(PreferenceProblem(
            "unclear", "No weekday or start-time preference was found. Name the days or times "
                       "you prefer."))
    if problems:
        return PreferenceReview((), tuple(problems), timezone, None)
    return PreferenceReview(rules, (), timezone, review_token(owner_text, rules, timezone))


def reviewed_preferences(text: str, rules_json: object, timezone: str,
                         token: str) -> SchedulingPreferences:
    """The configuration an activation may store, only if it is exactly what was reviewed."""
    owner_text = clean_text(text)
    rules = rules_from_payload(rules_json)
    if not rules or token != review_token(owner_text, rules, timezone):
        raise ReviewMismatch("The text or interpretation changed since it was reviewed")
    return SchedulingPreferences(owner_text, rules, timezone, token)


def save_preferences(repository: SchedulingPreferencesRepository, business_id: str,
                     actor_id: str, key: str, expected_version: int,
                     preferences: SchedulingPreferences | None) -> SchedulingPreferencesRecord:
    if expected_version < 0:
        raise ValueError("Expected version must be nonnegative")
    if not business_id or not actor_id or not key:
        raise ValueError("Owner and request identity are required")
    return repository.save_scheduling_preferences(business_id, actor_id, key, expected_version,
                                                  preferences)
