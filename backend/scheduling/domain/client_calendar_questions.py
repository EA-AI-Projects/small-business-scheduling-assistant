"""Read-only answers to a verified client's questions about their own visits.

A question is recognized deterministically, or proposed by the model as the
``calendar_question`` intent with an optional day range. Either way the answer
text comes only from the client's own current visits, read for that message:
confirmed visits and unexpired pending requests that have not ended yet. No
other client's visits, owner blocks, notes, or profile fields are read into it,
and nothing here writes a booking, a hold, or conversation memory.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.conversation_state import (
    clock_text,
    day_text,
    is_affirmative,
    is_negative,
    normalized,
    when_text,
)
from scheduling.domain.owner_calendar_questions import (
    RANGE_TOKEN,
    STATUS_CONFIRMED,
    STATUS_PENDING,
)
from scheduling.domain.owner_calendar_questions import _ranges as ranges_in

CONFIRMED = CalendarStatus.CONFIRMED
PENDING = CalendarStatus.PENDING_APPROVAL
BOTH = frozenset({CONFIRMED, PENDING})
NOUNS = {CONFIRMED: ("confirmed visit", "confirmed visits"),
         PENDING: ("pending request", "pending requests")}
MAX_RANGE_DAYS = 62
MAX_AMBIGUOUS_WORDS = 6

# A question about the client's own visits: one sentence that starts like a
# schedule question, names a visit, and uses only schedule-question words. Any
# other word (a change, a preference, open times, a price) sends the text to
# the model instead, so a request is never swallowed by a read-only answer.
TRIGGER = re.compile(
    r"^(?:do i have|have i got|am i|is my|are my|when|how many|what|what's|show|list|"
    r"tell me|give me|summarize|summarise|recap)\b")
SUBJECT = re.compile(
    r"\b(?:bookings?|booked|appointments?|visits?|cleanings?|cleaners?|requests?|scheduled|"
    r"schedule|itinerary|calendar|anything)\b")
COUNTED = re.compile(r"\b(?:bookings?|appointments?|visits?|cleanings?|requests?)\b")
QUESTION_WORDS = frozenset({
    "do", "i", "have", "got", "am", "is", "are", "my", "the", "a", "an", "any", "anything",
    "when", "what", "what's", "whats", "how", "many", "show", "list", "tell", "me", "time",
    "booking", "bookings", "booked", "appointment", "appointments", "visit", "visits",
    "cleaning", "cleanings", "cleaner", "cleaners", "request", "requests", "scheduled",
    "schedule", "itinerary", "calendar", "coming", "up", "upcoming", "next", "this", "week",
    "on", "for", "in", "at", "still", "yet", "confirmed", "pending", "approved", "and", "or",
    "both", "all", "of", "s", "give", "summarize", "summarise", "recap", "summary"})
# "Summarize my itinerary" right after a calendar answer means the range just shown.
REFERS_BACK = re.compile(r"\b(?:summari[sz]e|summary|recap)\b")
# A short follow-up to the last calendar answer: only range, status, and view words.
MAX_FOLLOW_UP_WORDS = 8
FOLLOW_UP_WORDS = frozenset({
    "what", "what's", "whats", "about", "and", "how", "many", "just", "only", "the", "ones",
    "one", "those", "them", "confirmed", "pending", "request", "requests", "awaiting",
    "unconfirmed", "approved", "visit", "visits", "cleaning", "cleanings", "booking",
    "bookings", "appointment", "appointments", "show", "list", "me", "all", "both",
    "everything", "instead", "for", "on", "in", "ok", "okay", "my", "of",
    "this", "next", "week", "s", "count", "so", "too", "also", "or"})
# Words that make a short text refer back to the last answer ("And Friday?").
FOLLOW_UP_CUE = re.compile(
    r"\b(?:what|what's|whats|about|and|how|just|only|ones|those|them|instead|so|too|also)\b")
EVERYTHING = re.compile(r"\b(?:all|both|everything)\b")
LIST_WORDS = re.compile(r"\b(?:show|list)\b")
# A short question that only names a booking ("Booking for Friday?") could mean
# either checking a visit or requesting a new one.
BOOKING_NOUN = re.compile(r"\b(?:bookings?|appointments?|cleanings?|visits?)\b")
AMBIGUOUS_FILLER = frozenset({"a", "an", "any", "the", "for", "on", "my", "this", "next",
                              "week", "s"})


def wants_count(body: str) -> bool:
    return bool(re.search(r"\bhow many\b", body, re.IGNORECASE))


class View(StrEnum):
    LIST = "list"
    COUNT = "count"


@dataclass(frozen=True)
class ClientQuestion:
    view: View
    first: date | None  # None with last: every upcoming visit.
    last: date | None
    statuses: frozenset[CalendarStatus]
    multiple_ranges: bool = False
    ambiguous_booking: bool = False  # Check visits, or request a new cleaning?
    refers_back: bool = False  # No range of its own: a follow-up keeps the last one.


def _clean(body: str) -> str:
    return re.sub(r"\s+", " ", normalized(body).replace("?", " ")).strip()


def asks_for_list(body: str) -> bool:
    return bool(re.search(r"\b(?:show|list|which|when|what)\b", _clean(body)))


def mentions_status(body: str) -> bool:
    text = _clean(body)
    return bool(STATUS_CONFIRMED.search(text) or STATUS_PENDING.search(text)
                or EVERYTHING.search(text))


ACTION_LIKE = re.compile(
    r"(?:option\s*)?\d{1,2}|(?:the\s+)?(?:first|second|third|fourth|fifth|last)(?:\s+one)?"
    r"|that one|this one")


def is_action_like(body: str) -> bool:
    """A bare yes, no, number, or pick: an answer to a prompt a calendar answer never asks."""
    return (is_affirmative(body) or is_negative(body)
            or bool(ACTION_LIKE.fullmatch(_clean(body))))


def statuses_named(body: str) -> frozenset[CalendarStatus]:
    """Statuses the text asks about; both unless it names exactly one."""
    text = _clean(body)
    confirmed = bool(STATUS_CONFIRMED.search(text))
    pending = bool(STATUS_PENDING.search(text)) or "approval" in text
    if confirmed != pending:
        return frozenset({CONFIRMED if confirmed else PENDING})
    return BOTH


def parse(body: str, today: date) -> ClientQuestion | None:
    """A question about the client's own visits, or None for every other text."""
    text = _clean(body)
    # One sentence only: "When is my visit? Cancel it" is a change, not a question.
    if not text or re.search(r"[.!?;]", re.sub(r"[.!?\s]+$", "", body)):
        return None
    ranges = sorted(set(ranges_in(text, today)))
    first, last = ranges[0] if len(ranges) == 1 else (None, None)
    words = re.findall(r"[a-z']+", RANGE_TOKEN.sub(" ", text))
    if TRIGGER.search(text) and SUBJECT.search(text) and all(
            word in QUESTION_WORDS for word in words):
        if wants_count(text) and not COUNTED.search(text):
            return None  # "How many cleaners are coming?" is not a visit count.
        view = View.COUNT if wants_count(text) else View.LIST
        return ClientQuestion(view, first, last, statuses_named(text), len(ranges) > 1,
                              refers_back=bool(REFERS_BACK.search(text)) and not ranges)
    asked = normalized(body).endswith("?")
    # A clock time ("Cleaning Friday 10?") is a booking request: the model reads it.
    timed = bool(re.search(r"\d", RANGE_TOKEN.sub(" ", text)))
    if (asked and not timed and BOOKING_NOUN.search(text) and len(text.split()) <= MAX_AMBIGUOUS_WORDS
            and all(word in AMBIGUOUS_FILLER or BOOKING_NOUN.fullmatch(word)
                    for word in words)):
        return ClientQuestion(View.LIST, first, last, BOTH, len(ranges) > 1, True)
    return None


