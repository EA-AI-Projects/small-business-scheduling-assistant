"""Bounded tools for a verified client's SMS model loop (#340, #341, #342).

The backend builds every tool from the verified receipt: the business and client
are fixed when the toolset is created, and no argument can name another. Results
are structured, bounded, and read fresh for the message. The three read tools create
no hold, appointment, cancellation, or outbox row. ``get_availability`` stores the
short-lived offer a later reply may accept, which is the same offer memory the
deterministic path writes. ``request_booking`` is the one write (#341): it names a
local date and time, and the conversation service books it only if it is exactly one
time in this client's own earlier, unexpired offer. It never books a time offered in the
same message.

Cancellation (#342) is two steps over two messages. ``prepare_cancellation`` names one of
this client's own visits by reference and stores the time-limited confirmation question
that binds that visit and its version; it cancels nothing. On a later message
``confirm_cancellation`` acts only on that stored question (the one cancel write, rechecked
and receipt-keyed by the service), and ``dismiss_cancellation`` closes it. None of the three
can name a visit or version of its own. Only one action tool takes effect per message.

``check_reply`` compares the model's final text with what the tools returned
before the text can be sent.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import (
    AvailabilityPolicy,
    HolidayCalendar,
    InvalidDuration,
    InvalidPolicy,
    LocalWindow,
    available_starts,
)
from scheduling.domain.business_profile import BusinessProfileRecord
from scheduling.domain.calendar import CalendarSnapshot, CalendarStatus
from scheduling.domain.client_replies import DATE, REF, TIME, gsm_septets
from scheduling.domain.conversation_state import (
    MIN_OPTIONS,
    PROMPT_LIFETIME,
    clock_text,
    spread,
    when_text,
)
from scheduling.domain.holidays import observed_us_federal_holidays
from scheduling.domain.service_duration import ServiceNotConfigured, service_duration

TOOL_NAMES = ("get_business_info", "get_availability", "get_my_bookings", "request_booking",
              "prepare_cancellation", "confirm_cancellation", "dismiss_cancellation")
MAX_TOOL_CALLS = 3
MAX_RANGE_DAYS = 14
MAX_REPLY_SEPTETS = 480
MAX_OFFER_OPTIONS = 5
RESERVED = "reserved_by_owner"
INVALID = {"ok": False, "error": "invalid_arguments"}
UNAVAILABLE = {"ok": False, "error": "unavailable"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
WRITE_CLAIM = re.compile(
    r"\b(?:i|we)(?:'ve|\s+have)?\s+(?:just\s+)?(?:booked|cancel+ed|rescheduled|scheduled|"
    r"reserved|requested|submitted|moved|confirmed|approved|put|added|locked|held|arranged|"
    r"penciled|pencilled|set\s+up|taken\s+care)\b"
    r"|\b(?:has|have)\s+been\s+(?:booked|cancel+ed|rescheduled|scheduled|requested|"
    r"submitted|approved)\b"
    r"|\byou(?:'re|\s+are)\s+(?:all\s+set|booked|scheduled)\b"
    r"|\bis\s+now\s+(?:booked|confirmed|scheduled|cancel+ed)\b"
    # Another client's booking is never disclosed, even as a reason.
    r"|\b(?:another|other)\s+(?:clients?|customers?)\b|\bsomeone\s+else\b"
    r"|\bbooked\s+by\b", re.IGNORECASE)
# A booking request is pending owner approval, so these claims stay banned even after one.
CONFIRM_CLAIM = re.compile(
    r"\b(?:i|we)(?:'ve|\s+have)?\s+(?:just\s+)?(?:booked|cancel+ed|rescheduled|scheduled|"
    r"reserved|moved|confirmed|approved|put|added|locked|held|arranged|penciled|pencilled|"
    r"set\s+up|taken\s+care)\b"
    r"|\b(?:has|have)\s+been\s+(?:booked|cancel+ed|rescheduled|scheduled|approved)\b"
    r"|\byou(?:'re|\s+are)\s+(?:all\s+set|booked|scheduled)\b"
    r"|\bis\s+now\s+(?:booked|confirmed|scheduled|cancel+ed)\b"
    r"|\b(?:another|other)\s+(?:clients?|customers?)\b|\bsomeone\s+else\b"
    r"|\bbooked\s+by\b", re.IGNORECASE)
NOT_CONFIRMED = re.compile(r"\b(?:not|isn't|aren't|n't)\s+(?:yet\s+)?confirmed\b", re.IGNORECASE)
NOT_BOOKED = re.compile(r"\b(?:nothing\s+(?:was|is|has\s+been)|not|isn't|wasn't|n't)\s+"
                        r"(?:yet\s+)?booked\b", re.IGNORECASE)
# A cancellation is claimed only after confirm_cancellation made one; negations are not claims.
CANCELLED = re.compile(r"\bcancel+ed\b", re.IGNORECASE)
NOT_CANCELLED = re.compile(
    r"\b(?:nothing\s+(?:was|is|has\s+been)|not|isn't|wasn't|n't|never|yet)\s+(?:yet\s+)?"
    r"(?:been\s+)?cancel+ed\b", re.IGNORECASE)
NOTHING_CHANGED = re.compile(r"\bnothing\s+(?:was\s+|has\s+been\s+)?changed\b|\bunchanged\b",
                             re.IGNORECASE)
STILL_BOOKED = re.compile(r"\b(?:still|stays?|remains?)\s+(?:booked|scheduled|confirmed)\b"
                          r"|\bkept\b", re.IGNORECASE)
REF_ARG = re.compile(r"[0-9A-Za-z-]{4,40}")
READ_ERRORS = (OSError, ValueError, TypeError, KeyError, RuntimeError)


@dataclass(frozen=True)
class CancellationHooks:
    """The conversation service's cancellation steps for this one message (#342)."""

    prepare: Callable[[str], dict[str, Any]]
    confirm: Callable[[], dict[str, Any]]
    dismiss: Callable[[], dict[str, Any]]


