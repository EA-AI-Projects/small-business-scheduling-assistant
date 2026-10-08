"""Read-only answers to a verified client's questions about their own visits.

The model interprets the question, in any wording or language, as the
``calendar_question`` intent with a day range, the statuses asked about, and
list or count (#241). The answer text comes only from the client's own current
visits, read for that message: confirmed visits and unexpired pending requests
that have not ended yet. No other client's visits, owner blocks, notes, or
profile fields are read into it, and nothing here writes a booking or a hold.

The only fixed word rules here guard replies that could otherwise write: a bare
yes, no, or pick right after a calendar answer, and a status word that keeps a
reply from answering an owner counteroffer.
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

CONFIRMED = CalendarStatus.CONFIRMED
PENDING = CalendarStatus.PENDING_APPROVAL
BOTH = frozenset({CONFIRMED, PENDING})
NOUNS = {CONFIRMED: ("confirmed visit", "confirmed visits"),
         PENDING: ("pending request", "pending requests")}
STATUS_NAMES = {"confirmed": CONFIRMED, "pending": PENDING}
MAX_RANGE_DAYS = 62


class View(StrEnum):
    LIST = "list"
    COUNT = "count"


@dataclass(frozen=True)
class ClientQuestion:
    view: View
    first: date | None  # None with last: every upcoming visit.
    last: date | None
    statuses: frozenset[CalendarStatus]
    ambiguous_booking: bool = False  # Check visits, or request a new cleaning?


# Write guards. A calendar answer offers nothing to pick or confirm, so these replies
# right after one are told that nothing changed instead of acting on an older prompt.
ACTION_LIKE = re.compile(
    r"(?:option\s*)?\d{1,2}|(?:the\s+)?(?:first|second|third|fourth|fifth|last)(?:\s+one)?"
    r"|that one|this one")
STATUS_WORD = re.compile(r"\b(?:confirmed|pending|requests?|unconfirmed|all|both)\b")


def is_action_like(body: str) -> bool:
    """A bare yes, no, number, or pick: an answer to a prompt a calendar answer never asks.
    "Just the confirmed one" names a status, so the model reads it as a follow-up."""
    if mentions_status(body):
        return False
    return (is_affirmative(body) or is_negative(body)
            or bool(ACTION_LIKE.fullmatch(normalized(body).rstrip("?"))))


def mentions_status(body: str) -> bool:
    """"Confirmed" is also a yes; inside a calendar conversation it names a status, so it
    must not answer an owner counteroffer."""
    return bool(STATUS_WORD.search(normalized(body)))


def statuses_from_proposal(names: tuple[str, ...] | None) -> frozenset[CalendarStatus] | None:
    if not names:
        return None
    return frozenset(STATUS_NAMES[name] for name in names if name in STATUS_NAMES) or None


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


def compact_list(question: ClientQuestion, visits: tuple[Appointment, ...],
                 now: datetime, zone: ZoneInfo) -> str:
    """List every matching current visit when the detailed answer exceeds one SMS.

    The conversation service has already limited this client to eight current
    visits. The compact form drops end times and references, not dates, start
    times, or status. A pending request is explicitly not confirmed.
    """
    today = now.astimezone(zone).date()
    shown = tuple(visit for visit in visits
                  if (question.first is None or question.last is None
                      or max(question.first, today) <= visit.start_at.astimezone(zone).date()
                      <= question.last))
    lines = [f"Visits ({zone.key}; pending = awaiting owner approval, not confirmed):"]
    for visit in shown:
        start = visit.start_at.astimezone(zone)
        status = "confirmed" if visit.status == CONFIRMED else "pending"
        lines.append(f"- {start:%Y-%m-%d %H:%M} {status}")
    return "\n".join(lines)


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