def parse_followup(body: str, today: date, previous: ClientQuestion) -> ClientQuestion | None:
    """A short follow-up ("What about next week?", "Just confirmed ones") to the last
    calendar question, merged with its range, statuses, and view; None for other text."""
    text = _clean(body)
    if (not text or len(text.split()) > MAX_FOLLOW_UP_WORDS
            or re.search(r"[.!?;]", re.sub(r"[.!?\s]+$", "", body))):
        return None
    rest = RANGE_TOKEN.sub(" ", text)
    words = re.findall(r"[a-z']+", rest)
    # A clock time ("Friday 10") or a word outside the list is a request: the model reads it.
    if re.search(r"\d", rest) or not all(word in FOLLOW_UP_WORDS for word in words):
        return None
    ranges = sorted(set(ranges_in(text, today)))
    named = bool(STATUS_CONFIRMED.search(text) or STATUS_PENDING.search(text))
    everything = bool(EVERYTHING.search(text))
    shown_again = named or everything or wants_count(text) or bool(LIST_WORDS.search(text))
    # "Cleaning Friday instead" or "What about cleaning Friday" may be a new request; "my
    # visits" or a plural noun points back at the visits already shown.
    single = re.search(r"\b(?:booking|appointment|cleaning|visit)\b", rest)
    if ranges and single and not shown_again and not re.search(r"\bmy\b", rest):
        first, last = ranges[0] if len(ranges) == 1 else (None, None)
        return ClientQuestion(View.LIST, first, last, BOTH, len(ranges) > 1, True)
    if not (shown_again or FOLLOW_UP_CUE.search(rest)):
        # A bare range ("Friday") goes to the model, which may read it as a request. With a
        # booking noun ("Booking for Friday") it could mean either: ask which.
        if ranges and BOOKING_NOUN.search(rest):
            first, last = ranges[0] if len(ranges) == 1 else (None, None)
            return ClientQuestion(View.LIST, first, last, BOTH, len(ranges) > 1, True)
        return None
    first, last = ranges[0] if len(ranges) == 1 else (previous.first, previous.last)
    statuses = (BOTH if everything else statuses_named(text) if named
                else previous.statuses)
    view = (View.COUNT if wants_count(text) else View.LIST if LIST_WORDS.search(text)
            else previous.view)
    return ClientQuestion(view, first, last, statuses, len(ranges) > 1)


