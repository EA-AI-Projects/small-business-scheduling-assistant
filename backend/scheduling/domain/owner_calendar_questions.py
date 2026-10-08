"""Read-only answers to the verified owner's calendar questions.

Question types are fixed and parsed deterministically: a day or week summary,
the clients scheduled in a range, and a count. The text of the answer comes only
from the authoritative calendar and client records read at that moment; nothing
here writes a booking, a block, or a client offer. The only state kept is a
short-lived record of the last question so a follow-up can narrow or expand it.
"""

import re
import threading
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import AvailabilityPolicy
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot, CalendarStatus
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.conversation_state import clock_text, day_text, normalized

QUESTION_LIFETIME = timedelta(minutes=30)
# A clarifying question about a named request: a reply to it counts for this long after it was
# sent. An unsent one blocks approvals for the longer retention, so a resend cannot skip it.
CLARIFICATION_LIFETIME = timedelta(minutes=30)
CLARIFICATION_RETENTION = timedelta(hours=24)
ASK_LIFETIME = timedelta(minutes=10)  # An unanswered clarifying question goes stale sooner.
MAX_REPLY_LENGTH = 480  # Three GSM-7 segments (160 each); longer answers are paged, never cut.
FOOTER_ROOM = 60
MAX_NAME_LENGTH = 60
MAX_FOLLOW_UP_WORDS = 8
MAX_FOLLOW_UP_DAYS = 31

CONFIRMED = CalendarStatus.CONFIRMED
PENDING = CalendarStatus.PENDING_APPROVAL
BLOCK = CalendarStatus.UNAVAILABLE
ALL_STATUSES = frozenset({CONFIRMED, PENDING, BLOCK})
ORDER = (CONFIRMED, PENDING, BLOCK)
NOUNS = {CONFIRMED: ("confirmed visit", "confirmed visits"),
         PENDING: ("pending request", "pending requests"),
         BLOCK: ("unavailable block", "unavailable blocks")}


def last_answered_at(context: "QuestionContext") -> datetime:
    """When the assistant last spoke in this calendar conversation."""
    return context.answered_at if context.answered_at is not None else context.created_at


RANGE_QUESTION = ("Which day or week do you mean? For example: tomorrow, Friday, "
                  "this week, next week, or a date like 2026-10-09.")


@dataclass(frozen=True)
class CalendarAnswer:
    """Reply text, plus the parts a model may reword when it is a rendered calendar page.

    ``body`` is the rendered header and entries and ``footer`` the paging line that follows
    it; ``text`` is exactly ``body + footer``. ``model_body`` is ``body`` with client first
    names only. Questions and notices have no ``body`` and are never reworded.
    """

    text: str
    body: str | None = None
    footer: str = ""
    model_body: str | None = None


class View(StrEnum):
    SUMMARY = "summary"
    CLIENTS = "clients"
    COUNT = "count"


class Ask(StrEnum):
    RANGE = "range"
    STATUS = "status"


@dataclass(frozen=True)
class QuestionContext:
    business_id: str
    sender: str
    view: View
    first: date | None
    last: date | None
    statuses: frozenset[CalendarStatus] | None
    skip: int  # Offset of the next entry for "MORE"; 0 when everything was shown.
    ask: Ask | None
    created_at: datetime
    expires_at: datetime
    page_start: int = 0  # Offset of the page last sent, so a redelivered MORE repeats it.
    receipt_id: str = ""  # The inbound message that produced that page.
    fingerprint: str = ""  # Hash of the full entry list the offsets refer to.
    clarified_request: str = ""  # Request the assistant last asked the owner about.
    clarified_version: int = 0  # Its version when asked; approval needs the same version.
    clarified_by: str = ""  # Inbound message that asked; its redelivery must ask again.
    clarified_at: datetime | None = None  # When it asked (our clock); answers must be later.
    answered_at: datetime | None = None  # When the assistant last answered or asked a range.

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class QuestionContextStore(Protocol):
    def read_context(self, business_id: str, sender: str) -> QuestionContext | None: ...
    def put_context(self, context: QuestionContext) -> None: ...


class InMemoryQuestionContexts:
    """Process-local memory for tests and the synthetic local harnesses."""

    def __init__(self) -> None:
        self._contexts: dict[tuple[str, str], QuestionContext] = {}
        self._lock = threading.Lock()

    def read_context(self, business_id: str, sender: str) -> QuestionContext | None:
        with self._lock:
            return self._contexts.get((business_id, sender))

    def put_context(self, context: QuestionContext) -> None:
        with self._lock:
            self._contexts[(context.business_id, context.sender)] = context


