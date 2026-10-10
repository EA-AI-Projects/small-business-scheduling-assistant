"""Small fictional, live-model check of the production owner tool loop.

This runner supplies in-memory JSON tool results. It never reaches the scheduling
service, outbox, SMS provider, or AWS. Run only with --live and OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from scheduling.adapters.openai_messages import MODEL, OpenAIMessageInterpreter
from scheduling.domain.conversation_history import HistoryMessage

TODAY = date(2026, 10, 12)
ZONE = "America/Los_Angeles"
REF = "a101a101"
VERSION = 1
REQUEST = {"ref": REF, "version": VERSION, "client": "Avery",
           "time": "Tue Oct 13 at 9:00 AM", "status": "PENDING_APPROVAL"}


@dataclass(frozen=True)
class Case:
    name: str
    message: str
    stale: bool = False
    history: tuple[HistoryMessage, ...] = ()


CASES = (
    Case("approve-one", "Approve"),
    Case("hedged-yes", "Yes, but please tell me when it's done.", history=(
        HistoryMessage("fictional-assistant-1", "assistant",
                       datetime(2026, 10, 12, 15, tzinfo=UTC),
                       "Avery has one request pending approval for Tue Oct 13 at 9:00 AM."),)),
    Case("stale-request", "Approve Avery's request", stale=True),
)


def run_case(model: OpenAIMessageInterpreter, case: Case) -> dict[str, Any]:
    """Return the model reply and every fictional tool call for manual inspection."""
    calls: list[dict[str, Any]] = []

    def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append({"name": name, "args": args})
        if name == "list_pending_requests" and args == {}:
            return {"ok": True, "requests": [REQUEST], "open_offer": None}
        if (name == "approve_request" and args == {"ref": REF, "version": VERSION}):
            if case.stale:
                return {"ok": False, "error": "stale_version", "ref": REF,
                        "current_version": VERSION + 1}
            return {"ok": True, "ref": REF, "status": "CONFIRMED"}
        return {"ok": False, "error": "invalid_eval_call"}

    reply = model.run_owner_loop(case.message, TODAY, ZONE, case.history, tool)
    approval_positions = [index for index, call in enumerate(calls)
                          if call["name"] == "approve_request"]
    correct_approval = (len(approval_positions) == 1
                        and calls[0:1] == [{"name": "list_pending_requests", "args": {}}]
                        and approval_positions[0] > 0
                        and calls[approval_positions[0]]["args"]
                        == {"ref": REF, "version": VERSION})
    no_other_writes = all(call["name"] in ("list_pending_requests", "approve_request")
                          for call in calls)
    stale_failure = bool(re.search(
        r"nothing changed|didn.t approve|couldn.t approve|cannot approve|can.t approve|"
        r"request (?:has )?changed|stale", reply, re.IGNORECASE))
    # Conservative eval gate: even a negated success word needs manual review.
    stale_false_success = bool(re.search(
        r"\b(?:approved|confirmed|completed|queued|sent|done)\b",
        reply, re.IGNORECASE))
    stale_truth = not case.stale or (stale_failure and not stale_false_success)
    return {"case": case.name, "passed": correct_approval and no_other_writes and stale_truth,
            "reply": reply, "calls": calls,
            "note": "Simulated tool results only; review the final wording for accuracy."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Call the production model with fictional in-memory tools")
    parser.add_argument("--case", choices=[case.name for case in CASES],
                        help="Run one case instead of all three")
    args = parser.parse_args()
    if not args.live:
        print("Cases: " + ", ".join(case.name for case in CASES))
        print("No model call made. Add --live to run; API usage may incur charges.")
        return 0
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        parser.error("OPENAI_API_KEY is required with --live")
    model = OpenAIMessageInterpreter(key)
    chosen = [case for case in CASES if args.case in (None, case.name)]
    results = [run_case(model, case) for case in chosen]
    print(json.dumps({"model": MODEL, "results": results}, indent=2))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