def _span(first: date, last: date) -> str:
    return f"on {day_text(first)}" if first == last else (
        f"from {day_text(first)} to {day_text(last)}")


def _plural(count: int, status: CalendarStatus) -> str:
    return f"{count} {NOUNS[status][0 if count == 1 else 1]}"


def _line(visit: Appointment, zone: ZoneInfo) -> str:
    zone_name = visit.start_at.astimezone(zone).strftime("%Z")
    when = f"{when_text(visit.start_at, zone)}-{clock_text(visit.end_at, zone)} {zone_name}"
    if visit.status == CONFIRMED:
        return f"{when}, confirmed (ref {visit.appointment_id[:8]})"
    return (f"{when}, pending owner approval, not confirmed yet "
            f"(ref {visit.appointment_id[:8]})")


def booking_choice(question: ClientQuestion) -> str:
    """Ask whether a bare "booking" means existing visits or a new request. Writes nothing."""
    if (question.first is not None and question.last is not None
            and question.first <= question.last):
        when = (day_text(question.first) if question.first == question.last
                else f"{day_text(question.first)} to {day_text(question.last)}")
        return (f"Do you want to check the visits you already have for {when}, or request "
                f"a new cleaning? Ask \"Do I have a visit {when}?\" or \"What times are "
                f"open {when}?\"")
    return ("Do you want to check the visits you already have, or request a new cleaning? "
            "Ask \"When is my next visit?\" or tell me what day you'd like a cleaning.")


def answer(question: ClientQuestion, visits: tuple[Appointment, ...], now: datetime,
           zone: ZoneInfo) -> str:
    """Reply text from ``visits``: the client's own current visits, read for this message."""
    if question.ambiguous_booking:
        return booking_choice(question)
    if question.multiple_ranges:
        return ("I can check one day or one week at a time. Which do you mean: tomorrow, "
                "a weekday like Friday, this week, next week, or a date?")
    today = now.astimezone(zone).date()
    first, last = question.first, question.last
    if first is not None and last is not None:
        if last < first or (last - first).days >= MAX_RANGE_DAYS:
            return ("Which day or week do you mean? For example: tomorrow, Friday, "
                    "this week, or next week.")
        if last < today:
            return ("That date has passed. I can check visits that haven't happened yet: "
                    "ask about today or a later day.")
        first = max(first, today)
        span = _span(first, last)
        shown = tuple(visit for visit in visits
                      if first <= visit.start_at.astimezone(zone).date() <= last)
    else:
        span = "coming up"
        shown = visits
    wanted = question.statuses
    matching = tuple(visit for visit in shown if visit.status in wanted)
    others = tuple(visit for visit in shown if visit.status not in wanted)
    kinds = " or ".join(NOUNS[status][1] for status in (CONFIRMED, PENDING) if status in wanted)
    if question.view == View.COUNT:
        counted = ", ".join(_plural(sum(1 for visit in matching if visit.status == status),
                                    status)
                            for status in (CONFIRMED, PENDING) if status in wanted)
        text = f"You have {counted} {span}."
    elif not matching:
        text = f"You have no {kinds} {span}."
    else:
        tally = ", ".join(_plural(count, status) for status in (CONFIRMED, PENDING)
                          if (count := sum(1 for visit in matching if visit.status == status)))
        text = f"You have {tally} {span}:\n" + "\n".join(
            f"- {_line(visit, zone)}" for visit in matching)
    if others:
        # Never let a filtered answer hide a pending request or imply it is confirmed.
        extra = ", ".join(_plural(sum(1 for visit in others if visit.status == status), status)
                          for status in (CONFIRMED, PENDING)
                          if any(visit.status == status for visit in others))
        text += f"\nYou also have {extra} {span}."
        if question.view == View.LIST:
            text += "\n" + "\n".join(f"- {_line(visit, zone)}" for visit in others)
    elif not shown and question.view == View.LIST:
        text += " To ask for a cleaning, tell me what day works."
    return text


def range_from_proposal(date_from: str | None, date_to: str | None
                        ) -> tuple[date | None, date | None] | None:
    """The model's day range for a calendar question; None when it is unusable."""
    if date_from is None and date_to is None:
        return None, None
    try:
        first = date.fromisoformat(date_from or date_to or "")
        last = date.fromisoformat(date_to or date_from or "")
    except ValueError:
        return None
    return first, last

