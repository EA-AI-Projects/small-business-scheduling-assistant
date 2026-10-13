"""Small fictional, live-model check of the production client tool loop (#357).

It drives the production client instructions, tool schemas, and loop
(``OpenAIMessageInterpreter.run_client_loop``) with the production tool dispatcher and
reply check (``ClientReadTools``, ``check_reply``). Tool results come from a fictional
in-memory business and scripted state; it never reaches the scheduling service, outbox, SMS
provider, or AWS. It reports which tools the model chose for each case; it proves nothing
about the backend's own stored-state checks, which have offline tests.

Run only with --live and OPENAI_API_KEY in the process environment. Never run by pytest/CI.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from scheduling.adapters.openai_messages import MODEL, OpenAIMessageInterpreter
from scheduling.domain.appointments import Appointment
from scheduling.domain.availability import AvailabilityPolicy, pilot_policy
from scheduling.domain.business_profile import BusinessProfile, BusinessProfileRecord
from scheduling.domain.calendar import CalendarSnapshot, CalendarStatus
from scheduling.domain.client_tools import (
    CancellationHooks,
    ClientReadTools,
    CounterofferHooks,
    check_reply,
)
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.conversation_state import clock_text, when_text

ZONE_NAME = "America/Los_Angeles"
ZONE = ZoneInfo(ZONE_NAME)
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=ZONE)  # Monday; synthetic "today".
BUSINESS = "eval-business"
CLIENT = "eval-client"
WRITES = frozenset({"request_booking", "prepare_cancellation", "confirm_cancellation",
                    "dismiss_cancellation", "request_reschedule", "respond_to_counteroffer"})
HISTORY_AT = NOW - timedelta(minutes=5)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE)


def visit(ref: str, start: datetime, status: CalendarStatus = CalendarStatus.CONFIRMED,
          ) -> Appointment:
    return Appointment(
        f"{ref}-0000-4000-8000-000000000000", BUSINESS, CLIENT, start,
        start + timedelta(minutes=120), status,
        NOW + timedelta(days=1) if status == CalendarStatus.PENDING_APPROVAL else None,
        120, 30, 1)


THURSDAY = visit("b202b202", local(15, 10))
FRIDAY = visit("c303c303", local(16, 13))
OFFER_TIMES = (local(13, 9), local(13, 13))  # Tue Oct 13 at 9:00 AM and 1:00 PM
MOVE_TIMES = (local(16, 9), local(16, 13))  # Fri Oct 16 at 9:00 AM and 1:00 PM
COUNTER = local(14, 10)  # Wed Oct 14 at 10:00 AM, the owner's counteroffer
ORIGINAL_REQUEST = "Tue Oct 13 at 9:00 AM"


def said(text: str) -> tuple[HistoryMessage, ...]:
    return (HistoryMessage("fictional-assistant-1", "assistant", HISTORY_AT, text),)


OFFER_SAID = said("Open Tue Oct 13: 1) 9:00 AM, 2) 1:00 PM. The owner approves every "
                  "request. Reply with a number or time.")
MOVE_SAID = said("To move your Thu Oct 15 at 10:00 AM visit (ref b202b202), open times are "
                 "Fri Oct 16: 1) 9:00 AM, 2) 1:00 PM. Your current visit stays confirmed "
                 "until the owner approves the move. Reply with a number or time.")
CANCEL_SAID = said("Reply YES to cancel your Thu Oct 15 at 10:00 AM visit (ref b202b202). "
                   "Nothing is cancelled until you do.")
COUNTER_SAID = said("The owner can't do Tue Oct 13 at 9:00 AM but offers Wed Oct 14 at 10:00 "
                    "AM instead. Reply YES to request it or NO to decline. The owner "
                    "approves every request.")


@dataclass(frozen=True)
class Step:
    """An expected tool call: its name and the arguments it must include."""

    name: str
    args: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Case:
    name: str
    message: str
    expected: tuple[Step, ...]
    history: tuple[HistoryMessage, ...] = ()
    visits: tuple[Appointment, ...] = ()
    offer: tuple[datetime, ...] = ()  # An earlier unexpired new-booking offer.
    move: tuple[str, tuple[datetime, ...]] | None = None  # (ref, times) of a move offer.
    cancel_open: Appointment | None = None  # An open cancellation question.
    counter_open: bool = False  # An open owner counteroffer.
    note: str = ""


def step(name: str, **args: Any) -> Step:
    return Step(name, tuple(args.items()))


CASES: tuple[Case, ...] = (
    Case("book-accepted-offer", "2 works for me", (step("request_booking",
         date="2026-10-13", time="13:00"),), OFFER_SAID, offer=OFFER_TIMES),
    Case("book-accepted-in-words", "The 9 am one please", (step(
        "request_booking", date="2026-10-13", time="09:00"),), OFFER_SAID, offer=OFFER_TIMES),
    Case("book-unclear-acceptance", "Hmm, maybe one of those, I am not sure yet", (),
         OFFER_SAID, offer=OFFER_TIMES),
    Case("book-counter-proposal", "Could we do 3 PM instead?", (), OFFER_SAID,
         offer=OFFER_TIMES),
    Case("book-question-about-offer", "How long does the visit take?", (), OFFER_SAID,
         offer=OFFER_TIMES),
    Case("cancel-one-clear-visit", "Please cancel my Thursday visit",
         (step("prepare_cancellation", ref="b202b202"),), visits=(THURSDAY,)),
    Case("cancel-ambiguous-visit", "Please cancel my visit", (), visits=(THURSDAY, FRIDAY)),
    Case("cancel-yes", "Yes, cancel it", (step("confirm_cancellation"),), CANCEL_SAID,
         visits=(THURSDAY,), cancel_open=THURSDAY),
    Case("cancel-no", "No, keep it", (step("dismiss_cancellation"),), CANCEL_SAID,
         visits=(THURSDAY,), cancel_open=THURSDAY),
    Case("cancel-unclear", "Hmm, let me think about it", (), CANCEL_SAID,
         visits=(THURSDAY,), cancel_open=THURSDAY),
    Case("cancel-unrelated", "Do you bring your own supplies?", (), CANCEL_SAID,
         visits=(THURSDAY,), cancel_open=THURSDAY),
    Case("move-asks-availability", "Can you move my Thursday visit to Friday morning?",
         (step("get_availability", ref="b202b202"),), visits=(THURSDAY,),
         note="No move offer is open yet, so request_reschedule must not be called."),
    Case("move-accepted-time", "The 9 am works", (step(
        "request_reschedule", ref="b202b202", date="2026-10-16", time="09:00"),),
         MOVE_SAID, visits=(THURSDAY,), move=("b202b202", MOVE_TIMES)),
    Case("counteroffer-yes", "Yes, that works for me", (step(
        "respond_to_counteroffer", decision="accept"),), COUNTER_SAID, counter_open=True),
    Case("counteroffer-no", "No thanks, I will pass", (step(
        "respond_to_counteroffer", decision="decline"),), COUNTER_SAID, counter_open=True),
    Case("counteroffer-counter-proposal", "Could we do Thursday instead?", (),
         COUNTER_SAID, counter_open=True),
    Case("counteroffer-question", "Is that with the same crew as usual?", (),
         COUNTER_SAID, counter_open=True),
    Case("mixed-service-question-and-booking",
         "The 9 am one is good. Also, what does the service include?",
         (step("request_booking", date="2026-10-13", time="09:00"),), OFFER_SAID,
         offer=OFFER_TIMES),
    Case("mixed-cancel-and-question",
         "Please cancel my Thursday visit. Also, are you open on Saturdays?",
         (step("prepare_cancellation", ref="b202b202"),), visits=(THURSDAY,)),
)


class Repository:
    """A fictional in-memory business: one service, weekday hours, an empty calendar."""

    def read_policy(self, business_id: str) -> AvailabilityPolicy:
        return pilot_policy()

    def read_business_profile(self, business_id: str) -> BusinessProfileRecord:
        return BusinessProfileRecord(BusinessProfile(
            "Example Cleaning Co.", "A synthetic small business.", "support@example.invalid",
            "Standard cleaning", "A synthetic service visit.", 120), 1)

    def read_calendar(self, business_id: str) -> CalendarSnapshot:
        return CalendarSnapshot(business_id, 1, ())


def _facts(start: datetime) -> dict[str, Any]:
    return {"date": start.date().isoformat(), "weekday": start.strftime("%A"),
            "time": clock_text(start, ZONE), "when": when_text(start, ZONE)}


def _requested(start: datetime, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "requested": True, "status": "pending_owner_approval",
            "confirmed": False, "owner_approval_required": True, "ref": "d404d404",
            **_facts(start), "duration_minutes": 120, **extra}


def _offered(times: tuple[datetime, ...]) -> list[dict[str, Any]]:
    return [{"number": number, "date": start.date().isoformat(), "time": clock_text(start, ZONE),
             "when": when_text(start, ZONE)} for number, start in enumerate(times, 1)]


def short_ref(value: object) -> object:
    """A reference as production compares it: case-insensitive, full ID or its 8 characters."""
    if not isinstance(value, str):
        return value
    lowered = value.lower()
    for item in (THURSDAY, FRIDAY):
        if lowered == item.appointment_id.lower():
            return item.appointment_id[:8].lower()
    return lowered


def _nothing(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "requested": False, "nothing_changed": True, "reason": reason, **extra}


def build_tools(case: Case) -> ClientReadTools:
    """Production tool dispatcher over fictional state; hooks only read the case."""
    def request(day: date, clock: time) -> dict[str, Any]:
        if case.move is not None:
            return _nothing("offer_is_for_a_move")
        if not case.offer:
            return _nothing("no_offer_to_accept")
        wall = datetime.combine(day, clock)
        found = [start for start in case.offer if start.replace(tzinfo=None) == wall]
        return (_requested(found[0]) if len(found) == 1
                else _nothing("not_an_offered_time", offered=_offered(case.offer)))

    def reschedule(ref: str, day: date, clock: time) -> dict[str, Any]:
        if case.move is None:
            return _nothing("no_move_offer_to_accept" if case.offer
                            else "no_offer_to_accept")
        if short_ref(ref) != case.move[0]:
            return _nothing("not_the_visit_offered_for")
        wall = datetime.combine(day, clock)
        found = [start for start in case.move[1] if start.replace(tzinfo=None) == wall]
        original = case.visits[0]
        if len(found) != 1:
            return _nothing("not_an_offered_time", offered=_offered(case.move[1]))
        return _requested(found[0], replaces_ref=original.appointment_id[:8],
                          original_status="confirmed", original_stays_confirmed=True,
                          original_when=when_text(original.start_at, ZONE))

    def prepare(ref: str) -> dict[str, Any]:
        matches = [item for item in case.visits if short_ref(item.appointment_id) == short_ref(ref)]
        if len(matches) != 1:
            return {"ok": True, "prepared": False, "cancelled": False,
                    "cancellation_question_open": False, "nothing_changed": True,
                    "reason": "ambiguous_visit" if matches else "visit_not_found",
                    "visits": [{"ref": item.appointment_id[:8],
                                "when": when_text(item.start_at, ZONE), "status": "confirmed"}
                               for item in case.visits]}
        target = matches[0]
        return {"ok": True, "prepared": True, "cancelled": False,
                "cancellation_question_open": True, "nothing_cancelled_yet": True,
                "ref": target.appointment_id[:8], "status": "confirmed", "confirmed": True,
                **_facts(target.start_at), "confirmation_valid_minutes": 30}

    def answer(dismiss: bool) -> dict[str, Any]:
        key = "dismissed" if dismiss else "cancelled"
        target = case.cancel_open
        if target is None:
            return {"ok": True, key: False, "cancelled": False,
                    "cancellation_question_open": False, "nothing_changed": True,
                    "reason": "no_open_confirmation"}
        if dismiss:
            return {"ok": True, "dismissed": True, "cancelled": False,
                    "cancellation_question_open": False, "nothing_cancelled": True,
                    "still_booked": True, "ref": target.appointment_id[:8],
                    "when": when_text(target.start_at, ZONE)}
        return {"ok": True, "cancelled": True, "nothing_changed": False,
                "ref": target.appointment_id[:8], "was": "confirmed",
                **_facts(target.start_at)}

    def counter_read() -> dict[str, Any]:
        if not case.counter_open:
            return {"ok": True, "open": False}
        return {"ok": True, "open": True, "owner_approval_required": True,
                **_facts(COUNTER), "duration_minutes": 120,
                "your_request": {"ref": "a101a101", "status": "pending_owner_approval",
                                 "when": ORIGINAL_REQUEST}}

    def counter_respond(accept: bool) -> dict[str, Any]:
        if not case.counter_open:
            return {"ok": True, **_nothing("no_open_offer")}
        if not accept:
            return {"ok": True, "requested": False, "declined": True, "nothing_changed": True,
                    "original_request_unchanged": True}
        return {"ok": True, **_requested(COUNTER, earlier_request_ref="a101a101",
                                         earlier_request_stays_pending=True,
                                         earlier_request_when=ORIGINAL_REQUEST)}

    return ClientReadTools(
        Repository(), BUSINESS, NOW, pilot_policy(), case.visits,
        lambda _starts, _minutes, _original: None, request,
        CancellationHooks(prepare, lambda: answer(False), lambda: answer(True)),
        reschedule, CounterofferHooks(counter_read, counter_respond))


class ClientLoopModel(Protocol):
    def run_client_loop(self, body: str, now: datetime, timezone: str,
                        history: tuple[HistoryMessage, ...],
                        tool: Callable[[str, dict[str, Any]], dict[str, Any]],
                        check: Callable[[str], str | None]) -> str: ...


def _matches(call: dict[str, Any], wanted: Step) -> bool:
    return call["name"] == wanted.name and all(
        isinstance(call["args"], dict) and (
            short_ref(call["args"].get(key)) == short_ref(value) if key == "ref"
            else call["args"].get(key) == value)
        for key, value in wanted.args)


def tool_choice_ok(expected: Sequence[Step], calls: Sequence[dict[str, Any]]) -> bool:
    """Expected calls appear in order; no other write tool was called; each write once."""
    position = 0
    for wanted in expected:
        while position < len(calls) and not _matches(calls[position], wanted):
            position += 1
        if position == len(calls):
            return False
        position += 1
    allowed = {wanted.name for wanted in expected}
    written = [call["name"] for call in calls if call["name"] in WRITES]
    return (all(name in allowed for name in written)
            and len(written) == len(set(written)))


def run_case(model: ClientLoopModel, case: Case) -> dict[str, Any]:
    """Return expected vs actual tools, the final reply, and its check_reply result."""
    tools = build_tools(case)
    calls: list[dict[str, Any]] = []
    rejected: list[str] = []

    def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append({"name": name, "args": args})
        return tools(name, args)

    def check(text: str) -> str | None:
        problem = check_reply(text, tools.results, tools.offer, ZONE)
        if problem is not None:
            rejected.append(problem)
        return problem

    reply: str | None = None
    error: str | None = None
    try:
        reply = model.run_client_loop(case.message, NOW, ZONE_NAME, case.history, tool, check)
    except (ValueError, RuntimeError, TypeError, TimeoutError, OSError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    reply_ok = reply is not None and check_reply(reply, tools.results, tools.offer,
                                                 ZONE) is None
    # A failed model call is an error, never a tool-choice pass.
    chosen = tool_choice_ok(case.expected, calls) if error is None else None
    return {
        "case": case.name, "message": case.message,
        "expected_tools": [{"name": item.name, "args": dict(item.args)}
                           for item in case.expected] or "no write tool",
        "actual_tools": calls, "tool_choice_passed": chosen,
        "reply": reply, "reply_passes_check_reply": reply_ok,
        "check_reply_rejections": rejected, "error": error, "note": case.note,
        "review": ("Simulated tool results only; the backend does not judge status wording, "
                   "so read the reply for accuracy against the tool results."),
    }


def main(argv: Sequence[str] | None = None,
         model_factory: Callable[[str], ClientLoopModel] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Call the production model with fictional in-memory tools")
    parser.add_argument("--case", choices=[case.name for case in CASES],
                        help="Run one case instead of all")
    args = parser.parse_args(argv)
    if not args.live:
        print("Cases: " + ", ".join(case.name for case in CASES))
        print("No model call made. Add --live to run; API usage may incur charges.")
        return 0
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        parser.error("OPENAI_API_KEY is required with --live")
    model = (model_factory or OpenAIMessageInterpreter)(key)
    chosen = [case for case in CASES if args.case in (None, case.name)]
    results = [run_case(model, case) for case in chosen]  # One pass per case.
    failed = [result["case"] for result in results if result["tool_choice_passed"] is False]
    errors = [{"case": result["case"], "error": result["error"]}
              for result in results if result["error"] is not None]
    unsendable = [result["case"] for result in results if not result["reply_passes_check_reply"]]
    print(json.dumps({
        "model": MODEL, "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cases": len(results), "tool_choice_failures": failed,
        "errors": errors, "reply_check_failures": unsendable,
        "results": results}, indent=2))
    # Status wording is the model's (#359) and is only reported for review. What check_reply
    # still rejects (a date, time, or reference no tool gave, or an unsendable length or
    # alphabet) is reported and does fail the run.
    return 1 if (failed or errors or unsendable) else 0


if __name__ == "__main__":
    raise SystemExit(main())
