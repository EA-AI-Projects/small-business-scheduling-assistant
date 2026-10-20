"""Owner scheduling preferences: reviewed, validated start guidance (#379, #380, #381).

The owner writes natural language; a model proposes an interpretation; this module
validates it deterministically. The only supported vocabulary is preferred weekdays and
visit start-time windows in the business timezone. Preferences are soft guidance: they
never create availability, so they live apart from the hours, buffers and reservations
that decide which starts exist (see ``recommendations``).

A review is bound to the exact text, interpretation and timezone through a token, so
activation can store only what the owner saw. Anything unsupported, unclear,
contradictory or aimed at booking constraints blocks activation; a subset is never
activated silently.

A rule is business-wide or bound to one client profile by its stable ``client_id``. The
model only reports the name the owner wrote; ``resolve_client_names`` matches it to the
business's own active profiles in code. An ambiguous name needs the owner's explicit
selection, an unknown name blocks, and the stored rule keeps the profile id so a rename
cannot redirect it.

Precedence (``applicable_rules``): a client's rules are their own client rules plus the
business-wide rules for every weekday their client rules do not name. All preferences are
positive, so rules at one scope simply add up; a same-scope contradiction is whatever the
interpreter reports as ``contradictory``, and it blocks activation.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from hashlib import sha256
from itertools import pairwise
from typing import Protocol

MAX_TEXT_CHARACTERS = 1000
MAX_RULES = 8
MAX_WINDOWS_PER_RULE = 4
REVIEW_TTL = timedelta(hours=1)
MAX_CLIENT_NAME_CHARACTERS = 100
# Kinds the model may report. ``ambiguous_client`` and ``unknown_client`` come from code.
PROBLEM_KINDS = ("unclear", "contradictory", "unsupported", "constraint_override")


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

    No windows means any start time on those weekdays. ``client_id`` None means the whole
    business; otherwise the rule applies only to that client profile.
    """

    weekdays: tuple[int, ...]
    windows: tuple[PreferenceWindow, ...] = ()
    client_id: str | None = None

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
        if self.client_id is not None and not self.client_id:
            raise ValueError("A client rule needs a client id")

    def matches(self, weekday: int, wall: time) -> bool:
        return weekday in self.weekdays and (
            not self.windows or any(window.contains(wall) for window in self.windows))


@dataclass(frozen=True)
class ClientReference:
    """A client profile as the owner sees it: stable id, current display name, and enough
    owner-only detail to tell duplicate names apart."""

    client_id: str
    name: str
    phone_last4: str = ""
    service_address: str = ""


@dataclass(frozen=True)
class AmbiguousClient:
    """A name that fits several profiles; the owner picks one and reviews again."""

    name: str
    candidates: tuple[ClientReference, ...]


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
class StoredReview:
    """A server-produced, ready review, kept briefly so activation can apply exactly it."""

    text: str
    rules: tuple[PreferenceRule, ...]
    timezone: str
    token: str

    def __post_init__(self) -> None:
        if self.token != review_token(self.text, self.rules, self.timezone):
            raise ValueError("Stored review does not match its token")


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
    clients: tuple[ClientReference, ...] = ()
    ambiguities: tuple[AmbiguousClient, ...] = ()

    @property
    def ready(self) -> bool:
        return self.token is not None


class ReviewMismatch(ValueError):
    """The submitted text or interpretation is not the one the review covered."""


class InvalidClientSelection(ValueError):
    """A selected profile is unknown, inactive, or not a candidate for that name."""


class PreferenceInterpreter(Protocol):
    def interpret_scheduling_preferences(self, text: str, timezone: str) -> dict[str, object]:
        """Return ``{"rules": [...], "problems": [...]}``; may raise on failure."""
        ...


