"""Manually run one synthetic, multi-text scenario through the local simulator.

This file is outside pytest and CI. OpenAI calls happen only from ``main`` after
``--live`` and ``OPENAI_API_KEY`` are supplied. Nothing reaches Twilio or AWS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.domain.conversation import MessageInterpreter
from scheduling.local_owner import create_local_owner_app

TOKEN = "local-scenario-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REQUESTS_URL = "/v1/owner/businesses/pilot/requests"
CALENDAR_URL = "/v1/owner/businesses/pilot/calendar"
PARTIES = frozenset({"owner", "client-1", "client-2", "client-3"})
CALENDAR_STATUSES = frozenset({"CONFIRMED", "PENDING_APPROVAL", "UNAVAILABLE"})
MAX_STEPS = 20
PENDING_REF = re.compile(r"\{\{pending_ref:(client-[1-3])\}\}")


@dataclass(frozen=True)
class Step:
    party: str
    body: str
    advance_minutes: int
    out_contains: tuple[str, ...]
    pending_for: dict[str, int]
    calendar_statuses: dict[str, int]


@dataclass(frozen=True)
class Scenario:
    name: str
    start_at: datetime
    steps: tuple[Step, ...]


def _object(value: Any, label: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"{label} must be an object with only {sorted(allowed)}")
    return value


def load_scenario(path: Path) -> Scenario:
    raw = _object(json.loads(path.read_text(encoding="utf-8")), "Scenario",
                  {"name", "start_at", "steps"})
    if not isinstance(raw.get("name"), str) or not raw["name"].strip():
        raise ValueError("Scenario needs a name")
    if not isinstance(raw.get("start_at"), str):
        raise TypeError("Scenario needs start_at with a timezone offset")
    start_at = datetime.fromisoformat(raw["start_at"])
    if start_at.tzinfo is None or start_at.utcoffset() is None:
        raise ValueError("Scenario start_at needs a timezone offset")
    rows = raw.get("steps")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_STEPS:
        raise ValueError(f"Scenario needs 1 to {MAX_STEPS} steps")
    steps: list[Step] = []
    for number, row in enumerate(rows, 1):
        item = _object(row, f"Step {number}",
                       {"party", "body", "advance_minutes", "expect"})
        party, body = item.get("party"), item.get("body")
        if isinstance(party, str):
            party = party.lower()
        if (not isinstance(party, str) or party not in PARTIES
                or not isinstance(body, str) or not body.strip()):
            raise ValueError(f"Step {number} needs a synthetic party and nonblank body")
        advance = item.get("advance_minutes", 0)
        if type(advance) is not int or advance < 0:
            raise ValueError(f"Step {number} advance_minutes must be a nonnegative integer")
        expected = _object(item.get("expect", {}), f"Step {number} expect",
                           {"out_contains", "pending_for", "calendar_statuses"})
        contains = expected.get("out_contains", [])
        if (not isinstance(contains, list)
                or any(not isinstance(part, str) or not part for part in contains)):
            raise ValueError(f"Step {number} out_contains must be a list of nonempty strings")
        pending = _object(expected.get("pending_for", {}), f"Step {number} pending_for",
                          set(PARTIES) - {"owner"})
        if any(type(count) is not int or count < 0 for count in pending.values()):
            raise ValueError(f"Step {number} pending_for counts must be nonnegative integers")
        statuses = _object(expected.get("calendar_statuses", {}),
                           f"Step {number} calendar_statuses", set(CALENDAR_STATUSES))
        if any(type(count) is not int or count < 0 for count in statuses.values()):
            raise ValueError(f"Step {number} calendar_statuses counts must be nonnegative integers")
        steps.append(Step(party, body, advance, tuple(contains), pending, statuses))
    return Scenario(raw["name"], start_at, tuple(steps))


def _resolve_body(client: TestClient, body: str) -> str:
    if "{{" not in body:
        return body
    response = client.get(REQUESTS_URL, headers=AUTH)
    if response.status_code != 200:
        raise ValueError(f"request read returned HTTP {response.status_code}")
    requests = response.json()

    def reference(match: re.Match[str]) -> str:
        client_id = match.group(1)
        matching = [request for request in requests if request["client_id"] == client_id]
        if len(matching) != 1:
            raise ValueError(f"{client_id} has {len(matching)} pending requests; expected one")
        return str(matching[0]["appointment_id"])[:8]

    resolved = PENDING_REF.sub(reference, body)
    if "{{" in resolved:
        raise ValueError("Unknown scenario placeholder")
    return resolved


def run_scenario(scenario: Scenario, interpreter: MessageInterpreter) -> bool:
    clock = [scenario.start_at]
    app = create_local_owner_app(TOKEN, interpreter=interpreter,
                                 clock=lambda: clock[0])
    passed = True
    with TestClient(app) as client:
        print(f"Scenario: {scenario.name} ({scenario.start_at.isoformat()})")
        for number, step in enumerate(scenario.steps, 1):
            clock[0] += timedelta(minutes=step.advance_minutes)
            try:
                body = _resolve_body(client, step.body)
            except ValueError as exc:
                print(f"\n{number}. FAIL: {exc}")
                return False
            print(f"\n{number}. {step.party} at {clock[0].isoformat()}: {body}")
            response = client.post("/local/texts", headers=AUTH,
                                   json={"party": step.party, "body": body})
            if response.status_code != 200:
                print(f"  FAIL: simulator returned HTTP {response.status_code}: {response.text}")
                return False
            messages = response.json()["messages"]
            outgoing = [message["body"] for message in messages if message["direction"] == "out"]
            for message in messages:
                if message["direction"] != "in":
                    print(f"  {message['party']} {message['kind']}: {message['body']}")
            requests = client.get(REQUESTS_URL, headers=AUTH)
            if requests.status_code != 200:
                print(f"  FAIL: request read returned HTTP {requests.status_code}")
                return False
            pending: dict[str, int] = {}
            for request in requests.json():
                client_id = request["client_id"]
                pending[client_id] = pending.get(client_id, 0) + 1
            print(f"  Pending requests: {pending}")
            calendar = client.get(CALENDAR_URL, headers=AUTH)
            if calendar.status_code != 200:
                print(f"  FAIL: calendar read returned HTTP {calendar.status_code}")
                return False
            statuses: dict[str, int] = {}
            for event in calendar.json()["events"]:
                status = event["status"]
                statuses[status] = statuses.get(status, 0) + 1
            print(f"  Calendar statuses: {statuses}")
            failures = [f"outbound text did not contain {part!r}"
                        for part in step.out_contains
                        if not any(part in body for body in outgoing)]
            failures += [f"{party} had {pending.get(party, 0)} pending requests, expected {count}"
                         for party, count in step.pending_for.items()
                         if pending.get(party, 0) != count]
            failures += [f"calendar had {statuses.get(status, 0)} {status} events, expected {count}"
                         for status, count in step.calendar_statuses.items()
                         if statuses.get(status, 0) != count]
            for failure in failures:
                print(f"  FAIL: {failure}")
            passed = passed and not failures
    print(f"\n{'PASS' if passed else 'FAIL'}: {scenario.name}")
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path, help="JSON file of fictional texts and checks")
    parser.add_argument("--live", action="store_true", help="Allow OpenAI API calls for this run")
    args = parser.parse_args(argv)
    try:
        scenario = load_scenario(args.scenario)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid scenario: {exc}", file=sys.stderr)
        return 2
    key = os.environ.get("OPENAI_API_KEY")
    if not args.live or not key:
        print("A live run requires --live and OPENAI_API_KEY; no model call was made.",
              file=sys.stderr)
        return 2
    return 0 if run_scenario(scenario, OpenAIMessageInterpreter(key)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
