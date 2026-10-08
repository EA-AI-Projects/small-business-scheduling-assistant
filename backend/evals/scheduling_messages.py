"""Read-only tool-call evaluation and fictional-message preview.

Run with OPENAI_API_KEY set in the process environment. Fixed cases are synthetic;
interactive input is user supplied. This program never calls the scheduling service.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from scheduling.adapters.openai_messages import (
    INSTRUCTIONS,
    MODEL,
    TOOL,
    OpenAIMessageInterpreter,
    model_input,
)
from scheduling.domain.client_records import ACCESS_CODE_PATTERN
from scheduling.domain.client_replies import (
    ClientReplyFact,
    ClientReplyResult,
    facts_from_text,
    valid_draft,
    valid_owner_draft,
)
from scheduling.domain.conversation import MessageContext
from scheduling.domain.conversation_history import HistoryMessage
from scheduling.domain.owner_reply_classification import (
    OwnerReplyContext,
    OwnerReplyIntent,
    OwnerReplyProposal,
    PendingRef,
)
from scheduling.domain.sms_ingress import SenderRole

URL = "https://api.openai.com/v1/responses"
TIMEZONE = "America/Los_Angeles"
TODAY = date(2026, 9, 28)  # A Monday; relative dates below resolve from it.
TWO_REFS = ("a101a101", "b202b202")
PHONE_IN_TEXT = re.compile(
    r"(?<!\w)(?:\+?1[\s.()-]?)?(?:\(?[2-9]\d{2}\)?[\s.()-]?)"
    r"[2-9]\d{2}[\s.()-]?\d{4}(?!\w)"
)
ACTION_FIELDS = ("request_reference", "date_text", "date_from", "date_to", "time_from",
                 "time_to", "target_date", "owner_decision")


@dataclass(frozen=True)
class Case:
    name: str
    actor: str
    message: str
    references: tuple[str, ...]
    expect: Callable[[dict[str, Any]], bool]
    calendar_answer: str | None = None  # The open calendar answer, for follow-ups.
    prompt_kind: str = "none"  # The open prompt kind the service would send (#272, #273).
    history: tuple[tuple[str, str], ...] = ()  # (role, text) transcript lines, oldest first.
    today: date | None = None  # Overrides TODAY for cases about a later week.


def clarifies(proposal: dict[str, Any]) -> bool:
    return (proposal["needs_clarification"] is True and proposal["intent"] == "clarify"
            and all(proposal[name] is None for name in ACTION_FIELDS)
            and isinstance(proposal["question"], str) and bool(proposal["question"].strip()))


def asks_for_day(day: str | set[str], earliest: str | None = None,
                 latest: str | None = None) -> Callable[[dict[str, Any]], bool]:
    """An availability proposal for one resolved day, optionally inside a time window."""
    days = {day} if isinstance(day, str) else day

    def check(proposal: dict[str, Any]) -> bool:
        if (proposal["intent"] not in ("availability", "request_booking")
                or proposal["needs_clarification"] or proposal["request_reference"] is not None
                or proposal["date_text"] is not None or proposal["owner_decision"] is not None
                or proposal["date_from"] not in days
                or proposal["date_to"] not in (None, proposal["date_from"])):
            return False
        if earliest is None or latest is None:
            return True
        start, end = proposal["time_from"], proposal["time_to"] or proposal["time_from"]
        return (isinstance(start, str) and isinstance(end, str)
                and earliest <= start <= end <= latest)
    return check


def no_reference(proposal: dict[str, Any]) -> bool:
    return proposal["request_reference"] is None and proposal["date_text"] is None


THIS_WEEK = ("2026-09-28", "2026-10-04")
NEXT_WEEK = ("2026-10-05", "2026-10-11")
SHOWN_THIS_WEEK = "2026-09-28 to 2026-10-04; statuses: confirmed, pending; view: list"


def calendar(span: tuple[str, str] | None | str = "any", statuses: set[str] | None | str = "any",
             view: str | None = "any") -> Callable[[dict[str, Any]], bool]:
    """A calendar_question with the given range, statuses, and view ("any" skips a check;
    None requires the field left unset so the backend keeps the shown value)."""
    def check(proposal: dict[str, Any]) -> bool:
        if (proposal["intent"] != "calendar_question" or proposal["needs_clarification"]
                or not no_reference(proposal)):
            return False
        if span != "any" and (proposal["date_from"], proposal["date_to"]) != (
                span if span is not None else (None, None)):
            return False
        if statuses != "any" and (set(proposal["statuses"] or ()) or None) != statuses:
            return False
        return view == "any" or proposal["view"] == view
    return check


def this_week(proposal: dict[str, Any]) -> bool:
    """This week from Monday, or from today (the same day here)."""
    return calendar(THIS_WEEK)(proposal)


INVITE = ("assistant", (
    "Would you like to book a cleaning visit in the next two weeks? "
    "Reply with a day and time that works for you."))
ASKED_1PM = ("client", "Oct 13 at 1 pm")
UNAVAILABLE_1PM = ("assistant", (
    "That exact time isn't open. Open times on Tue Oct 13: "
    "1) 10:00 AM, 2) 11:00 AM. Reply with the number or time you want."))
OFFER_1PM = ("assistant", (
    "Tue Oct 13 at 1:00 PM is open for your 2-hour cleaning. "
    "Reply YES to request it. The owner approves every request."))
CANCEL_QUESTION = ("assistant", (
    "Cancel your Thu Oct 1 at 9:00 AM visit? Reply YES to cancel, or NO to keep it."))


CASES = (
    Case("invalid-date", "client", "Book me February 30 at 2pm.", (), clarifies),
    Case("tomorrow", "client", "Hi. Do you have availability for tomorrow?", (),
         asks_for_day("2026-09-29")),
    # "Next Friday" is genuinely ambiguous; either Friday is visible in the offer.
    Case("broad-window", "client", "Can you come next Friday afternoon?", (),
         lambda proposal: clarifies(proposal) or asks_for_day(
             {"2026-10-02", "2026-10-09"}, "12:00", "17:00")(proposal)),
    Case("missing-month", "client", "How about Tuesday around 3?", (),
         lambda proposal: clarifies(proposal) or asks_for_day(
             "2026-09-29", "14:00", "16:00")(proposal)),
    Case("owner-yes-two-requests", "owner", "Yes", TWO_REFS, no_reference),
    Case("owner-two-references", "owner", "Approve a101a101 or b202b202", TWO_REFS,
         no_reference),
    Case("cancel-two-visits", "client", "Cancel my appointment", TWO_REFS,
         lambda proposal: clarifies(proposal) or (
             proposal["intent"] == "cancel" and no_reference(proposal)
             and proposal["target_date"] is None)),
    Case("cant-make-thursday", "client", "I can't make Thursday", TWO_REFS,
         lambda proposal: proposal["intent"] == "cancel" and no_reference(proposal)
         and proposal["target_date"] == "2026-10-01"),
    Case("explicit-owner-reference", "owner", "Approve a101a101", ("a101a101",),
         lambda proposal: proposal["needs_clarification"] is False
         and proposal["intent"] == "owner_decision"
         and proposal["request_reference"] == "a101a101"
         and proposal["owner_decision"] == "approve"),
    # Client calendar questions (#241): the model reads any wording or language; the backend
    # answers from the client's own visits. Cases 1-3 are the reported screenshot texts.
    Case("screenshot-1-bookings-this-week", "client", "do I have any bookings for this week?",
         TWO_REFS, this_week),
    Case("screenshot-2-confirmed-already", "client",
         "Do I have any confirmed bookings already?  I'm trying to find out when the "
         "cleaners are coming", TWO_REFS,
         lambda proposal: calendar(statuses={"confirmed"})(proposal)
         and (proposal["date_from"], proposal["date_to"]) in ((None, None), THIS_WEEK)
         or calendar(None, {"confirmed"})(proposal),
         SHOWN_THIS_WEEK),
    Case("screenshot-3-summary", "client", "I want a summary of my itinerary", TWO_REFS,
         lambda proposal: calendar(view="list")(proposal) or calendar(view=None)(proposal),
         SHOWN_THIS_WEEK),
    Case("count-confirmed-next-week", "client", "How many confirmed visits next week?",
         TWO_REFS, calendar(NEXT_WEEK, {"confirmed"}, "count")),
    Case("spanish-question", "client", "¿Tengo alguna cita esta semana?", TWO_REFS,
         this_week),
    Case("spanish-follow-up", "client", "¿Y la próxima semana?", TWO_REFS,
         calendar(NEXT_WEEK), SHOWN_THIS_WEEK),
    Case("follow-up-only-confirmed", "client", "Just the confirmed ones", TWO_REFS,
         lambda proposal: calendar(None, {"confirmed"})(proposal)
         or calendar(THIS_WEEK, {"confirmed"})(proposal), SHOWN_THIS_WEEK),
    # A fresh question with no day, inside a calendar conversation, covers every visit.
    Case("all-upcoming-after-a-week", "client", "Show me all my upcoming visits", TWO_REFS,
         lambda proposal: calendar(None)(proposal)
         and proposal["range_scope"] == "all_upcoming", SHOWN_THIS_WEEK),
    # Open times keep the availability offer (owner decision on #240).
    Case("open-times", "client", "What times are open Friday?", TWO_REFS,
         asks_for_day("2026-10-02")),
    Case("request-during-calendar-talk", "client", "Can I get a cleaning Friday instead?",
         TWO_REFS, asks_for_day("2026-10-02"), SHOWN_THIS_WEEK),
    Case("ambiguous-booking", "client", "Booking for Friday?", TWO_REFS,
         lambda proposal: proposal["intent"] in ("clarify_booking", "clarify")
         and no_reference(proposal)),
    # Direct booking tools (#272, #273): the model books only a time the assistant offered.
    # Monday 2026-10-12 is "today" for these; the backend still validates every write.
    Case("invited-exact-time-is-offered-first", "client", "Oct 13 at 1 pm", (),
         lambda proposal: asks_for_day("2026-10-13", "13:00", "13:00")(proposal)
         and proposal["intent"] == "availability",
         history=(INVITE,), today=date(2026, 10, 12)),
    # The tester's friction: a date-only reply straight to an invitation (no prompt kind).
    Case("date-only-reply-to-invitation", "client", "Oct 13", (),
         lambda proposal: proposal["intent"] == "availability"
         and proposal["date_from"] == "2026-10-13" and not proposal["needs_clarification"],
         history=(INVITE,), today=date(2026, 10, 12)),
    Case("date-only-after-unavailable-time", "client", "Oct 13", (),
         lambda proposal: proposal["intent"] == "availability"
         and proposal["date_from"] == "2026-10-13" and not proposal["needs_clarification"]
         and proposal["time_from"] != "13:00",  # The unavailable time is not carried forward.
         prompt_kind="offer", today=date(2026, 10, 12),
         history=(INVITE, ASKED_1PM, UNAVAILABLE_1PM)),
    Case("free-form-acceptance-books-offered-time", "client", "lovely, lets lock that in", (),
         lambda proposal: proposal["intent"] == "request_booking"
         and (proposal["date_from"], proposal["date_to"]) == ("2026-10-13", "2026-10-13")
         and (proposal["time_from"], proposal["time_to"]) == ("13:00", "13:00"),
         prompt_kind="offer", today=date(2026, 10, 12),
         history=(INVITE, ASKED_1PM, OFFER_1PM)),
    Case("counter-proposal-is-not-an-acceptance", "client", "can we do 3 pm instead?", (),
         lambda proposal: proposal["intent"] == "availability"
         and proposal["date_from"] == "2026-10-13" and proposal["time_from"] == "15:00",
         prompt_kind="offer", today=date(2026, 10, 12),
         history=(OFFER_1PM,)),
    Case("question-about-offer-is-not-an-acceptance", "client", "is 1 pm the earliest you have?",
         (), lambda proposal: proposal["intent"] != "request_booking",
         prompt_kind="offer", today=date(2026, 10, 12),
         history=(OFFER_1PM,)),
    # The offer lapsed (no open prompt) but is still in the transcript: nothing may book.
    Case("acceptance-with-no-open-offer-books-nothing", "client", "sounds good", (),
         lambda proposal: proposal["intent"] != "request_booking",
         history=(OFFER_1PM,), today=date(2026, 10, 12)),
    Case("free-form-yes-confirms-cancel", "client", "yes please, go ahead", TWO_REFS,
         lambda proposal: proposal["intent"] == "confirm_cancel",
         prompt_kind="confirm_cancel",
         history=(("client", "I can't make Thursday"), CANCEL_QUESTION)),
    Case("free-form-keep-after-cancel-question", "client", "actually let's hold onto it",
         TWO_REFS, lambda proposal: proposal["intent"] == "keep_visit",
         prompt_kind="confirm_cancel",
         history=(CANCEL_QUESTION,)),
    Case("unrelated-question-does-not-confirm-cancel", "client", "what times are open Friday?",
         TWO_REFS, lambda proposal: proposal["intent"] == "availability"
         and proposal["date_from"] == "2026-10-02", prompt_kind="confirm_cancel",
         history=(CANCEL_QUESTION,)),
    Case("confirm-cancel-with-no-question-open", "client", "yes please, go ahead", TWO_REFS,
         lambda proposal: proposal["intent"] != "confirm_cancel",
         history=(CANCEL_QUESTION,)),  # The question lapsed; it is only in the transcript.
)


@dataclass(frozen=True)
class OwnerCase:
    """An owner text read by the production owner classifier (#173, #274)."""

    name: str
    message: str
    pending: tuple[PendingRef, ...]
    expect: Callable[[OwnerReplyProposal], bool]
    last_kind: str = "none"


AVERY = PendingRef("a101a101", "Avery", "Thu Oct 1 at 9:00 AM", 1)
BLAKE = PendingRef("b202b202", "Blake", "Fri Oct 2 at 1:00 PM", 3)

OWNER_CASES = (
    OwnerCase("owner-show-requests", "what's waiting for me?", (AVERY, BLAKE),
              lambda p: p.intent == OwnerReplyIntent.SHOW_REQUESTS),
    OwnerCase("owner-prepare-counteroffer", "offer Avery Friday at 2pm instead", (AVERY, BLAKE),
              lambda p: p.intent == OwnerReplyIntent.PREPARE_COUNTEROFFER
              and p.request_reference == "a101a101" and p.request_version == 1
              and p.offer_date == date(2026, 10, 2) and str(p.offer_time) == "14:00:00"),
    OwnerCase("owner-approve-by-name-quotes-version", "approve Blake's", (AVERY, BLAKE),
              lambda p: p.intent == OwnerReplyIntent.APPROVE_NAMED_REQUEST
              and p.request_reference == "b202b202" and p.request_version == 3),
    OwnerCase("owner-approve-it-with-two-pending-names-no-request", "approve it", (AVERY, BLAKE),
              lambda p: p.intent in (OwnerReplyIntent.UNCLEAR, OwnerReplyIntent.SHOW_REQUESTS)
              or p.request_reference is None),
    OwnerCase("owner-free-form-yes-after-calendar-is-not-approval", "yes please", (AVERY,),
              lambda p: p.intent not in (OwnerReplyIntent.APPROVE_NAMED_REQUEST,
                                         OwnerReplyIntent.DECLINE_NAMED_REQUEST),
              last_kind="calendar_answer"),
    OwnerCase("owner-single-pending-decline", "no, decline that one", (AVERY,),
              lambda p: p.intent == OwnerReplyIntent.DECLINE_NAMED_REQUEST
              and p.request_reference == "a101a101" and p.request_version == 1),
)


@dataclass(frozen=True)
class DraftCase:
    name: str
    message: str
    result: ClientReplyResult
    history: tuple[tuple[str, str], ...] = ()


DRAFT_CASES = (
    DraftCase("offer-after-invitation", "Oct 13 at 1 pm",
              ClientReplyResult(
                  "offer_made", "none",
                  "Tue Oct 13 at 1:00 PM is open for your cleaning. Reply YES to request it. "
                  "The owner approves every request.",
                  (ClientReplyFact("Tue Oct 13", "1:00 PM", "open"),)), (INVITE,)),
    DraftCase("pending-after-acceptance", "yes please",
              ClientReplyResult(
                  "request_created", "pending",
                  "Requested Tue Oct 13 at 1:00 PM (ref a101a101). It's pending owner "
                  "approval, not confirmed yet.",
                  (ClientReplyFact("Tue Oct 13", "1:00 PM", "pending owner approval",
                                   "a101a101"),)), (INVITE, ASKED_1PM, OFFER_1PM)),
    DraftCase("slot-taken", "yes please",
              ClientReplyResult(
                  "request_failed", "none",
                  "Sorry, Tue Oct 13 at 1:00 PM is no longer open, so nothing was booked. "
                  "Tell me what day works.",
                  (ClientReplyFact("Tue Oct 13", "1:00 PM", "unavailable"),),
                  "slot_taken"), (OFFER_1PM,)),
    DraftCase("cancelled-visit", "yes, cancel it",
              ClientReplyResult(
                  "cancelled", "cancelled",
                  "Cancelled your Thu Oct 1 at 9:00 AM visit (ref b202b202).",
                  (ClientReplyFact("Thu Oct 1", "9:00 AM", "cancelled", "b202b202"),)),
              (CANCEL_QUESTION,)),
    DraftCase("kept-visit", "actually keep it",
              ClientReplyResult(
                  "cancel_kept", "confirmed",
                  "OK, I kept your Thu Oct 1 at 9:00 AM visit. Nothing was cancelled.",
                  (ClientReplyFact("Thu Oct 1", "9:00 AM", "confirmed"),)),
              (CANCEL_QUESTION,)),
)


def evaluate_draft(case: DraftCase, key: str) -> dict[str, Any]:
    history_case = Case(case.name, "client", case.message, case.result.references,
                        lambda _proposal: True, history=case.history,
                        today=date(2026, 10, 12))
    draft = OpenAIMessageInterpreter(key, 30).draft_client_reply(
        case.message, context_for(history_case), case.result)
    return {"case": case.name, "passed": valid_draft(draft, case.result),
            "draft": draft}


@dataclass(frozen=True)
class OwnerDraftCase:
    """A read-only owner result the model may reword (#285); the suffix stays fixed text."""

    name: str
    message: str
    result: ClientReplyResult
    history: tuple[tuple[str, str], ...] = ()


def owner_result(kind: str, model_body: str, suffix: str) -> ClientReplyResult:
    return ClientReplyResult(kind, "read_only", model_body + suffix,
                             facts_from_text(model_body), None, model_body, suffix)


OWNER_ASKED = ("owner", "what's waiting for me?")
OWNER_ANSWERED = ("assistant", (
    "2 requests are pending, so nothing changed: Avery Sample, Thu Oct 1 at 9:00 AM "
    "(ref a101a101); Blake Example, Fri Oct 2 at 1:00 PM (ref b202b202). Reply APPROVE or "
    "DECLINE with the reference."))

OWNER_DRAFT_CASES = (
    OwnerDraftCase("owner-calendar-day", "what does Thursday look like?", owner_result(
        "owner_calendar",
        "Thu Oct 1, 2026 (America/Los_Angeles): 1 pending request.\n"
        "Thu Oct 1, 9:00 AM-11:00 AM: Avery (pending, ref a101a101)", "")),
    OwnerDraftCase("owner-calendar-page-keeps-paging-line", "show me next week", owner_result(
        "owner_calendar",
        "Mon Oct 5 to Sun Oct 11, 2026 (America/Los_Angeles): 1 confirmed visit, "
        "1 pending request.\nMon Oct 5, 9:00 AM-11:00 AM: Blake (confirmed)\n"
        "Tue Oct 6, 1:00 PM-3:00 PM: Avery (pending, ref a101a101)",
        "\nShowing 1-2 of 3. Reply MORE for the rest.")),
    OwnerDraftCase("owner-pending-summary", "what is pending?", owner_result(
        "owner_requests",
        "2 requests are pending: Avery, Thu Oct 1 at 9:00 AM (ref a101a101); Blake, "
        "Fri Oct 2 at 1:00 PM (ref b202b202).",
        " Nothing has changed. Reply APPROVE or DECLINE with the reference.")),
    OwnerDraftCase("owner-one-request-detail", "tell me about Avery's request", owner_result(
        "owner_requests",
        "Pending owner approval, not confirmed: Avery, Thu Oct 1 at 9:00 AM (ref a101a101), "
        "120 minutes.",
        " Reply APPROVE a101a101 or DECLINE a101a101 to decide it. Nothing has changed."),
        (OWNER_ASKED, OWNER_ANSWERED)),
    OwnerDraftCase("owner-how-to", "how do I approve something?", owner_result(
        "owner_how_to", "One request is pending: Avery, Thu Oct 1 at 9:00 AM (ref a101a101).",
        " To approve or decline a request, reply APPROVE or DECLINE with its reference.")),
    OwnerDraftCase("owner-how-to-no-requests", "what can you do?", owner_result(
        "owner_how_to", "No request is waiting for approval right now.",
        " To approve or decline a request, reply APPROVE or DECLINE with its reference.")),
)


def owner_context(case: OwnerCase) -> OwnerReplyContext:
    return OwnerReplyContext(TODAY, TIMEZONE, case.last_kind, "none", None, None, (),
                             None, case.pending)


def evaluate_owner(case: OwnerCase, key: str) -> dict[str, Any]:
    proposal = OpenAIMessageInterpreter(key, 30).classify_owner_reply(
        case.message, owner_context(case))
    return {"case": case.name, "passed": bool(case.expect(proposal)),
            "intent": proposal.intent.value, "request_reference": proposal.request_reference,
            "request_version": proposal.request_version, "confidence": proposal.confidence.value}


def evaluate_owner_draft(case: OwnerDraftCase, key: str) -> dict[str, Any]:
    at = datetime(2026, 10, 12, 17, tzinfo=ZoneInfo("UTC"))
    history = tuple(HistoryMessage(f"m{number}", role, at, text)
                    for number, (role, text) in enumerate(case.history))
    context = MessageContext(SenderRole.OWNER, date(2026, 10, 12), TIMEZONE, (),
                             history=history)
    draft = OpenAIMessageInterpreter(key, 30).draft_owner_reply(
        case.message, context, case.result)
    return {"case": case.name, "passed": valid_owner_draft(draft, case.result),
            "draft": draft, "sent_text": draft + case.result.suffix}


def context_for(case: Case, today: date = TODAY) -> MessageContext:
    at = datetime(2026, 10, 12, 17, tzinfo=ZoneInfo("UTC"))
    history = tuple(HistoryMessage(f"m{number}", role, at, text)
                    for number, (role, text) in enumerate(case.history))
    return MessageContext(SenderRole(case.actor), case.today or today, TIMEZONE, case.references,
                          calendar_answer=case.calendar_answer, history=history,
                          prompt_kind=case.prompt_kind)


def request_payload(case: Case, today: date = TODAY) -> dict[str, Any]:
    return {
        "model": MODEL,
        "instructions": INSTRUCTIONS,
        "input": model_input(case.message, context_for(case, today)),
        "tools": [TOOL],
        "tool_choice": {"type": "function", "name": "propose_message"},
        "parallel_tool_calls": False,
        "reasoning": {"effort": "none"},
        "max_output_tokens": 512,
        "store": False,
    }


def parse_proposal(response: dict[str, Any]) -> dict[str, Any]:
    calls = [item for item in response.get("output", ())
             if item.get("type") == "function_call"]
    if len(calls) != 1 or calls[0].get("name") != "propose_message":
        raise ValueError("Expected exactly one propose_message tool call")
    proposal = json.loads(calls[0]["arguments"])
    if not isinstance(proposal, dict):
        raise TypeError("Tool arguments must be an object")
    if set(proposal) != set(TOOL["parameters"]["required"]):
        raise ValueError("Tool arguments do not match the evaluation schema")
    if not isinstance(proposal["needs_clarification"], bool):
        raise TypeError("Clarification flag must be boolean")
    if proposal["needs_clarification"] and not proposal["question"]:
        raise ValueError("Clarification needs a question")
    return proposal


def case_passes(case: Case, proposal: dict[str, Any]) -> bool:
    return bool(case.expect(proposal))


def propose(case: Case, key: str, today: date = TODAY) -> dict[str, Any]:
    data = json.dumps(request_payload(case, today)).encode()
    request = Request(URL, data=data, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
    })
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"OpenAI API returned HTTP {exc.code}") from exc
    return parse_proposal(result)