@runtime_checkable
class ReplyLookup(Protocol):
    """Whether the reply to an inbound message was saved, and when our sender sent it."""

    def read_reply_text(self, business_id: str, provider_id: str) -> str | None: ...

    def read_reply_sent_at(self, business_id: str, provider_id: str) -> datetime | None:
        """When the reply was sent, or None unless its outbox item is SENT."""
        ...


class QuestionRepository(Protocol):
    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...
    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...
    def read_appointment(self, appointment_id: str) -> Appointment | None: ...


# --- Parsing -----------------------------------------------------------------

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
RANGE_TOKEN = re.compile(
    r"(?P<iso>(?<!\d)\d{4}-\d{2}-\d{2}(?!\d))"
    r"|(?P<month>\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<mday>\d{1,2})(?:st|nd|rd|th)?\b)"
    r"|(?P<week>\b(?P<which>this|next)\s+week\b)"
    r"|(?P<weekday>\b(?:(?P<qual>this|next)\s+)?"
    r"(?P<name>monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b)"
    r"|(?P<relative>\b(?:today|tomorrow|yesterday)\b)")
TRIGGER = re.compile(
    r"\b(?:how many|what|what's|which|who|show|list|give me|tell me|do i have|do we have|"
    r"am i|are we)\b")
SUBJECT = re.compile(
    r"\b(?:bookings?|appointments?|visits?|clients?|customers?|requests?|jobs?|schedule|"
    r"calendar|agenda|looking like|look like|booked|blocks?|blocked|unavailable|pending|"
    r"have|on the books|on my plate|coming|scheduled)\b")
STATUS_PENDING = re.compile(r"\b(?:pending|requests?|awaiting|unconfirmed|holds?)\b")
STATUS_CONFIRMED = re.compile(r"\bconfirmed\b")
STATUS_BLOCK = re.compile(r"\b(?:blocks?|blocked|unavailable|time off)\b")
STATUS_ALL = re.compile(r"\b(?:all|everything)\b")
STATUS_BOTH = re.compile(r"\bboth\b")
ADDITIVE = re.compile(r"\b(?:including|include|plus|with|too|also)\b")
GENERIC_BOOKING = re.compile(
    r"\b(?:bookings?|appointments?|visits?|jobs?|clients?|customers?|cleanings?)\b")
CLIENT_QUESTION = re.compile(r"\b(?:who|clients?|customers?|names?)\b")
BREAKDOWN = re.compile(r"\bby day\b|\bbreak ?down\b|\bdaily\b|\beach day\b")
MORE = re.compile(r"(?:more|show more|next|continue|the rest|rest)")
FOLLOW_UP_FILLER = frozenset({
    "what", "whats", "what's", "about", "and", "how", "the", "only", "just", "include",
    "including", "also", "with", "without", "for", "on", "in", "please", "then", "ok",
    "okay", "instead", "show", "me", "them", "it", "that", "those", "ones", "one", "is",
    "are", "do", "we", "i", "have", "my", "of", "to", "a", "by", "day", "days", "break",
    "down", "breakdown", "list", "give", "tell", "many", "who", "names", "name", "can",
    "you", "too", "plus", "all", "both", "everything", "week", "weeks", "daily", "each",
    "per", "or", "next", "this", "s", "looking", "like", "was", "were", "count",
    "total", "number", "status", "statuses"})


@dataclass(frozen=True)
class ParsedQuestion:
    fresh: bool  # A complete new question, as opposed to a follow-up.
    view: View | None
    first: date | None
    last: date | None
    statuses: frozenset[CalendarStatus] | None
    multiple_ranges: bool = False
    more: bool = False


def _month_day(name: str, day: int, today: date) -> date | None:
    month = MONTHS.index(name[:3]) + 1
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today - timedelta(days=60):
            return candidate
    return None


