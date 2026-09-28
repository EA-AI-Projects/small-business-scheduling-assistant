"""Synthetic, read-only tool-call evaluation for the pilot message interpreter.

Run with OPENAI_API_KEY set in the process environment. This program never calls
the scheduling service and never includes real client data.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MODEL = "gpt-6-luna"
URL = "https://api.openai.com/v1/responses"
INSTRUCTIONS = (
    "Classify one synthetic scheduling text. You propose interpretation only; "
    "you cannot authorize a booking, approval, cancellation, or reschedule. "
    "When a date, appointment, or owner decision target is ambiguous or invalid, "
    "set needs_clarification true and ask one specific question. "
    "Never guess a calendar date or request reference. Always call propose_message once."
)
TOOL: dict[str, Any] = {
    "type": "function",
    "name": "propose_message",
    "description": "Propose an interpretation for backend validation; performs no writes.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": [
                "request_booking", "reschedule", "cancel", "owner_decision", "clarify", "unsupported"
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


@dataclass(frozen=True)
class Case:
    name: str
    actor: str
    context: str
    message: str
    must_clarify: bool


CASES = (
    Case("invalid-date", "client", "Today is 2026-09-28. No upcoming visit.",
         "Book me February 30 at 2pm.", True),
    Case("broad-window", "client", "Today is 2026-09-28. No upcoming visit.",
         "Can you come next Friday afternoon?", True),
    Case("missing-month", "client", "Today is 2026-09-28. No upcoming visit.",
         "How about Tuesday around 3?", True),
    Case("owner-yes-two-requests", "owner", "Two pending requests: A-101 and B-202.",
         "Yes", True),
    Case("owner-two-references", "owner", "Two pending requests: A-101 and B-202.",
         "Approve A-101 or B-202", True),
    Case("cancel-two-visits", "client", "Upcoming visits: A-101 and B-202.",
         "Cancel my appointment", True),
    Case("explicit-owner-reference", "owner", "One pending request: A-101.",
         "Approve A-101", False),
)


def request_payload(case: Case) -> dict[str, Any]:
    return {
        "model": MODEL,
        "instructions": INSTRUCTIONS,
        "input": f"Actor: {case.actor}\nContext: {case.context}\nText: {case.message}",
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
    if case.must_clarify:
        return (proposal["needs_clarification"] is True and
                proposal["intent"] == "clarify" and
                proposal["request_reference"] is None and
                proposal["date_text"] is None and
                proposal["owner_decision"] is None and
                isinstance(proposal["question"], str) and
                bool(proposal["question"].strip()))
    return (case.name == "explicit-owner-reference" and
            proposal["needs_clarification"] is False and
            proposal["intent"] == "owner_decision" and
            proposal["request_reference"] == "A-101" and
            proposal["owner_decision"] == "approve")


def evaluate(case: Case, key: str) -> dict[str, Any]:
    data = json.dumps(request_payload(case)).encode()
    request = Request(URL, data=data, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
    })
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"OpenAI API returned HTTP {exc.code}") from exc
    proposal = parse_proposal(result)
    passed = case_passes(case, proposal)
    return {"case": case.name, "passed": passed,
            "needs_clarification": proposal["needs_clarification"],
            "intent": proposal["intent"], "question": proposal["question"]}


def main() -> int:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is required for live synthetic evaluation", file=sys.stderr)
        return 2
    results = [evaluate(case, key) for case in CASES]
    print(json.dumps({"model": MODEL, "results": results}, indent=2))
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
