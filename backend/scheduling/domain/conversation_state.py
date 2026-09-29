"""Short-lived memory of the last offer or confirmation prompt sent to one sender.

The model never selects a calendar write. A client's reply changes the calendar
only when it maps deterministically to exactly one option this memory holds,
and only before the memory expires.
"""

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

PROMPT_LIFETIME = timedelta(minutes=30)
MIN_OPTIONS = 3
MAX_OPTIONS = 5
MAX_SELECTION_LENGTH = 80


class PromptKind(StrEnum):
    OFFER = "offer"  # Numbered start times for a new request or a replacement.
    CONFIRM_CANCEL = "confirm_cancel"  # "Cancel your ... visit? Reply YES."
    RESCHEDULE_DAY = "reschedule_day"  # Which day to move a known visit to.
    CHOOSE_CANCEL = "choose_cancel"  # Which of several listed visits to cancel.
    CHOOSE_MOVE = "choose_move"  # Which of several listed visits to move.


CHOICE_KINDS = frozenset({PromptKind.OFFER, PromptKind.CHOOSE_CANCEL, PromptKind.CHOOSE_MOVE})


@dataclass(frozen=True)
class ConversationState:
    business_id: str
    sender: str
    state_id: str
    kind: PromptKind
    created_at: datetime
    expires_at: datetime
    options: tuple[datetime, ...] = ()
    appointment_id: str | None = None
    appointment_version: int | None = None

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Conversation state times must be timezone-aware")
        if not self.state_id or self.expires_at <= self.created_at:
            raise ValueError("Conversation state identity or lifetime is invalid")
        if any(option.tzinfo is None for option in self.options):
            raise ValueError("Offered starts must be timezone-aware")
        if self.kind in CHOICE_KINDS and not 0 < len(self.options) <= MAX_OPTIONS:
            raise ValueError("A list of choices holds one to five options")
        if self.kind not in CHOICE_KINDS and (self.options or self.appointment_id is None):
            raise ValueError("A confirmation prompt names one appointment and no options")
        if self.kind in (PromptKind.CHOOSE_CANCEL, PromptKind.CHOOSE_MOVE) and \
                self.appointment_id is not None:
            raise ValueError("A list of visits names no single appointment")

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class ConversationStateStore(Protocol):
    def read_state(self, business_id: str, sender: str) -> ConversationState | None: ...
    def put_state(self, state: ConversationState) -> None: ...
    def clear_state(self, business_id: str, sender: str, state_id: str) -> None: ...


