"""Bounded OpenAI intent proposal; the conversation domain authorizes every action."""

import json
import re
from datetime import date
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.conversation import MessageContext, MessageProposal
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyContext,
    OwnerReplyIntent,
    OwnerReplyProposal,
)

MODEL = "gpt-6-luna"
DATE_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")
TIME_SHAPE = re.compile(r"\d{2}:\d{2}")
URL = "https://api.openai.com/v1/responses"
INSTRUCTIONS = (
    "Interpret one text message sent to a home-cleaning business. The text may be in any "
    "language or wording; read what the sender means. You only propose an "
    "interpretation. The backend checks the calendar, offers real open times, and changes "
    "nothing until the sender picks an offered option or confirms. "
    "Resolve relative dates such as today, tomorrow, Friday, this week, or next week using "
    "Today and its weekday in the context. A weekday name alone means its next occurrence "
    "on or after tomorrow. Write dates as YYYY-MM-DD and times as HH:MM in 24-hour local time. "
    "Intents: availability when a client asks for open times or wants to book a cleaning, "
    "with or without a time. Set date_from and date_to to the inclusive requested day range "
    "(the same day for one day). Set time_from and time_to for a time preference: the same "
    "value for one specific time, 08:00 to 12:00 for morning, 12:00 to 17:00 for afternoon, "
    "otherwise null. "
    "reschedule when a client wants to move an existing visit: target_date is the day of the "
    "existing visit if the text names it, and date_from/date_to/time_from/time_to describe "
    "the new preferred time if given. "
    "cancel when a client wants to cancel or cannot make a visit: target_date is the day of "
    "that visit if the text names it. "
    "calendar_question when a client asks about visits or requests they already have "
    "rather than open times: whether they have bookings, when the cleaners are coming, how "
    "many visits, whether one is confirmed, or a summary of their schedule or itinerary. "
    "Set range_scope to dates with date_from and date_to for an asked day range, to "
    "all_upcoming (dates null) when the client asks about all upcoming visits or names no "
    "day in a new question, or to keep (dates null) only for a follow-up that leaves the "
    "shown range as it is. "
    "Set statuses to [\"confirmed\"] or [\"pending\"] when the client asks only about "
    "confirmed visits or only about pending requests, [\"confirmed\", \"pending\"] when "
    "they ask for both or everything, otherwise null. Set view to count when they ask how "
    "many, list when they ask when, which, or for a summary, otherwise null. "
    "When Last calendar answer is given, the client is continuing that conversation: a "
    "follow-up (another day or week, the week after, only confirmed ones, how many, a "
    "summary) is calendar_question, and any field the follow-up does not change stays null "
    "so the backend keeps the shown value. "
    "clarify_booking, with date_from and date_to if a day is named, when the text could "
    "mean either checking an existing booking or requesting a new one. "
    "For every other intent, statuses, view, and range_scope are null. "
    "owner_decision when the owner approves or declines a request. "
    "Copy request_reference only when one of the available references appears literally in "
    "the text; never invent one. "
    "Use clarify, with every other field null, when no usable day or intent can be found, or "
    "when a date is impossible, and put one short question in question. "
    "Use unsupported for messages unrelated to scheduling. "
    "Leave date_text null. Always call propose_message exactly once."
)
_DATE = {"type": ["string", "null"], "description": "YYYY-MM-DD or null"}
_TIME = {"type": ["string", "null"], "description": "HH:MM 24-hour local time or null"}
TOOL: dict[str, Any] = {
    "type": "function",
    "name": "propose_message",
    "description": "Propose an intent for trusted backend validation; performs no writes.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": [
                "availability", "request_booking", "reschedule", "cancel", "calendar_question",
                "clarify_booking", "owner_decision", "clarify", "unsupported",
            ]},
            "request_reference": {"type": ["string", "null"]},
            "date_text": {"type": ["string", "null"]},
            "date_from": _DATE,
            "date_to": _DATE,
            "time_from": _TIME,
            "time_to": _TIME,
            "target_date": _DATE,
            "owner_decision": {"type": ["string", "null"],
                               "enum": ["approve", "decline", None]},
            "needs_clarification": {"type": "boolean"},
            "question": {"type": ["string", "null"]},
            "statuses": {"type": ["array", "null"],
                         "items": {"type": "string", "enum": ["confirmed", "pending"]}},
            "view": {"type": ["string", "null"], "enum": ["list", "count", None]},
            "range_scope": {"type": ["string", "null"],
                            "enum": ["dates", "all_upcoming", "keep", None]},
        },
        "required": ["intent", "request_reference", "date_text", "date_from", "date_to",
                     "time_from", "time_to", "target_date", "owner_decision",
                     "needs_clarification", "question", "statuses", "view", "range_scope"],
        "additionalProperties": False,
    },
}
OWNER_REPLY_INSTRUCTIONS = (
    "The business owner just texted a scheduling assistant. Use the recent context to decide "
    "what the reply means. You only classify it; the backend decides whether anything "
    "happens and never approves, declines, or sends anything on your word alone. "
    "Last assistant message says what the owner is replying to: calendar_answer (a calendar "
    "summary), approval_question (the assistant asked which request, and named one), "
    "offer_prompt (a drafted counteroffer text awaits YES or NO), "
    "offer_with_calendar_answer (a calendar answer was sent while a drafted offer still "
    "waits), offer_closed (an offer was cancelled, sent, or lapsed), or none. "
    "Intents: approve_named_request or decline_named_request only when the reply clearly "
    "decides the one request listed under Named request, or, when last message is none and "
    "exactly one request is pending, that request; copy its ref exactly. A short yes right "
    "after a calendar answer, an offer, or a closed offer is not an approval: 'confirmed "
    "please', 'yes please', 'ok thanks', 'go ahead' are not approvals then. "
    "confirm_offer when the reply clearly tells the assistant to send the drafted offer; "
    "cancel_offer when it clearly withdraws it. calendar_followup when it asks to change "
    "the statuses or dates already shown (set statuses to confirmed, pending, unavailable "
    "and date_from/date_to only for a new range). calendar_question for a calendar "
    "question that lacks a day or week. how_to when the owner asks how to approve or "
    "decline or what the assistant can do. Anything else, or any doubt: unclear. "
    "Set confidence to high only when you are certain; when in doubt choose unclear or "
    "low. Always call classify_owner_reply exactly once."
)
OWNER_REPLY_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "classify_owner_reply",
    "description": "Classify an owner reply for trusted backend validation; performs no writes.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": [item.value for item in OwnerReplyIntent]},
            "request_reference": {"type": ["string", "null"]},
            "confidence": {"type": "string", "enum": [item.value for item in Confidence]},
            "statuses": {"type": ["array", "null"],
                         "items": {"type": "string",
                                   "enum": ["confirmed", "pending", "unavailable"]}},
            "date_from": _DATE,
            "date_to": _DATE,
        },
        "required": ["intent", "request_reference", "confidence", "statuses", "date_from",
                     "date_to"],
        "additionalProperties": False,
    },
}
STATUS_NAMES = {"confirmed": CalendarStatus.CONFIRMED, "pending": CalendarStatus.PENDING_APPROVAL,
                "unavailable": CalendarStatus.UNAVAILABLE}
