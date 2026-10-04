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
    "Interpret one text message sent to a home-cleaning business. You only propose an "
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
                "availability", "request_booking", "reschedule", "cancel", "owner_decision",
                "clarify", "unsupported",
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
        },
        "required": ["intent", "request_reference", "date_text", "date_from", "date_to",
                     "time_from", "time_to", "target_date", "owner_decision",
                     "needs_clarification", "question"],
        "additionalProperties": False,
    },
}
OWNER_REPLY_INSTRUCTIONS = (
    "The business owner just replied to a scheduling assistant that is answering calendar "
    "questions. You only classify the reply; the backend decides whether anything happens "
    "and never approves or declines on your word alone. "
    "Intents: approve_named_request only when the reply clearly and unambiguously approves "
    "the specific request listed under Named request, and decline_named_request likewise "
    "for declining it; copy its ref exactly. If no request is named, or the reply could "
    "instead be asking to see or narrow the calendar (for example 'confirmed please', 'yes "
    "please', 'ok thanks' right after a calendar answer), do not choose an approval: use "
    "calendar_followup when it asks to change the status or dates shown, otherwise unclear. "
    "For calendar_followup set statuses to the statuses to show (confirmed, pending, "
    "unavailable) and date_from/date_to only if the reply names a new range. "
    "Use unclear when unsure. Set confidence to high only when you are certain; "
    "when in doubt choose unclear or low. Always call classify_owner_reply exactly once."
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
    return (
        f"Actor: {context.actor.value}\n"
        f"Today: {context.today.isoformat()} ({context.today.strftime('%A')})\n"
        f"Timezone: {context.timezone}\n"
        f"Booking horizon: {context.horizon_days} days\n"
        f"Available references: {', '.join(context.references) or 'none'}\n"
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
                or (raw["question"] is not None and not isinstance(raw["question"], str))):
            raise ValueError("Model proposal values are invalid")
        if raw["needs_clarification"] and (raw["intent"] != "clarify"
                                           or any(raw[name] is not None for name in ACTION_FIELDS)):
            raise ValueError("Ambiguous proposal contains an action")
        # The domain validates each date against the calendar; a shape-valid but
        # impossible date (February 30) becomes a clarification there.
        return MessageProposal(raw["intent"], raw["request_reference"], raw["date_text"],
                               raw["owner_decision"], raw["needs_clarification"],
                               raw["date_from"], raw["date_to"], raw["time_from"],
                               raw["time_to"], raw["target_date"])


    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal:
        if not body or len(body) > 1000 or len(context.pending) > 8:
            raise ValueError("Message or context exceeds model bounds")
        named = context.named
        lines = [
            f"Today: {context.today.isoformat()} ({context.today.strftime('%A')})",
            f"Timezone: {context.timezone}",
            f"Last assistant message: {context.last_kind}",
            f"Calendar view: {context.view}; statuses shown: {', '.join(context.statuses) or 'none'}",
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
