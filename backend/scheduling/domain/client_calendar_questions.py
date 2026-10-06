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
from scheduling.domain.conversation_state import clock_text, day_text, normalized, when_text
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

# A question about the client's own visits. Open times, and anything that asks
# for a change, go to the model instead and keep their existing handling.
TRIGGER = re.compile(
    r"\b(?:do i have|have i got|am i|is my|are my|when|how many|what|"
    r"what's|which|show|list|tell me)\b")
SUBJECT = re.compile(
    r"\b(?:bookings?|booked|appointments?|visits?|cleanings?|cleaners?|requests?|scheduled|"
    r"schedule|itinerary|calendar|coming|anything)\b")
NOT_A_QUESTION = re.compile(
    r"\b(?:open|openings?|available|availability|free|slots?|times|cancel\w*|reschedul\w*|"
    r"move|change|switch|instead|can|could|would|want|wanna|need|book|"
    r"can't|cannot|won't)\b")
# A short message that only names a booking ("Booking for Friday?") could mean
# either checking a visit or requesting a new one.
BOOKING_NOUN = re.compile(r"\b(?:bookings?|appointments?|cleanings?|visits?)\b")
AMBIGUOUS_FILLER = frozenset({"a", "an", "any", "the", "for", "on", "my", "this", "next",
                              "week", "s"})


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


def _clean(body: str) -> str:
    return re.sub(r"\s+", " ", normalized(body).replace("?", " ")).strip()


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
    if not text:
        return None
    ranges = sorted(set(ranges_in(text, today)))
    first, last = ranges[0] if len(ranges) == 1 else (None, None)
    view = View.COUNT if "how many" in text else View.LIST
    if TRIGGER.search(text) and SUBJECT.search(text) and not NOT_A_QUESTION.search(text):
        return ClientQuestion(view, first, last, statuses_named(text), len(ranges) > 1)
    rest = RANGE_TOKEN.sub(" ", text)
    words = re.findall(r"[a-z']+", rest)
    if (BOOKING_NOUN.search(rest) and len(text.split()) <= MAX_AMBIGUOUS_WORDS
            and all(word in AMBIGUOUS_FILLER or BOOKING_NOUN.fullmatch(word) for word in words)):
        return ClientQuestion(View.LIST, first, last, BOTH, len(ranges) > 1, True)
    return None


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