DATE_FIELDS = ("date_from", "date_to", "target_date")
TIME_FIELDS = ("time_from", "time_to")
ACTION_FIELDS = ("request_reference", "date_text", *DATE_FIELDS, *TIME_FIELDS, "owner_decision")


def model_input(body: str, context: MessageContext) -> str:
    calendar = (f"Last calendar answer: {context.calendar_answer}\n"
                if context.calendar_answer else "")
    return (
        f"Actor: {context.actor.value}\n"
        f"Today: {context.today.isoformat()} ({context.today.strftime('%A')})\n"
        f"Timezone: {context.timezone}\n"
        f"Booking horizon: {context.horizon_days} days\n"
        f"Available references: {', '.join(context.references) or 'none'}\n"
        f"{calendar}"
        f"Inbound text: {body}"
    )


class OpenAIMessageInterpreter:
    def __init__(self, api_key: str, timeout_seconds: int = 15) -> None:
        if not api_key or timeout_seconds <= 0:
            raise ValueError("Interpreter needs an API key and positive timeout")
        self._key = api_key
        self._timeout = timeout_seconds

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        if not body or len(body) > 1000 or len(context.references) > 8:
            raise ValueError("Message or context exceeds model bounds")
        input_text = model_input(body, context)
        payload = {
            "model": MODEL,
            "instructions": INSTRUCTIONS,
            "input": input_text,
            "tools": [TOOL],
            "tool_choice": {"type": "function", "name": "propose_message"},
            "parallel_tool_calls": False,
            "reasoning": {"effort": "none"},
            "max_output_tokens": 512,
            "store": False,
        }
        request = Request(URL, data=json.dumps(payload).encode(), headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=self._timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Model API HTTP {exc.code}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("output"), list):
            raise TypeError("Model response is malformed")
        calls = [item for item in result.get("output", ())
                 if isinstance(item, dict) and item.get("type") == "function_call"]
        if len(calls) != 1 or calls[0].get("name") != "propose_message":
            raise ValueError("Model did not return one proposal")
        arguments = calls[0].get("arguments")
        if not isinstance(arguments, str):
            raise TypeError("Model proposal arguments are malformed")
        raw = json.loads(arguments)
        if not isinstance(raw, dict) or set(raw) != set(TOOL["parameters"]["required"]):
            raise ValueError("Model proposal schema mismatch")
        # The question is discarded below; the response token cap bounds its size.
        # Keep the strict 128-character limit on fields used for actions.
        if (raw["intent"] not in TOOL["parameters"]["properties"]["intent"]["enum"]
                or type(raw["needs_clarification"]) is not bool
                or raw["owner_decision"] not in ("approve", "decline", None)
                or any(value is not None and (not isinstance(value, str) or len(value) > 128)
                       for value in (raw["request_reference"], raw["date_text"]))
                or any(raw[name] is not None and not (
                    isinstance(raw[name], str) and DATE_SHAPE.fullmatch(raw[name]))
                    for name in DATE_FIELDS)
                or any(raw[name] is not None and not (
                    isinstance(raw[name], str) and TIME_SHAPE.fullmatch(raw[name]))
                    for name in TIME_FIELDS)
                or (raw["question"] is not None and not isinstance(raw["question"], str))
                or (raw["statuses"] is not None and not (
                    isinstance(raw["statuses"], list) and len(raw["statuses"]) <= 2
                    and all(item in ("confirmed", "pending") for item in raw["statuses"])))
                or raw["view"] not in ("list", "count", None)
                or raw["range_scope"] not in ("dates", "all_upcoming", "keep", None)):
            raise ValueError("Model proposal values are invalid")
        calendar = raw["intent"] in ("calendar_question", "clarify_booking")
        if calendar and raw["range_scope"] == "dates" and raw["date_from"] is None:
            raise ValueError("A dated range needs its dates")
        if raw["needs_clarification"] and (raw["intent"] != "clarify"
                                           or any(raw[name] is not None for name in ACTION_FIELDS)):
            raise ValueError("Ambiguous proposal contains an action")
        # The domain validates each date against the calendar; a shape-valid but
        # impossible date (February 30) becomes a clarification there.
        return MessageProposal(raw["intent"], raw["request_reference"], raw["date_text"],
                               raw["owner_decision"], raw["needs_clarification"],
                               raw["date_from"], raw["date_to"], raw["time_from"],
                               raw["time_to"], raw["target_date"],
                               # Ignored on other intents, as the instructions say.
                               tuple(raw["statuses"]) if calendar and raw["statuses"] else None,
                               raw["view"] if calendar else None,
                               raw["range_scope"] if calendar else None)


    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal:
        if not body or len(body) > 1000 or len(context.pending) > 8:
            raise ValueError("Message or context exceeds model bounds")
        named = context.named
        lines = [
            f"Today: {context.today.isoformat()} ({context.today.strftime('%A')})",
            f"Timezone: {context.timezone}",
            f"Last assistant message: {context.last_kind}",
            f"Calendar view: {context.view}; statuses shown: {', '.join(context.statuses) or 'none'}",
            "Open offer: " + (f"ref {context.offer.ref}, {context.offer.client}, "
                              f"new time {context.offer.when}" if context.offer else "none"),
            "Range shown: " + (f"{context.range_first} to {context.range_last}"
                               if context.range_first and context.range_last else "none"),
            "Named request: " + (f"ref {named.ref}, {named.client}, {named.when}"
                                 if named else "none"),
            "Pending requests: " + ("; ".join(
                f"ref {item.ref}, {item.client}, {item.when}" for item in context.pending)
                or "none"),
            f"Owner reply: {body}",
        ]
        payload = {
            "model": MODEL, "instructions": OWNER_REPLY_INSTRUCTIONS,
            "input": "\n".join(lines), "tools": [OWNER_REPLY_TOOL],
            "tool_choice": {"type": "function", "name": "classify_owner_reply"},
            "parallel_tool_calls": False, "reasoning": {"effort": "none"},
            "max_output_tokens": 256, "store": False,
        }
        request = Request(URL, data=json.dumps(payload).encode(), headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=self._timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Model API HTTP {exc.code}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("output"), list):
            raise TypeError("Model response is malformed")
        calls = [item for item in result["output"]
                 if isinstance(item, dict) and item.get("type") == "function_call"]
        if len(calls) != 1 or calls[0].get("name") != "classify_owner_reply":
            raise ValueError("Model did not return one classification")
        arguments = calls[0].get("arguments")
        if not isinstance(arguments, str):
            raise TypeError("Model classification arguments are malformed")
        raw = json.loads(arguments)
        required = OWNER_REPLY_TOOL["parameters"]["required"]
        if not isinstance(raw, dict) or set(raw) != set(required):
            raise ValueError("Model classification schema mismatch")
        reference = raw["request_reference"]
        statuses = raw["statuses"]
        if (raw["intent"] not in {item.value for item in OwnerReplyIntent}
                or raw["confidence"] not in {item.value for item in Confidence}
                or (reference is not None and (not isinstance(reference, str)
                                               or len(reference) > 64))
                or (statuses is not None and not (
                    isinstance(statuses, list) and all(item in STATUS_NAMES for item in statuses)))
                or any(raw[name] is not None and not (
                    isinstance(raw[name], str) and DATE_SHAPE.fullmatch(raw[name]))
                    for name in ("date_from", "date_to"))):
            raise ValueError("Model classification values are invalid")
        return OwnerReplyProposal(
            OwnerReplyIntent(raw["intent"]), reference, Confidence(raw["confidence"]),
            frozenset(STATUS_NAMES[item] for item in statuses) if statuses else None,
            date.fromisoformat(raw["date_from"]) if raw["date_from"] else None,
            date.fromisoformat(raw["date_to"]) if raw["date_to"] else None)