class SchedulingPreferencesRepository(Protocol):
    def read_scheduling_preferences(self, business_id: str) -> SchedulingPreferencesRecord:
        """Return the empty version-zero record for a business that never saved one."""
        ...

    def save_preference_review(self, business_id: str, review: StoredReview,
                               expires_at: datetime) -> None:
        """Store a ready review under its token, scoped to the business, until it expires."""
        ...

    def read_preference_review(self, business_id: str, token: str,
                               now: datetime) -> StoredReview | None:
        """The unexpired review this business produced for the token, else None."""
        ...

    def save_scheduling_preferences(self, business_id: str, actor_id: str, key: str,
                                    expected_version: int,
                                    preferences: SchedulingPreferences | None,
                                    ) -> SchedulingPreferencesRecord:
        """Atomically compare version, replace (or clear with None), audit, replay a key."""
        ...

    def scrub_client_preferences(self, business_id: str, client_id: str) -> None:
        """Client erasure: drop the client's rules from the active record (neutral text, new
        version, audit without identifiers; cleared when nothing remains) and delete every
        stored review that binds the client. Idempotent."""
        ...


_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _clock(value: time) -> str:
    return f"{value.hour % 12 or 12}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def neutral_text(rules: tuple[PreferenceRule, ...]) -> str:
    """Owner-facing text rebuilt from rules alone, so it can name no client."""
    parts = []
    for rule in rules:
        days = "Every day" if len(rule.weekdays) == 7 else ", ".join(
            _DAY_NAMES[day] for day in rule.weekdays)
        times = ("starts " + " or ".join(f"{_clock(w.start)}–{_clock(w.end)}" for w in rule.windows)
                 if rule.windows else "any start time")
        parts.append(f"{'One client: ' if rule.client_id else ''}{days}: {times}")
    return "; ".join(parts)[:MAX_TEXT_CHARACTERS]


def without_client(preferences: SchedulingPreferences, client_id: str,
                   ) -> SchedulingPreferences | None:
    """The configuration after erasing a client: their rules removed, the text regenerated from
    the remaining rules (the original may name the client), or None if nothing remains."""
    rules = tuple(rule for rule in preferences.rules if rule.client_id != client_id)
    if len(rules) == len(preferences.rules):
        return preferences
    if not rules:
        return None
    text = neutral_text(rules)
    return SchedulingPreferences(text, rules, preferences.timezone,
                                 review_token(text, rules, preferences.timezone))


def clean_text(text: str) -> str:
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_TEXT_CHARACTERS:
        raise ValueError(f"Preferences must be 1 to {MAX_TEXT_CHARACTERS} characters")
    return stripped


def rules_payload(rules: tuple[PreferenceRule, ...]) -> list[dict[str, object]]:
    # ``client_id`` appears only on client rules, so business-wide tokens stay as #380 made them.
    return [{"weekdays": list(rule.weekdays),
             "windows": [{"start": window.start.strftime("%H:%M"),
                          "end": window.end.strftime("%H:%M")} for window in rule.windows],
             **({} if rule.client_id is None else {"client_id": rule.client_id})}
            for rule in rules]


def rules_from_payload(payload: object, extra: frozenset[str] = frozenset({"client_id"}),
                       ) -> tuple[PreferenceRule, ...]:
    """Strictly parse and validate rules; ValueError for anything off-schema.

    ``extra`` names the optional keys beyond weekdays and windows: stored rules may carry a
    ``client_id``; the model's raw rules may carry a ``client_name`` instead.
    """
    if not isinstance(payload, list) or len(payload) > MAX_RULES:
        raise ValueError(f"Use at most {MAX_RULES} preference rules")
    rules = []
    for item in payload:
        if (not isinstance(item, dict) or not {"weekdays", "windows"} <= set(item)
                or set(item) - {"weekdays", "windows"} - extra
                or not isinstance(item["weekdays"], list)
                or not isinstance(item["windows"], list)):
            raise ValueError("A preference rule needs weekdays and windows")
        client_id = item.get("client_id")
        if client_id is not None and not isinstance(client_id, str):
            raise ValueError("A client id must be text")
        windows = []
        for window in item["windows"]:
            if not isinstance(window, dict) or set(window) != {"start", "end"}:
                raise ValueError("A time window needs a start and an end")
            windows.append(PreferenceWindow(_wall(window["start"]), _wall(window["end"])))
        days = item["weekdays"]
        if any(type(day) is not int for day in days):
            raise ValueError("Weekdays must be whole numbers")
        rules.append(PreferenceRule(tuple(days), tuple(windows), client_id))
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