class ToolRepository(Protocol):
    """What the tools read. The conversation repository satisfies it."""

    def read_policy(self, business_id: str) -> AvailabilityPolicy: ...
    def read_business_profile(self, business_id: str) -> BusinessProfileRecord: ...
    def read_calendar(self, business_id: str) -> CalendarSnapshot: ...


def _clock(value: time) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _window(window: LocalWindow) -> str:
    return f"{_clock(window.opens)}-{_clock(window.closes)}"


def _day(day: date) -> dict[str, str]:
    return {"date": day.isoformat(), "weekday": day.strftime("%A")}


def _day_arg(value: object) -> date | None:
    if not isinstance(value, str) or ISO_DAY.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _clock_arg(value: object) -> time | None:
    if not isinstance(value, str) or CLOCK.fullmatch(value) is None:
        return None
    return time.fromisoformat(value)


class ClientReadTools:
    """One message's read tools for one verified client. Not reusable across messages."""

    def __init__(self, repository: ToolRepository, business_id: str, now: datetime,
                 policy: AvailabilityPolicy, own_visits: tuple[Appointment, ...],
                 store_offer: Callable[[tuple[datetime, ...], int], None],
                 request: Callable[[date, time], dict[str, Any]] | None = None,
                 cancellation: CancellationHooks | None = None) -> None:
        self._repository = repository
        self._business_id = business_id
        self._now = now
        self._policy = policy
        self._zone = ZoneInfo(policy.timezone)
        self._visits = own_visits
        self._store_offer = store_offer
        self._request = request
        self._cancellation = cancellation
        self.requested: dict[str, Any] | None = None
        self.prepared: dict[str, Any] | None = None  # A cancellation question asked now.
        self.answered = False  # confirm_cancellation or dismiss_cancellation ran.
        self.results: list[dict[str, Any]] = []
        self.offer: tuple[datetime, ...] = ()
        self.calls = 0

    def __call__(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            return {"ok": False, "error": "unknown_tool"}
        self.calls += 1
        if self.calls > MAX_TOOL_CALLS:
            return {"ok": False, "error": "tool_limit"}
        try:
            result = getattr(self, name)(args)
        except READ_ERRORS:
            result = dict(UNAVAILABLE)
        self.results.append(result)
        return result

    # -- get_business_info -------------------------------------------------

    def get_business_info(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            return dict(INVALID)
        policy, zone = self._policy, self._zone
        record = self._repository.read_business_profile(self._business_id)
        profile = record.profile
        today = self._now.astimezone(zone).date()
        last = today + timedelta(days=policy.booking_horizon_days)
        closures: list[dict[str, str]] = []
        special: list[dict[str, Any]] = []
        for offset in range((last - today).days + 1):
            day = today + timedelta(days=offset)
            if day in policy.date_exceptions:
                windows = policy.date_exceptions[day]
                if windows:
                    special.append({**_day(day), "hours": [_window(w) for w in windows]})
                else:
                    closures.append({**_day(day), "reason": "closed"})
            elif (policy.holiday_calendar == HolidayCalendar.US_FEDERAL
                  and day in observed_us_federal_holidays(day.year)
                  and policy.weekly_windows.get(day.weekday())):
                closures.append({**_day(day), "reason": "holiday"})
        return {
            "ok": True, "configured": profile is not None,
            "business": None if profile is None else {
                "name": profile.display_name, "description": profile.description,
                "support_contact": profile.support_contact},
            "service": None if profile is None else {
                "name": profile.service_name, "description": profile.service_description,
                "duration_minutes": profile.default_duration_minutes},
            "hours": {
                "timezone": policy.timezone,
                "weekly": [{"weekday": WEEKDAYS[number],
                            "hours": [_window(w) for w in policy.weekly_windows.get(number, ())]}
                           for number in range(7)],
                "closures_in_booking_range": closures,
                "special_hours_in_booking_range": special,
            },
            "booking": {
                "today": today.isoformat(), "weekday": today.strftime("%A"),
                "local_time": clock_text(self._now, zone),
                "booking_horizon_days": policy.booking_horizon_days,
                "bookable_through": last.isoformat(),
                "owner_approval_required": True,
                "offer_valid_minutes": int(PROMPT_LIFETIME.total_seconds() // 60)},
        }

    # -- get_availability --------------------------------------------------

    def get_availability(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"from", "to", "time_from", "time_to"}:
            return dict(INVALID)
        first, last = _day_arg(args["from"]), _day_arg(args["to"])
        earliest = _clock_arg(args["time_from"]) if args["time_from"] is not None else None
        latest = _clock_arg(args["time_to"]) if args["time_to"] is not None else None
        if (first is None or last is None or last < first
                or (last - first).days >= MAX_RANGE_DAYS
                or (args["time_from"] is not None and earliest is None)
                or (args["time_to"] is not None and latest is None)
                or (earliest is not None and latest is not None and latest < earliest)):
            return dict(INVALID)
        if self.offer:
            return {"ok": False, "error": "offer_already_stored"}
        if self.requested is not None:
            return {"ok": False, "error": "already_requested"}
        if self.prepared is not None or self.answered:
            return {"ok": False, "error": "cancellation_in_progress"}
        if (earliest is None) != (latest is None):  # "After 2" or "before noon".
            earliest, latest = earliest or time(0), latest or time(23, 59)
        policy, zone, now = self._policy, self._zone, self._now
        try:
            minutes = service_duration(self._repository, self._business_id, policy)
        except (ServiceNotConfigured, InvalidDuration):
            return {"ok": False, "error": "service_not_configured"}
        events = self._repository.read_calendar(self._business_id).events
        today = now.astimezone(zone).date()
        horizon = today + timedelta(days=policy.booking_horizon_days)
        starts: list[datetime] = []
        days: list[dict[str, Any]] = []
        try:
            for offset in range((last - first).days + 1):
                day = first + timedelta(days=offset)
                found = available_starts(policy, day, minutes, events, now)
                starts.extend(found)
                days.append({**_day(day), "open": bool(found),
                             "reason": None if found else self._reason(
                                 day, today, horizon, minutes)})
        except (InvalidDuration, InvalidPolicy):
            return {"ok": False, "error": "availability_unavailable"}
        chosen, matched = self._choose(tuple(sorted(starts)), earliest, latest)
        if chosen:
            self._store_offer(chosen, minutes)
            self.offer = chosen
        return {
            "ok": True, "timezone": policy.timezone, "duration_minutes": minutes,
            "from": first.isoformat(), "to": last.isoformat(), "days": days,
            "requested_time_matched": matched,
            "offer": None if not chosen else {
                "valid_minutes": int(PROMPT_LIFETIME.total_seconds() // 60),
                "owner_approval_required": True,
                "options": [{"number": number, **_day(start.astimezone(zone).date()),
                             "time": clock_text(start, zone),
                             "when": when_text(start, zone)}
                            for number, start in enumerate(chosen, 1)]},
            "more_open_times_than_offered": len(starts) > len(chosen),
        }

    def _choose(self, starts: tuple[datetime, ...], earliest: time | None,
                latest: time | None) -> tuple[tuple[datetime, ...], bool]:
        zone = self._zone
        if not starts:
            return (), True
        if earliest is None or latest is None:
            return spread(starts, zone, MAX_OFFER_OPTIONS), True
        wall = {start: start.astimezone(zone).time() for start in starts}
        if earliest == latest:
            exact = tuple(start for start in starts if wall[start] == earliest)
            if exact:
                return spread(exact, zone, MAX_OFFER_OPTIONS), True
            target = earliest.hour * 60 + earliest.minute
            nearest = sorted(starts, key=lambda start: (
                abs(wall[start].hour * 60 + wall[start].minute - target), start))
            return tuple(sorted(nearest[:MIN_OPTIONS])), False
        window = tuple(start for start in starts if earliest <= wall[start] <= latest)
        return spread(window or starts, zone, MAX_OFFER_OPTIONS), bool(window)

    def _reason(self, day: date, today: date, horizon: date, minutes: int) -> str:
        """The truthful reason a day has no bookable start.

        Occupied calendar time is always "reserved by owner": it may be another client's
        visit or a private block, and neither is disclosed.
        """
        policy = self._policy
        if day < today:
            return "date_passed"
        if day > horizon:
            return "beyond_booking_horizon"
        if day in policy.date_exceptions:
            if not policy.date_exceptions[day]:
                return "closed"
        elif (policy.holiday_calendar == HolidayCalendar.US_FEDERAL
              and day in observed_us_federal_holidays(day.year)):
            return "holiday"
        elif not policy.weekly_windows.get(day.weekday()):
            return "outside_business_hours"
        if available_starts(policy, day, minutes, (), self._now):
            return RESERVED
        return "no_time_left_in_business_hours"

    # -- request_booking ---------------------------------------------------

    def request_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        """Ask the service to book one time from this client's earlier, unexpired offer.

        The arguments only name a local date and time. Whether that is exactly one time of
        this client's own stored offer, and every other check, is the service's decision.
        """
        if set(args) != {"date", "time"}:
            return dict(INVALID)
        day, clock = _day_arg(args["date"]), _clock_arg(args["time"])
        if day is None or clock is None:
            return dict(INVALID)
        if self._request is None:
            return dict(UNAVAILABLE)
        if self.offer:
            # An offer made in this same message has not been sent to the client yet.
            return {"ok": False, "error": "offer_not_sent_yet"}
        if self.requested is not None:
            return {"ok": False, "error": "already_requested"}
        if self.prepared is not None or self.answered:
            return {"ok": False, "error": "cancellation_in_progress"}
        result = self._request(day, clock)
        if result.get("requested") is True:
            self.requested = result
        return result

    # -- cancellation (#342) ------------------------------------------------

    def _other_action(self) -> str | None:
        """Why no cancellation step may run now: another action already took effect."""
        if self._cancellation is None:
            return "unavailable"
        if self.offer:
            return "offer_not_sent_yet"
        if self.requested is not None:
            return "already_requested"
        if self.prepared is not None:
            return "cancellation_question_not_sent_yet"
        if self.answered:
            return "already_answered"
        return None

    def prepare_cancellation(self, args: dict[str, Any]) -> dict[str, Any]:
        """Ask the service to store a cancellation question for one of this client's visits."""
        ref = args.get("ref")
        if set(args) != {"ref"} or not isinstance(ref, str) or REF_ARG.fullmatch(ref) is None:
            return dict(INVALID)
        if (why := self._other_action()) is not None:
            return {"ok": False, "error": why}
        assert self._cancellation is not None
        result = self._cancellation.prepare(ref)
        if result.get("prepared") is True:
            self.prepared = result
        return result

    def confirm_cancellation(self, args: dict[str, Any]) -> dict[str, Any]:
        """Ask the service to cancel the visit of this client's open cancellation question."""
        if args:
            return dict(INVALID)
        if (why := self._other_action()) is not None:
            return {"ok": False, "error": why}
        assert self._cancellation is not None
        self.answered = True  # The service decides once; a second call cannot try again.
        return self._cancellation.confirm()

    def dismiss_cancellation(self, args: dict[str, Any]) -> dict[str, Any]:
        """Ask the service to close this client's open cancellation question."""
        if args:
            return dict(INVALID)
        if (why := self._other_action()) is not None:
            return {"ok": False, "error": why}
        assert self._cancellation is not None
        self.answered = True
        return self._cancellation.dismiss()

    # -- get_my_bookings ---------------------------------------------------

    def get_my_bookings(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"from", "to"}:
            return dict(INVALID)
        first = _day_arg(args["from"]) if args["from"] is not None else None
        last = _day_arg(args["to"]) if args["to"] is not None else None
        if ((args["from"] is not None and first is None)
                or (args["to"] is not None and last is None)
                or (first is not None and last is not None
                    and (last < first or (last - first).days >= 62))):
            return dict(INVALID)
        zone = self._zone
        shown = []
        for visit in self._visits:
            local = visit.start_at.astimezone(zone)
            if (first is not None and local.date() < first) or (
                    last is not None and local.date() > last):
                continue
            confirmed = visit.status == CalendarStatus.CONFIRMED
            shown.append({
                "ref": visit.appointment_id[:8],
                "status": "confirmed" if confirmed else "pending_owner_approval",
                "confirmed": confirmed, **_day(local.date()),
                "time": clock_text(visit.start_at, zone),
                "end_time": clock_text(visit.end_at, zone),
                "when": when_text(visit.start_at, zone), "timezone": local.strftime("%Z")})
        return {
            "ok": True, "timezone": self._policy.timezone,
            "from": first.isoformat() if first else None,
            "to": last.isoformat() if last else None,
            "confirmed_count": sum(item["confirmed"] is True for item in shown),
            "pending_count": sum(item["confirmed"] is False for item in shown),
            "bookings": shown,
        }


# -- final reply check -----------------------------------------------------

def _month_day(text: str) -> tuple[int, int] | None:
    iso = re.search(r"\d{4}-(\d{2})-(\d{2})", text)
    if iso:
        return int(iso[1]), int(iso[2])
    slash = re.match(r"(\d{1,2})/(\d{1,2})", text)
    if slash:
        return int(slash[1]), int(slash[2])
    named = re.search(r"([a-z]{3})[a-z]*\s+(\d{1,2})", text.lower())
    if named and named[1] in MONTHS:
        return MONTHS[named[1]], int(named[2])
    return None


def _hour_minute(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text.strip().lower())
    if match is None:
        return None
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), match[3]
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return hour, minute


def _facts(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]], list[str]]:
    """Month-day pairs, hour-minute pairs, and eight-character references, in text order."""
    # A date's year ("2026-10-16") must not read as a time, so times are taken from the
    # text with dates removed.
    days = [pair for match in DATE.finditer(text) if (pair := _month_day(match.group()))]
    bare = DATE.sub(" ", text)
    times = [pair for match in TIME.finditer(bare) if (pair := _hour_minute(match.group()))]
    refs = [match.group().lower()[:8] for match in REF.finditer(text)]
    return days, times, refs


MARKER = re.compile(r"(?<!\d)(\d)\s*\)")


def _events(text: str) -> list[tuple[str, Any]]:
    """Dates, option markers, and clock times of the text, in the order they are read."""
    found: list[tuple[int, str, Any]] = []
    for match in DATE.finditer(text):
        if pair := _month_day(match.group()):
            found.append((match.start(), "date", pair))
    blanked = DATE.sub(lambda match: " " * len(match.group()), text)
    for match in TIME.finditer(blanked):
        if pair := _hour_minute(match.group()):
            found.append((match.start(), "time", pair))
    for match in MARKER.finditer(blanked):
        found.append((match.start(), "marker", int(match[1])))
    return [(kind, value) for _, kind, value in sorted(found, key=lambda item: item[0])]


def _offer_in_order(text: str, starts: list[datetime]) -> bool:
    """Each offered time is read in order, under its own date and (if several) its own number.

    Option i's time must follow option i-1's, be introduced by marker ``i)`` (with only a
    date between them), and sit under the most recent date before it, which is option i's.
    """
    events = _events(text)
    position = 0
    for number, start in enumerate(starts, 1):
        want_date, want_time = (start.month, start.day), (start.hour, start.minute)
        for index in range(position, len(events)):
            kind, value = events[index]
            if kind != "time" or value != want_time:
                continue
            before = [event for event in events[:index] if event[0] != "date"]
            dates = [event[1] for event in events[:index] if event[0] == "date"]
            if len(starts) > 1 and (not before or before[-1] != ("marker", number)):
                continue
            if not dates or dates[-1] != want_date:
                continue
            position = index + 1
            break
        else:
            return False
    return True


def _authorized(results: list[dict[str, Any]]) -> tuple[set[tuple[int, int]],
                                                       set[tuple[int, int]], set[str]]:
    days: set[tuple[int, int]] = set()
    times: set[tuple[int, int]] = set()
    refs: set[str] = set()
    for result in results:
        if result.get("ok") is not True:
            continue
        stack: list[Any] = [result]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str):
                found_days, found_times, found_refs = _facts(item)
                days.update(found_days)
                times.update(found_times)
                refs.update(found_refs)
                if (parsed := _day_arg(item)) is not None:
                    days.add((parsed.month, parsed.day))
    return days, times, refs


def _cancellation_problem(text: str, results: list[dict[str, Any]]) -> str | None:
    """Why the text misstates the cancellation tools' outcome (#342), or None.

    A cancellation is claimed only when confirm_cancellation made one, and then with its
    reference. A question must ask for YES and name the visit time. Every other outcome must
    say that nothing was cancelled; only a dismissed question may say the visit stays booked.
    """
    ok = [result for result in results if result.get("ok") is True]
    done = [result for result in ok if result.get("cancelled") is True]
    asked = [result for result in ok if result.get("prepared") is True]
    kept = [result for result in ok if result.get("dismissed") is True]
    refused = [result for result in ok if result.get("cancelled") is False
               or result.get("prepared") is False or result.get("dismissed") is False]
    unclaimed = NOT_CANCELLED.sub("", text)
    claimed = CANCELLED.search(unclaimed) is not None
    lowered = text.lower()
    if done:
        if not claimed:
            return "Say the visit was cancelled, with its date, time, and reference."
        if str(done[0]["ref"]).lower() not in lowered:
            return "Include the visit reference from the tool result."
        if STILL_BOOKED.search(text):
            return "The visit was cancelled; do not say it is still booked."
        return None
    if claimed:
        return "The SMS claims a cancellation; no tool made one."
    if asked:
        shown = _hour_minute(str(asked[0]["time"]))
        if "yes" not in lowered or "?" not in text or shown not in _facts(text)[1]:
            return ("Ask the client to reply YES to cancel, naming the visit's date and time; "
                    "nothing is cancelled until they do.")
    elif kept or refused:
        if NOT_CANCELLED.search(text) is None and NOTHING_CHANGED.search(text) is None:
            return "Say that nothing was cancelled."
        if not kept and STILL_BOOKED.search(text):
            return "The tool did not show the visit still booked; say only that nothing changed."
    return None


def check_reply(text: str, results: list[dict[str, Any]],
                offer: tuple[datetime, ...], zone: ZoneInfo) -> str | None:
    """Why the final text may not be sent, or None. Compared with the tool outcomes only.

    Every date, clock time, and reference the text states must come from a successful tool
    result of this message. The text may not claim a booking, request, change, or
    cancellation, because no read tool makes one. After a ``request_booking`` that created a
    request, it must be called pending owner approval with its reference and never
    confirmed; after one that did not, it must say nothing was booked. A stored offer must be listed whole, in
    order, numbered, and described as pending owner approval, so that a later number or time
    reply means what the client read.
    """
    size = gsm_septets(text, True)
    if not text.strip() or size is None or size > MAX_REPLY_SEPTETS:
        return "The SMS is empty, over 480 GSM-7 characters, or not GSM-7."
    outcomes = [result for result in results
                if result.get("ok") is True and "requested" in result]
    requested = [result for result in outcomes if result["requested"] is True]
    if requested:
        # A pending request may be called a request, never a confirmed booking.
        unclaimed = NOT_BOOKED.sub("", NOT_CONFIRMED.sub("", text))
        if (CONFIRM_CLAIM.search(text)
                or re.search(r"\b(?:confirmed|booked)\b", unclaimed, re.IGNORECASE)):
            return "A booking request is pending owner approval; do not call it confirmed."
        lowered = text.lower()
        if "pending" not in lowered or "approv" not in lowered:
            return "Say the request is pending owner approval."
        if str(requested[0]["ref"]).lower() not in lowered:
            return "Include the request reference from the tool result."
    elif WRITE_CLAIM.search(CANCELLED.sub("done", text)
                            if any(result.get("cancelled") is True for result in results)
                            else text):
        return "The SMS claims a change; the tools made none."
    elif outcomes and "nothing" not in text.lower():
        return "The request was not made. Say that nothing was booked or changed."
    if (problem := _cancellation_problem(text, results)) is not None:
        return problem
    days, times, refs = _facts(text)
    allowed_days, allowed_times, allowed_refs = _authorized(results)
    if (not set(days) <= allowed_days or not set(times) <= allowed_times
            or not set(refs) <= allowed_refs):
        return "The SMS states a date, time, or reference that no tool result gave."
    if offer:
        starts = [start.astimezone(zone) for start in offer]
        if not _offer_in_order(text, starts):
            return "The SMS must list every offered time, in order, with its own number and date."
        if len(starts) > 1 and not all(
                re.search(rf"(?<!\d){number}\s*\)", text) for number in range(1, len(starts) + 1)):
            return "Number the offered times 1), 2), and so on."
        if "approv" not in text.lower():
            return "The SMS must say the owner approves every request."
    return None