def evaluate(case: Case, key: str) -> dict[str, Any]:
    proposal = propose(case, key)
    passed = case_passes(case, proposal)
    return {"case": case.name, "passed": passed,
            "needs_clarification": proposal["needs_clarification"],
            "intent": proposal["intent"], "question": proposal["question"],
            **{name: proposal[name] for name in (*ACTION_FIELDS, "statuses", "view",
                                                 "range_scope")
               if proposal[name] is not None}}


def safe_preview_message(message: str) -> bool:
    """Reject recognizable private input before any network request."""
    return not (ACCESS_CODE_PATTERN.search(message) or PHONE_IN_TEXT.search(message))


def interactive(key: str) -> int:
    print("Synthetic message preview. Use no real client details or access codes.")
    print("/client or /owner changes the actor; /quit exits. No bookings or texts are sent.")
    print(f"Synthetic references {' and '.join(TWO_REFS)} stand in for visits or requests.")
    actor = "client"
    while True:
        try:
            message = input(f"{actor}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if message == "/quit":
            return 0
        if message in {"/client", "/owner"}:
            actor = message[1:]
            continue
        if not message:
            continue
        if not safe_preview_message(message):
            print("Input looks like a phone number or access code; nothing was sent.")
            continue
        case = Case("preview", actor, message, TWO_REFS, lambda _proposal: True)
        today = datetime.now(ZoneInfo(TIMEZONE)).date()
        print(json.dumps(propose(case, key, today), indent=2))
        print("Preview only; the scheduling service has not acted on this proposal.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interactive", action="store_true",
                        help="Preview synthetic messages without scheduling writes")
    parser.add_argument("--drafts", action="store_true",
                        help="Run synthetic client SMS draft cases (requires a separately authorized live run)")
    parser.add_argument("--owner-drafts", action="store_true",
                        help="Run synthetic owner SMS draft cases (requires a separately "
                        "authorized live run)")
    args = parser.parse_args(argv)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is required for live synthetic evaluation", file=sys.stderr)
        return 2
    if args.interactive:
        return interactive(key)
    if args.drafts:
        results = [evaluate_draft(case, key) for case in DRAFT_CASES]
        print(json.dumps({"model": MODEL, "draft_results": results}, indent=2))
        return 0 if all(item["passed"] for item in results) else 1
    if args.owner_drafts:
        results = [evaluate_owner_draft(case, key) for case in OWNER_DRAFT_CASES]
        print(json.dumps({"model": MODEL, "owner_draft_results": results}, indent=2))
        return 0 if all(item["passed"] for item in results) else 1
    results = [evaluate(case, key) for case in CASES]
    results += [evaluate_owner(case, key) for case in OWNER_CASES]
    print(json.dumps({"model": MODEL, "results": results}, indent=2))
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