def _ranges(text: str, today: date) -> list[tuple[date, date]]:
    found: list[tuple[date, date]] = []
    monday = today - timedelta(days=today.weekday())
    for match in RANGE_TOKEN.finditer(text):
        if match["iso"]:
            try:
                day = date.fromisoformat(match["iso"])
            except ValueError:
                continue
            found.append((day, day))
        elif match["month"]:
            month_day = _month_day(match["month"], int(match["mday"]), today)
            if month_day is not None:
                found.append((month_day, month_day))
        elif match["week"]:
            start = monday + timedelta(days=7 if match["which"] == "next" else 0)
            found.append((start, start + timedelta(days=6)))
        elif match["weekday"]:
            target = WEEKDAYS.index(match["name"])
            if match["qual"] == "next":
                weekday = monday + timedelta(days=7 + target)
            else:
                weekday = today + timedelta(days=(target - today.weekday()) % 7)
            found.append((weekday, weekday))
        else:
            offset = {"today": 0, "tomorrow": 1, "yesterday": -1}[match["relative"]]
            relative = today + timedelta(days=offset)
            found.append((relative, relative))
    return found


def _named_statuses(text: str) -> frozenset[CalendarStatus] | None:
    if STATUS_ALL.search(text):
        return ALL_STATUSES
    if STATUS_BOTH.search(text):
        return frozenset({CONFIRMED, PENDING})
    named: set[CalendarStatus] = set()
    if STATUS_PENDING.search(text):
        named.add(PENDING)
    if STATUS_CONFIRMED.search(text):
        named.add(CONFIRMED)
    if STATUS_BLOCK.search(text):
        named.add(BLOCK)
    if named == {PENDING} and ADDITIVE.search(text):
        named.add(CONFIRMED)
    return frozenset(named) if named else None


def _statuses(text: str, view: View | None) -> frozenset[CalendarStatus] | None:
    """Statuses a question asks about, or None when it does not say."""
    named = _named_statuses(text)
    if named is not None:
        return named
    if view == View.SUMMARY:
        return ALL_STATUSES
    if view == View.CLIENTS:
        return frozenset({CONFIRMED, PENDING})
    return None  # A count of "bookings" does not say which statuses; the caller asks.


def _view_of(text: str) -> View:
    if "how many" in text:
        return View.COUNT
    if CLIENT_QUESTION.search(text):
        return View.CLIENTS
    return View.SUMMARY


def _follow_up_only(text: str) -> bool:
    """True when nothing but range, status, and view words remain."""
    rest = RANGE_TOKEN.sub(" ", text)
    for pattern in (STATUS_PENDING, STATUS_CONFIRMED, STATUS_BLOCK, GENERIC_BOOKING):
        rest = pattern.sub(" ", rest)
    return all(word in FOLLOW_UP_FILLER for word in re.findall(r"[a-z']+", rest))


def parse(body: str, today: date, has_context: bool) -> ParsedQuestion | None:
    """Return the question, or a follow-up when ``has_context``; None if neither."""
    text = re.sub(r"\s+", " ", normalized(body).replace("?", " ")).strip()
    if not text:
        return None
    ranges = set(_ranges(text, today))
    first, last = next(iter(ranges)) if len(ranges) == 1 else (None, None)
    multiple = len(ranges) > 1
    if TRIGGER.search(text) and SUBJECT.search(text):
        view = _view_of(text)
        return ParsedQuestion(True, view, first, last, _statuses(text, view), multiple)
    if not has_context or len(text.split()) > MAX_FOLLOW_UP_WORDS:
        return None
    if MORE.fullmatch(text):
        return ParsedQuestion(False, None, None, None, None, more=True)
    if not _follow_up_only(text):
        return None
    wants_count = "how many" in text
    wants_clients = bool(re.search(r"\b(?:who|names?|clients?)\b", text))
    named = _named_statuses(text)
    if not (ranges or named or wants_count or wants_clients or BREAKDOWN.search(text)):
        return None
    follow_view: View | None = (
        View.COUNT if wants_count else View.CLIENTS if wants_clients
        else View.SUMMARY if BREAKDOWN.search(text) else None)
    return ParsedQuestion(False, follow_view, first, last, named, multiple)


# --- Answering ---------------------------------------------------------------

@dataclass(frozen=True)
class _Item:
    event: CalendarEvent
    name: str | None


