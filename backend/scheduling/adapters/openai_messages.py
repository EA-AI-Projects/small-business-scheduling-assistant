"""Bounded OpenAI intent proposal; the conversation domain authorizes every action."""

import json
import re
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scheduling.domain.conversation import MessageContext, MessageProposal

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