class InMemoryConversationStates:
    """Process-local memory for tests and the synthetic local harnesses."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], ConversationState] = {}
        self._lock = threading.Lock()

    def read_state(self, business_id: str, sender: str) -> ConversationState | None:
        with self._lock:
            return self._states.get((business_id, sender))

    def put_state(self, state: ConversationState) -> None:
        with self._lock:
            self._states[(state.business_id, state.sender)] = state

    def clear_state(self, business_id: str, sender: str, state_id: str) -> None:
        with self._lock:
            current = self._states.get((business_id, sender))
            if current is not None and current.state_id == state_id:
                del self._states[(business_id, sender)]


def when_text(start: datetime, zone: ZoneInfo) -> str:
    """Full local date and time, matching the notification templates."""
    local = start.astimezone(zone)
    return f"{local.strftime('%a %b')} {local.day} at {clock_text(start, zone)}"


def day_text(day: date) -> str:
    return f"{day.strftime('%a %b')} {day.day}"


def clock_text(start: datetime, zone: ZoneInfo) -> str:
    return start.astimezone(zone).strftime("%I:%M %p").lstrip("0")


def spread(starts: tuple[datetime, ...], zone: ZoneInfo,
           limit: int = MAX_OPTIONS) -> tuple[datetime, ...]:
    """Pick up to ``limit`` starts spread across days and across each day."""
    if len(starts) <= limit:
        return tuple(sorted(starts))
    by_day: dict[date, list[datetime]] = {}
    for start in sorted(starts):
        by_day.setdefault(start.astimezone(zone).date(), []).append(start)
    days = sorted(by_day)[:limit]
    quotas = [limit // len(days) + (1 if index < limit % len(days) else 0)
              for index in range(len(days))]
    chosen: list[datetime] = []
    for day, quota in zip(days, quotas, strict=True):
        choices = by_day[day]
        # Round times are easier to read and to answer ("10 works").
        for minutes in ((0,), (0, 30)):
            rounder = [start for start in choices if start.astimezone(zone).minute in minutes]
            if len(rounder) >= quota:
                choices = rounder
                break
        if len(choices) <= quota:
            chosen.extend(choices)
        elif quota == 1:
            chosen.append(choices[0])
        else:
            step = (len(choices) - 1) / (quota - 1)
            chosen.extend(choices[round(index * step)] for index in range(quota))
    return tuple(sorted(set(chosen)))


# Reply parsing is deliberately narrow: anything it does not recognize falls back
# to the model, which can only produce a new offer or a question.
NEGATION = re.compile(
    r"\b(?:no|not|nope|nah|never|none|neither|nor|don'?t|dont|can'?t|cant|cannot|"
    r"won'?t|wont|wouldn'?t|shouldn'?t|isn'?t|doesn'?t|stop|wait|hold)\b|n't\b")
NOT_A_SELECTION = re.compile(
    r"\?|\bor\b|\b(?:cancel|reschedul\w*|move|change|instead|different|other|another|"
    r"else|later|earlier|after|before|between|next|week|weekend|month|jan\w*|feb\w*|"
    r"mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)\b")
AFFIRMATIVE = frozenset({"yes", "y", "yep", "yeah", "ya", "yup", "sure", "ok", "okay",
                         "confirm", "confirmed", "correct", "perfect", "great", "works",
                         "good", "fine"})
FILLER = frozenset({"please", "pls", "thanks", "thank", "you", "that", "it", "sounds", "good",
                    "fine", "for", "me", "the", "one", "a", "an", "let's", "lets", "i'll",
                    "i'd", "i", "take", "will", "would", "like", "want", "is", "at", "on",
                    "can", "do", "we", "o'clock", "oclock", "option", "number", "slot",
                    "time", "work", "go", "with", "then", "and", "just", "choose", "pick",
                    "book", "it's", "that's", "great", "ahead"})
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
            "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}
_TIME = re.compile(
    r"(?<![\w:])(?P<index>(?:#|no\.?|option|number)\s*)?(?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap]\.?m\.?)?(?![\w:])")
_WORD = re.compile(r"[a-z0-9#]+(?:'[a-z]+)?")


def normalized(body: str) -> str:
    text = re.sub(r"\s+", " ", body.strip().lower().replace("\u2019", "'"))
    return re.sub(r"[.!,]+$", "", text).strip()


def is_affirmative(body: str, extra_words: frozenset[str] = frozenset()) -> bool:
    """A short, plain yes with no negation, number, question, or alternative."""
    text = normalized(body)
    if (not text or len(text) > 40 or NEGATION.search(text)
            or re.search(r"\?|\d|\bor\b", text)):
        return False
    words = _WORD.findall(text.replace(",", " "))
    return (any(word in AFFIRMATIVE for word in words)
            and all(word in AFFIRMATIVE or word in FILLER or word in extra_words
                    for word in words))


def is_negative(body: str) -> bool:
    return bool(re.fullmatch(
        r"(?:no|nope|nah|no thanks|no thank you|never ?mind|keep it|"
        r"no,? (?:please )?keep it|no,? don'?t(?: cancel(?: it)?)?)", normalized(body)))


class Selection(StrEnum):
    MATCH = "match"
    AMBIGUOUS = "ambiguous"  # Looks like a choice but could mean more than one option.
    UNMATCHED = "unmatched"  # Names a time or number that was not offered.
    NONE = "none"  # Not an answer to the offer; let the model interpret it.


def match_selection(body: str, options: tuple[datetime, ...], zone: ZoneInfo,
                    today: date) -> tuple[Selection, int | None]:
    """Map a reply to exactly one offered option, or report why it cannot."""
    text = normalized(body)
    if (not text or len(text) > MAX_SELECTION_LENGTH or NEGATION.search(text)
            or NOT_A_SELECTION.search(text)):
        return Selection.NONE, None
    times = list(_TIME.finditer(text))
    words = _WORD.findall(_TIME.sub(" ", text).replace(",", " "))
    local = [option.astimezone(zone) for option in options]
    scope = set(range(len(options)))
    ordinals: list[int] = []
    for word in words:
        if word == "noon":
            continue
        if word in ("today", "tomorrow"):
            day = today + timedelta(days=1 if word == "tomorrow" else 0)
            scope &= {index for index, start in enumerate(local) if start.date() == day}
        elif word.removesuffix("s") in WEEKDAYS:
            weekday = WEEKDAYS.index(word.removesuffix("s"))
            scope &= {index for index, start in enumerate(local) if start.weekday() == weekday}
        elif word in ORDINALS or word == "last":
            ordinals.append(ORDINALS.get(word, len(options)))
        elif word not in FILLER and word not in AFFIRMATIVE:
            return Selection.NONE, None
    selectors = len(times) + len(ordinals) + words.count("noon")
    if selectors > 1:
        return Selection.AMBIGUOUS, None
    if selectors == 0:
        named_day = len(scope) < len(options)
        if not (named_day or is_affirmative(text)):
            return Selection.NONE, None
        if len(scope) == 1:
            return Selection.MATCH, next(iter(scope))
        return (Selection.AMBIGUOUS if scope else Selection.UNMATCHED), None
    candidates: set[int] = set()
    if ordinals:
        if 1 <= ordinals[0] <= len(options):
            candidates.add(ordinals[0] - 1)
    elif "noon" in words:
        candidates.update(index for index, start in enumerate(local)
                          if (start.hour, start.minute) == (12, 0))
    else:
        match = times[0]
        hour, minute = int(match["hour"]), int(match["minute"] or 0)
        meridiem = (match["meridiem"] or "").replace(".", "")
        bare = not match["minute"] and not meridiem
        if match["index"]:
            if bare and 1 <= hour <= len(options):
                candidates.add(hour - 1)
        else:
            if bare and 1 <= hour <= len(options):
                candidates.add(hour - 1)  # "2" may be option 2 or 2 o'clock.
            if meridiem:
                hours = {hour % 12 + (12 if meridiem == "pm" else 0)} if 1 <= hour <= 12 else set()
            elif hour == 0 or hour > 12:
                hours = {hour}
            else:
                hours = {hour % 12, hour % 12 + 12}
            candidates.update(index for index, start in enumerate(local)
                              if start.hour in hours and start.minute == minute)
    candidates &= scope
    if len(candidates) == 1:
        return Selection.MATCH, next(iter(candidates))
    return (Selection.AMBIGUOUS if candidates else Selection.UNMATCHED), None