class OwnerCalendarQuestions:
    """Verified, read-only calendar question types for the owner's SMS thread."""

    def __init__(self, repository: QuestionRepository,
                 contexts: QuestionContextStore | None = None) -> None:
        self._repository = repository
        self._contexts = contexts if contexts is not None else InMemoryQuestionContexts()

    def awaiting_answer(self, business_id: str, sender: str, now: datetime) -> bool:
        """True while a clarifying question is open, so a bare reply is its answer."""
        context = self._contexts.read_context(business_id, sender)
        return context is not None and context.ask is not None and not context.expired(now)

    def open_context(self, business_id: str, sender: str,
                     now: datetime) -> QuestionContext | None:
        """The owner's unexpired calendar conversation, if any."""
        context = self._contexts.read_context(business_id, sender)
        return context if context is not None and not context.expired(now) else None

    def is_fresh_question(self, body: str, today: date) -> bool:
        parsed = parse(body, today, False)
        return parsed is not None and parsed.fresh

    def read_clarification(self, business_id: str, sender: str,
                           now: datetime) -> QuestionContext | None:
        """The stored record of the last clarifying question about a request, if it has not
        lapsed on its own. It outlives the calendar conversation it was asked in."""
        context = self._contexts.read_context(business_id, sender)
        if (context is None or not context.clarified_request or context.clarified_at is None
                or now >= context.clarified_at + CLARIFICATION_RETENTION):
            return None
        return context

    def mark_clarified(self, business_id: str, sender: str, now: datetime,
                       request_id: str, version: int, receipt_id: str) -> None:
        """Remember the request the owner was just asked about, with or without an open
        calendar conversation (an expired or missing one is kept closed)."""
        context = self._contexts.read_context(business_id, sender)
        if context is None:
            context = QuestionContext(business_id, sender, View.SUMMARY, None, None, None, 0,
                                      None, now, now, answered_at=now)
        self._contexts.put_context(replace(
            context, clarified_request=request_id, clarified_version=version,
            clarified_by=receipt_id, clarified_at=now))

    def ask_range(self, business_id: str, sender: str, now: datetime) -> str:
        """Ask which day or week, so a bare "next week" answers it."""
        stored = self._contexts.read_context(business_id, sender)
        draft = QuestionContext(business_id, sender, View.SUMMARY, None, None, None, 0,
                                Ask.RANGE, now, now + ASK_LIFETIME, answered_at=now)
        if stored is not None:  # Keep any open clarifying question about a request.
            draft = replace(draft, clarified_request=stored.clarified_request,
                            clarified_version=stored.clarified_version,
                            clarified_by=stored.clarified_by, clarified_at=stored.clarified_at)
        self._contexts.put_context(draft)
        return RANGE_QUESTION

    def apply_followup(self, business_id: str, sender: str, now: datetime, receipt_id: str,
                       statuses: frozenset[CalendarStatus] | None,
                       first: date | None, last: date | None) -> CalendarAnswer | None:
        """Re-answer the open question with validated, model-proposed changes."""
        context = self.open_context(business_id, sender, now)
        if context is None or (first is None) != (last is None) or (
                first is None and statuses is None):
            return None  # A half-stated range is a question, not a reason to reuse the old one.
        if first is not None and last is not None:
            if last < first or (last - first).days >= MAX_FOLLOW_UP_DAYS:
                return None
        else:
            first, last = context.first, context.last
        statuses = statuses if statuses is not None else context.statuses
        if first is None or last is None or statuses is None:
            return None
        policy = self._repository.read_policy(business_id)
        draft = replace(context, first=first, last=last, statuses=statuses, ask=None)
        return self._reply(draft, now, policy, ZoneInfo(policy.timezone), 0, receipt_id)

    def answer(self, business_id: str, sender: str, body: str, now: datetime,
               receipt_id: str = "") -> str | None:
        """Reply text for a calendar question, or None if the text is not one."""
        answered = self.answer_detailed(business_id, sender, body, now, receipt_id)
        return answered.text if answered is not None else None

    def answer_detailed(self, business_id: str, sender: str, body: str, now: datetime,
                        receipt_id: str = "") -> CalendarAnswer | None:
        """Like ``answer``, with the parts of a rendered page that may be reworded."""
        policy = self._repository.read_policy(business_id)
        zone = ZoneInfo(policy.timezone)
        today = now.astimezone(zone).date()
        stored = self._contexts.read_context(business_id, sender)
        context = stored if stored is not None and not stored.expired(now) else None
        parsed = parse(body, today, context is not None)
        if parsed is None:
            return None
        if parsed.multiple_ranges:
            return CalendarAnswer(
                "I can answer one day or one week at a time. Which do you want: "
                "tomorrow, a weekday like Friday, this week, next week, or a date?")
        if parsed.more:
            assert context is not None
            if context.ask is not None or context.first is None or context.last is None:
                return None
            # A redelivered MORE repeats its page instead of advancing past it.
            retry = bool(receipt_id) and context.receipt_id == receipt_id
            if context.skip == 0 and not retry:
                return CalendarAnswer(
                    f"That was everything for {_span_text(context.first, context.last)}; "
                    "nothing more to show.")
            return self._reply(context, now, policy, zone,
                               context.page_start if retry else context.skip, receipt_id,
                               resume=True)
        base = context if not parsed.fresh else None
        view = parsed.view or (base.view if base is not None else View.SUMMARY)
        first, last = parsed.first, parsed.last
        statuses = parsed.statuses
        if first is None and context is not None and context.first is not None and (
                base is not None or len(body.split()) <= MAX_FOLLOW_UP_WORDS):
            first, last = context.first, context.last  # A follow-up keeps the range.
        if statuses is None and base is not None:
            statuses = base.statuses
        draft = QuestionContext(business_id, sender, view, first, last, statuses, 0, None,
                                now, now + QUESTION_LIFETIME)
        if stored is not None:  # A clarifying question about a request outlives the answer.
            draft = replace(draft, clarified_request=stored.clarified_request,
                            clarified_version=stored.clarified_version,
                            clarified_by=stored.clarified_by, clarified_at=stored.clarified_at)
        if first is None or last is None:
            self._contexts.put_context(replace(
                draft, ask=Ask.RANGE, expires_at=now + ASK_LIFETIME, answered_at=now))
            return CalendarAnswer(RANGE_QUESTION)
        if statuses is None:
            self._contexts.put_context(replace(
                draft, ask=Ask.STATUS, expires_at=now + ASK_LIFETIME, answered_at=now))
            return CalendarAnswer(
                f"For {_span_text(first, last)}, should I count confirmed visits only, "
                "pending requests only, or both? Reply confirmed, pending, or both.")
        return self._reply(draft, now, policy, zone, 0, receipt_id)

    def _reply(self, context: QuestionContext, now: datetime, policy: AvailabilityPolicy,
               zone: ZoneInfo, skip: int, receipt_id: str,
               resume: bool = False) -> CalendarAnswer:
        assert context.first is not None and context.last is not None
        days = _days(context.first, context.last)
        items = self._items(context.business_id, days, zone, now)
        header, entries, model_entries = _render(context, days, items, zone, policy.timezone)
        fingerprint = sha256("\n".join(entries).encode()).hexdigest()
        if resume and fingerprint != context.fingerprint:
            # Offsets into the old list could skip or repeat entries, so start over.
            skip = 0
            header = f"The calendar changed, so this is the updated list from the start. {header}"
        text, footer, shown = _page(header, entries, skip)
        following = skip + shown
        self._contexts.put_context(replace(
            context, skip=following if following < len(entries) else 0, ask=None,
            expires_at=now + QUESTION_LIFETIME, page_start=skip, receipt_id=receipt_id,
            fingerprint=fingerprint, answered_at=now))
        return CalendarAnswer(text + footer, text, footer,
                              "\n".join([header, *model_entries[skip:skip + shown]]))

    def _items(self, business_id: str, days: list[date], zone: ZoneInfo,
               now: datetime) -> dict[date, list[_Item]]:
        window_start = datetime.combine(days[0], time.min, tzinfo=zone).astimezone(UTC)
        window_end = datetime.combine(days[-1] + timedelta(days=1), time.min,
                                      tzinfo=zone).astimezone(UTC)
        events = sorted(
            (event for event in self._repository.read_calendar(business_id).events
             if event.status in ALL_STATUSES and event.occupies_time(now)
             and event.start_at < window_end and window_start < event.end_at),
            key=lambda event: (event.start_at, event.event_id))
        names: dict[str, str | None] = {}
        for event in events:
            if event.status == BLOCK:
                continue
            appointment = self._repository.read_appointment(event.event_id)
            profile = (self._repository.read_profile(business_id, appointment.client_id)
                       if appointment is not None else None)
            names[event.event_id] = profile.name if profile is not None else None
        by_day: dict[date, list[_Item]] = {day: [] for day in days}
        for day in days:
            day_start = datetime.combine(day, time.min, tzinfo=zone)
            day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
            by_day[day] = [_Item(event, names.get(event.event_id)) for event in events
                           if event.start_at < day_end and day_start < event.end_at]
        return by_day


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _span_text(first: date, last: date) -> str:
    if first == last:
        return f"{day_text(first)}, {first.year}"
    return f"{day_text(first)} to {day_text(last)}, {last.year}"


