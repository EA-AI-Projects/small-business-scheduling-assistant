"""Bounded OpenAI intent proposal; the conversation domain authorizes every action."""

import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scheduling.domain.conversation import MessageContext, MessageProposal

MODEL = "gpt-6-luna"
URL = "https://api.openai.com/v1/responses"
INSTRUCTIONS = (
    "Interpret one scheduling SMS. Propose unambiguous intent and literal fields; "
    "the backend alone authorizes and performs any action. "
    "Use only the actor and references supplied in context. Never invent a reference. "
    "Return a date_text only when the exact YYYY-MM-DD or YYYY-MM-DD HH:MM "
    "substring appears in the inbound text; otherwise request clarification. "
    "For owner decisions, require the explicit word APPROVE or DECLINE and one reference. "
    "For a cancellation, require the explicit word CANCEL and one reference. "
    "For exact text APPROVE X or DECLINE X, where X is one available reference, "
    "propose owner_decision with that reference and decision, without clarification. "
    "For exact text CANCEL X, propose cancel with that reference. "
    "For BOOK YYYY-MM-DD or BOOK YYYY-MM-DD HH:MM, propose request_booking "
    "with the exact date substring from the inbound text, without clarification. "
    "When any target, date, time, or intent is ambiguous or invalid, set "
    "needs_clarification true, intent clarify, and all action fields null. "
    "Do not guess dates or references. Always call propose_message exactly once."
)
TOOL: dict[str, Any] = {
    "type": "function",
    "name": "propose_message",
    "description": "Propose an intent for trusted backend validation; performs no writes.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": [
                "request_booking", "reschedule", "cancel", "owner_decision", "clarify", "unsupported",
            ]},
            "request_reference": {"type": ["string", "null"]},
            "date_text": {"type": ["string", "null"]},
            "owner_decision": {"type": ["string", "null"],
                               "enum": ["approve", "decline", None]},
            "needs_clarification": {"type": "boolean"},
            "question": {"type": ["string", "null"]},
        },
        "required": ["intent", "request_reference", "date_text", "owner_decision",
                     "needs_clarification", "question"],
        "additionalProperties": False,
    },
}


class OpenAIMessageInterpreter:
    def __init__(self, api_key: str, timeout_seconds: int = 15) -> None:
        if not api_key or timeout_seconds <= 0:
            raise ValueError("Interpreter needs an API key and positive timeout")
        self._key = api_key
        self._timeout = timeout_seconds

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        if not body or len(body) > 1000 or len(context.references) > 8:
            raise ValueError("Message or context exceeds model bounds")
        input_text = (
            f"Actor: {context.actor.value}\n"
            f"Today: {context.today.isoformat()}\n"
            f"Timezone: {context.timezone}\n"
            f"Available references: {', '.join(context.references) or 'none'}\n"
            f"Inbound text: {body}"
        )
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
        if (raw["intent"] not in TOOL["parameters"]["properties"]["intent"]["enum"]
                or type(raw["needs_clarification"]) is not bool
                or raw["owner_decision"] not in ("approve", "decline", None)
                or any(value is not None and (not isinstance(value, str) or len(value) > 128)
                       for value in (raw["request_reference"], raw["date_text"],
                                     raw["question"]))):
            raise ValueError("Model proposal values are invalid")
        if raw["needs_clarification"] and (raw["intent"] != "clarify"
                                           or raw["request_reference"] is not None
                                           or raw["date_text"] is not None
                                           or raw["owner_decision"] is not None):
            raise ValueError("Ambiguous proposal contains an action")
        return MessageProposal(raw["intent"], raw["request_reference"], raw["date_text"],
                               raw["owner_decision"], raw["needs_clarification"])
