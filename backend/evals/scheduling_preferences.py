"""Fictional, live-model evaluation of owner scheduling-preference interpretation (#380, #381).

Scripted-model tests in ``tests/test_scheduling_preferences.py`` prove permissions and
effects only. This runner checks whether the real model reads supported guidance
correctly, separates business-wide from client-named guidance, and flags vague,
contradictory, unsupported and booking-constraint-override text. Client names are matched
to the synthetic profiles below by deterministic code, never by the model, so duplicate
names must surface as ambiguity rather than a guess. It uses synthetic sentences, never reaches the
scheduling service, SMS provider, or AWS, and calls the model only with --live and
OPENAI_API_KEY (billable; needs separate authorization).
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any

from scheduling.adapters.openai_messages import MODEL, OpenAIMessageInterpreter
from scheduling.domain.scheduling_preferences import ClientReference, review_preferences

ZONE = "America/Los_Angeles"
# Synthetic profiles: two share a name on purpose.
PROFILES = (
    ClientReference("client-alder", "Client Alder", "0001"),
    ClientReference("client-birch", "Client Birch", "0002"),
    ClientReference("client-cedar-1", "Cedar Household", "0003"),
    ClientReference("client-cedar-2", "Cedar Household", "0004"),
)


@dataclass(frozen=True)
class Case:
    name: str
    text: str
    # Acceptable problem kinds; empty means the text must be ready (and satisfy ``weekdays``).
    kinds: tuple[str, ...] = ()
    weekdays: tuple[int, ...] = ()
    # For a ready case: every window must start no earlier and end no later than these bounds.
    bounds: tuple[str, str] | None = None
    # For a ready case: exactly these profile ids are bound (empty means business-wide only).
    clients: frozenset[str] = frozenset()


CASES = (
    Case("supported-mornings", "Prefer mornings on Tuesdays and Thursdays.",
         weekdays=(1, 3), bounds=("06:00", "13:00")),
    Case("supported-explicit-range", "Thursdays between 1 PM and 4 PM are best.",
         weekdays=(3,), bounds=("13:00", "16:00")),
    Case("vague", "Whenever works best for me is fine.", kinds=("unclear",)),
    Case("vague-busy", "I like busy days.", kinds=("unclear", "unsupported")),
    Case("contradictory", "Prefer Mondays only. Also never schedule on Mondays.",
         kinds=("contradictory", "unsupported")),
    Case("avoidance", "Avoid Fridays.", kinds=("unsupported", "unclear")),
    Case("unsupported-route", "Group clients who live near each other on the same day.",
         kinds=("unsupported",)),
    Case("client-specific", "Prefer Thursday evenings for Client Alder.",
         weekdays=(3,), bounds=("17:00", "20:00"), clients=frozenset({"client-alder"})),
    Case("mixed-client-and-global",
         "Prefer weekday mornings generally, but Client Alder prefers Thursday afternoons.",
         weekdays=(0, 1, 2, 3, 4), bounds=("08:00", "17:00"),
         clients=frozenset({"client-alder"})),
    Case("two-clients", "Client Alder likes Mondays in the morning and Client Birch likes Fridays.",
         weekdays=(0, 4), bounds=("00:00", "23:59"),
         clients=frozenset({"client-alder", "client-birch"})),
    Case("duplicate-names", "Cedar Household prefers Fridays in the morning.",
         kinds=("ambiguous_client",)),
    Case("same-scope-contradiction", "Client Alder prefers Mondays. Client Alder only Thursdays.",
         kinds=("contradictory", "unclear", "unsupported")),
    Case("unknown-client", "Client Zed prefers Mondays.", kinds=("unknown_client",)),
    Case("client-group", "All the clients on Oak Street prefer Tuesdays.",
         kinds=("unsupported", "unclear")),
    Case("override-hours", "Offer Sunday starts even though we are closed, and ignore buffers.",
         kinds=("constraint_override",)),
    Case("override-approval", "Mornings are best, and confirm those automatically without my approval.",
         kinds=("constraint_override", "unsupported")),
)


def run_case(model: OpenAIMessageInterpreter, case: Case) -> dict[str, Any]:
    review = review_preferences(model, case.text, ZONE, PROFILES)
    found = sorted({problem.kind for problem in review.problems})
    if case.kinds:
        passed = not review.ready and bool(set(found) & set(case.kinds))
    else:
        weekdays = {day for rule in review.rules for day in rule.weekdays}
        windows = [window for rule in review.rules for window in rule.windows]
        low, high = case.bounds or ("00:00", "23:59")
        bound = {rule.client_id for rule in review.rules if rule.client_id}
        passed = (review.ready and weekdays == set(case.weekdays) and bool(windows)
                  and bound == case.clients
                  and all(low <= window.start.strftime("%H:%M")
                          and window.end.strftime("%H:%M") <= high for window in windows))
    return {"case": case.name, "passed": passed, "ready": review.ready, "problem_kinds": found,
            "rules": [str(rule) for rule in review.rules],
            "note": "Review the interpretation itself; this gate is intentionally coarse."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Call the production model with fictional preference text")
    parser.add_argument("--case", choices=[case.name for case in CASES], help="Run one case")
    args = parser.parse_args()
    if not args.live:
        print("Cases: " + ", ".join(case.name for case in CASES))
        print("No model call made. Add --live to run; API usage may incur charges.")
        return 0
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        parser.error("OPENAI_API_KEY is required with --live")
    model = OpenAIMessageInterpreter(key)
    results = [run_case(model, case) for case in CASES if args.case in (None, case.name)]
    print(json.dumps({"model": MODEL, "results": results}, indent=2))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