def _plural(count: int, status: CalendarStatus) -> str:
    return f"{count} {NOUNS[status][0 if count == 1 else 1]}"


def _label(item: _Item, first_name_only: bool = False) -> str:
    event = item.event
    if event.status == BLOCK:
        return "unavailable block"
    name = item.name if item.name is not None else "client record not found"
    if first_name_only and item.name is not None and item.name.split():
        name = item.name.split()[0]  # What a model sees: no more than the owner context sends.
    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH - 3] + "..."
    if event.status == PENDING:
        return f"{name} (pending, ref {event.event_id[:8]})"
    return f"{name} (confirmed)"


def _clock(instant: datetime, zone: ZoneInfo, tag: bool) -> str:
    """Clock time; on a daylight-saving change day also the zone, since a time can repeat."""
    text = clock_text(instant, zone)
    return f"{text} {instant.astimezone(zone).strftime('%Z')}" if tag else text


def _item_line(day: date, item: _Item, zone: ZoneInfo, first_name_only: bool = False) -> str:
    event = item.event
    start, end = event.start_at.astimezone(zone), event.end_at.astimezone(zone)
    midnight = datetime.combine(day, time.min, tzinfo=zone)
    following = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    tag = midnight.utcoffset() != following.utcoffset()
    if start.date() != day:
        span = f"until {_clock(event.end_at, zone, tag)}"
    elif end.date() != day:
        span = (f"from {_clock(event.start_at, zone, tag)} to {day_text(end.date())} "
                f"{clock_text(event.end_at, zone)}")
    else:
        span = f"{_clock(event.start_at, zone, tag)}-{_clock(event.end_at, zone, tag)}"
    return f"{day_text(day)}, {span}: {_label(item, first_name_only)}"