def normalize_name(name: str) -> str:
    """Case, spacing and punctuation insensitive form used only to match names to profiles."""
    return " ".join(re.findall(r"\w+", name.casefold()))


def candidate_profiles(name: str, profiles: Sequence[ClientReference]) -> tuple[ClientReference, ...]:
    """Active profiles whose name contains every word the owner wrote (so a first name or a
    surname finds candidates). The caller binds automatically only when this is a single
    profile whose whole name matches (``is_exact``); anything else needs the owner's pick."""
    words = set(normalize_name(name).split())
    if not words:
        return ()
    return tuple(profile for profile in profiles
                 if words <= set(normalize_name(profile.name).split()))


def is_exact(name: str, profile: ClientReference) -> bool:
    return normalize_name(name) == normalize_name(profile.name)


def resolve_client_names(rules: tuple[PreferenceRule, ...], names: Sequence[str],
                         profiles: Sequence[ClientReference], selections: Mapping[str, str],
                         ) -> tuple[tuple[PreferenceRule, ...], tuple[PreferenceProblem, ...],
                                    tuple[AmbiguousClient, ...]]:
    """Bind each named rule to a profile id, or explain why that cannot happen yet.

    ``profiles`` must be the business's own active profiles, so another business's client can
    never be a candidate. A selection must name one of those profiles and must be a candidate
    for the name it answers, otherwise ``InvalidClientSelection``: it is refused, not ignored
    and not turned into general guidance.
    """
    by_id = {profile.client_id: profile for profile in profiles}
    chosen = {normalize_name(name): client_id for name, client_id in selections.items()}
    if any(client_id not in by_id for client_id in chosen.values()):
        raise InvalidClientSelection("Choose a client from this business")
    resolved: list[PreferenceRule] = []
    problems: list[PreferenceProblem] = []
    ambiguities: dict[str, AmbiguousClient] = {}
    unknown: set[str] = set()
    for rule, name in zip(rules, names, strict=True):
        key = normalize_name(name)
        if not key:
            resolved.append(rule)
            continue
        candidates = candidate_profiles(name, profiles)
        pick = chosen.get(key)
        if pick is not None and pick not in {item.client_id for item in candidates}:
            raise InvalidClientSelection(f"That client does not match the name {name!r}")
        if not candidates:
            if key not in unknown:
                unknown.add(key)
                problems.append(PreferenceProblem(
                    "unknown_client", f"No client named \"{name}\" was found. Correct the name "
                                      "or add the client first, then review again."))
            continue
        sure = len(candidates) == 1 and is_exact(name, candidates[0])
        if not sure and pick is None:
            if key not in ambiguities:
                ambiguities[key] = AmbiguousClient(name, candidates)
                problems.append(PreferenceProblem(
                    "ambiguous_client", f"\"{name}\" may mean more than one client, or only part "
                                        "of a client's name. Choose the one you mean, then "
                                        "review again."))
            continue
        resolved.append(PreferenceRule(rule.weekdays, rule.windows,
                                       pick if pick is not None else candidates[0].client_id))
    return tuple(resolved), tuple(problems), tuple(ambiguities.values())


def applicable_rules(preferences: SchedulingPreferences | None, timezone: str,
                     client_id: str | None) -> tuple[PreferenceRule, ...]:
    """The rules that guide one client: their own, plus business-wide ones on other weekdays.

    Client guidance overrides business-wide guidance on the weekdays it names (a rule
    naming Thursday replaces the general Thursday preference for that client, whatever its
    hours). Business-wide guidance on every other weekday stays. Other clients' rules never
    appear. No client (``None``) gets business-wide rules only. Preferences reviewed for
    another timezone apply to no one.
    """
    if preferences is None or preferences.timezone != timezone:
        return ()
    own = tuple(rule for rule in preferences.rules
                if client_id is not None and rule.client_id == client_id)
    named = {day for rule in own for day in rule.weekdays}
    general: list[PreferenceRule] = []
    for rule in preferences.rules:
        if rule.client_id is not None:
            continue
        days = tuple(day for day in rule.weekdays if day not in named)
        if days:
            general.append(PreferenceRule(days, rule.windows))
    return own + tuple(general)


def review_preferences(interpreter: PreferenceInterpreter, text: str, timezone: str,
                       profiles: Sequence[ClientReference] = (),
                       selections: Mapping[str, str] | None = None) -> PreferenceReview:
    """Interpret, resolve client names and validate. Writes nothing; activation is separate."""
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
    ambiguities: tuple[AmbiguousClient, ...] = ()
    try:
        names = _client_names(raw["rules"])
        rules = rules_from_payload(raw["rules"], frozenset({"client_name"}))
        rules, name_problems, ambiguities = resolve_client_names(
            rules, names, profiles, selections or {})
        problems.extend(name_problems)
    except InvalidClientSelection:
        raise
    except ValueError as exc:
        rules = ()
        problems.append(PreferenceProblem("unclear", f"The interpretation was not usable: {exc}."))
    if not problems and not rules:
        problems.append(PreferenceProblem(
            "unclear", "No weekday or start-time preference was found. Name the days or times "
                       "you prefer."))
    if problems:
        return PreferenceReview((), tuple(problems), timezone, None, (), ambiguities)
    by_id = {profile.client_id: profile for profile in profiles}
    clients = tuple(dict.fromkeys(by_id[rule.client_id] for rule in rules if rule.client_id))
    return PreferenceReview(rules, (), timezone, review_token(owner_text, rules, timezone), clients)


def _client_names(payload: object) -> list[str]:
    """The client name each raw rule mentions ('' for business-wide); strict about shape."""
    names = []
    for item in payload if isinstance(payload, list) else []:  # the rule parser rejects non-lists
        name = item.get("client_name", "") if isinstance(item, dict) else ""
        if not isinstance(name, str) or len(name.strip()) > MAX_CLIENT_NAME_CHARACTERS:
            raise ValueError("A client name must be short text")
        names.append(name.strip())
    return names


def reviewed_preferences(repository: SchedulingPreferencesRepository, business_id: str,
                         token: str, timezone: str, now: datetime,
                         active_client_ids: frozenset[str] = frozenset()) -> SchedulingPreferences:
    """The configuration an activation may store: exactly a review this server produced.

    The text and rules come from the stored review, never from the caller. Every client the
    review binds must still be an active profile of this business (``active_client_ids``),
    otherwise the review is refused and must be redone. A retry after the review expired, or
    after a client was later removed, is accepted only when that same review is already the
    active one, so a replayed activation stays idempotent without admitting an unreviewed
    configuration.
    """
    active = repository.read_scheduling_preferences(business_id).preferences
    if active is not None and active.review_token == token and active.timezone == timezone:
        return active
    stored = repository.read_preference_review(business_id, token, now)
    if stored is not None and stored.timezone == timezone:
        if any(rule.client_id not in active_client_ids
               for rule in stored.rules if rule.client_id is not None):
            raise ReviewMismatch("A client in this review is no longer available")
        return SchedulingPreferences(stored.text, stored.rules, stored.timezone, stored.token)
    raise ReviewMismatch("No current review matches; the review may have expired")


def save_preferences(repository: SchedulingPreferencesRepository, business_id: str,
                     actor_id: str, key: str, expected_version: int,
                     preferences: SchedulingPreferences | None) -> SchedulingPreferencesRecord:
    if expected_version < 0:
        raise ValueError("Expected version must be nonnegative")
    if not business_id or not actor_id or not key:
        raise ValueError("Owner and request identity are required")
    return repository.save_scheduling_preferences(business_id, actor_id, key, expected_version,
                                                  preferences)