def _tally(items: list[_Item]) -> dict[CalendarStatus, int]:
    return {status: sum(1 for item in items if item.event.status == status)
            for status in ORDER}


def _render(context: QuestionContext, days: list[date], items: dict[date, list[_Item]],
            zone: ZoneInfo, zone_name: str) -> tuple[str, list[str], list[str]]:
    """The header, the entries, and the same entries with first names only (parallel)."""
    assert context.first is not None and context.last is not None
    assert context.statuses is not None
    wanted = context.statuses
    if context.view == View.CLIENTS:
        wanted = wanted - {BLOCK}
    span = f"{_span_text(context.first, context.last)} ({zone_name})"
    unique = {item.event.event_id: item for day in days for item in items[day]}
    totals = _tally(list(unique.values()))
    entries: list[str] = []
    if context.view == View.COUNT:
        counted = ", ".join(_plural(totals[s], s) for s in ORDER if s in wanted)
        header = f"{span}: {counted} counted."
        others = [_plural(totals[s], s) for s in ORDER if s not in wanted and totals[s]]
        if others:
            header += f" Not counted: {', '.join(others)}."
        if len(days) > 1:
            for day in days:
                tally = _tally(items[day])
                parts = ", ".join(_plural(tally[s], s) for s in ORDER if s in wanted)
                entries.append(f"{day_text(day)}: {parts}")
        return header, entries, list(entries)
    present = ", ".join(_plural(totals[s], s) for s in ORDER if s in wanted and totals[s])
    kinds = " or ".join(NOUNS[s][1] for s in ORDER if s in wanted)
    header = f"{span}: {present}." if present else f"{span}: no {kinds}."
    model_entries: list[str] = []
    for day in days:
        shown = [item for item in items[day] if item.event.status in wanted]
        if shown:
            entries.extend(_item_line(day, item, zone) for item in shown)
            model_entries.extend(_item_line(day, item, zone, True) for item in shown)
        elif len(days) > 1:
            entries.append(f"{day_text(day)}: nothing scheduled")
            model_entries.append(entries[-1])
    return header, entries, model_entries


def _page(header: str, entries: list[str], skip: int) -> tuple[str, str, int]:
    """Fit whole entries within the SMS limit and say what remains; never cut one.

    Returns the page, its paging footer (empty when everything fits), and the entries shown.
    """
    text = header
    shown = 0
    for entry in entries[skip:]:
        candidate = f"{text}\n{entry}"
        if len(candidate) > MAX_REPLY_LENGTH - FOOTER_ROOM and shown:
            break
        text = candidate
        shown += 1
    last = skip + shown
    footer = ""
    if entries and (last < len(entries) or skip):
        footer = f"\nShowing {skip + 1}-{last} of {len(entries)}."
        if last < len(entries):
            footer += " Reply MORE for the rest."
    return text, footer, shown
