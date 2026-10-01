"""Offline test of the schedule guard in scripts/dev/deploy-backend.sh (issue #97).

The guard block is extracted from the script and run with stubbed AWS helpers and a
synthetic change set and processed template. Nothing calls AWS.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/deploy-backend.sh"
LIB = ROOT / "scripts/dev/lib.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq")


def guard_block() -> str:
    text = SCRIPT.read_text()
    start = text.index("# Schedule guard: a deploy")
    end = text.index('if [[ "${COUNT}" -eq 0')
    return text[start:end]


def run_guard(
    changes: list[dict[str, str]],
    template_rules: dict[str, dict[str, object]],
    params: dict[str, str],
    live: dict[str, str],
    extra: list[str],
    tmp_path: Path,
    mappings: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    description = {
        "Changes": [
            {"ResourceChange": {"ResourceType": "AWS::Lambda::EventSourceMapping", **c}}
            if "Mapping" in c
            else {"ResourceChange": {"ResourceType": "AWS::Events::Rule", **c}}
            for c in changes
        ],
        "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()],
    }
    processed = {"Resources": {k: {"Type": "AWS::Events::Rule", "Properties": v} for k, v in template_rules.items()}}
    (tmp_path / "mappings.json").write_text(json.dumps(mappings or {}))
    (tmp_path / "live.json").write_text(json.dumps(live))
    (tmp_path / "processed.json").write_text(json.dumps(processed))
    (tmp_path / "description.json").write_text(json.dumps(description))
    prelude = f"""
set -euo pipefail
source {LIB}
die() {{ echo "error: $*"; exit 1; }}
info() {{ echo "$*"; }}
CHANGESET=arn:synthetic
STACK_NAME=synthetic
DESCRIPTION="$(cat {tmp_path}/description.json)"
EXTRA_PARAMS=({" ".join(extra)})
aws_cli() {{
  case "$1 $2" in
    "cloudformation get-template") jq '{{TemplateBody: .}}' {tmp_path}/processed.json | jq .TemplateBody ;;
    "events describe-rule") jq -r --arg n "$4" '.[$n]' {tmp_path}/live.json ;;
    "lambda get-event-source-mapping") jq -r --arg n "$4" '.[$n]' {tmp_path}/mappings.json ;;
    *) echo "unexpected aws call: $*" >&2; return 1 ;;
  esac
}}
stack_resources() {{
  case "$1" in
    AWS::Events::Rule) jq -r 'keys[] | [., .] | @tsv' {tmp_path}/live.json ;;
    *) jq -r 'keys[] | [., .] | @tsv' {tmp_path}/mappings.json ;;
  esac
}}
"""
    return subprocess.run(
        ["bash", "-c", prelude + guard_block()], capture_output=True, text=True, check=False
    )


HOLD = "HoldExpiryFunctionSweep"
OUTBOX = "OutboxDispatchFunctionSweep"
HARDCODED = "NewFunctionSweep"
SENDER = "SmsSenderFunctionOutbox"
PARAMS = {"HoldExpiryScheduleState": "ENABLED"}


def modify(logical: str) -> dict[str, str]:
    return {"Action": "Modify", "LogicalResourceId": logical, "PhysicalResourceId": logical}


def test_unchanged_states_pass(tmp_path: Path) -> None:
    result = run_guard(
        [modify(HOLD), modify(HARDCODED)],
        {HOLD: {"State": {"Ref": "HoldExpiryScheduleState"}}, HARDCODED: {"State": "DISABLED"}},
        PARAMS,
        {HOLD: "ENABLED", HARDCODED: "DISABLED"},
        [],
        tmp_path,
    )
    assert result.returncode == 0, result.stdout
    assert f"{HOLD} [Modify]: ENABLED -> ENABLED" in result.stdout


@pytest.mark.parametrize("state", [{"State": "ENABLED"}, {}, {"State": {"Ref": "HoldExpiryScheduleState"}}])
def test_unparameterized_rule_flip_is_refused_even_with_params(state: dict[str, object], tmp_path: Path) -> None:
    # A rule without its own parameter loses its DISABLED State (missing State = ENABLED), becomes
    # a literal ENABLED, or is wired to another parameter's ENABLED: all must be refused.
    result = run_guard(
        [modify(HARDCODED)],
        {HARDCODED: state},
        {"HoldExpiryScheduleState": "ENABLED"},
        {HARDCODED: "DISABLED"},
        ["HoldExpiryScheduleState=ENABLED"],
        tmp_path,
    )
    assert result.returncode == 1
    assert "REFUSED: hard-coded rule must stay DISABLED" in result.stdout


def test_added_rule_must_target_disabled(tmp_path: Path) -> None:
    added = {"Action": "Add", "LogicalResourceId": "NewFunctionSweep"}
    result = run_guard([added], {"NewFunctionSweep": {}}, {}, {}, [], tmp_path)
    assert result.returncode == 1
    assert "NewFunctionSweep [Add]: (new) -> ENABLED" in result.stdout


def test_parameter_state_change_needs_explicit_param(tmp_path: Path) -> None:
    template = {HOLD: {"State": {"Ref": "HoldExpiryScheduleState"}}}
    refused = run_guard([modify(HOLD)], template, {"HoldExpiryScheduleState": "DISABLED"}, {HOLD: "ENABLED"}, [], tmp_path)
    assert refused.returncode == 1
    allowed = run_guard(
        [modify(HOLD)], template, {"HoldExpiryScheduleState": "DISABLED"}, {HOLD: "ENABLED"},
        ["HoldExpiryScheduleState=DISABLED"], tmp_path,
    )
    assert allowed.returncode == 0, allowed.stdout


def test_drift_warning_on_unmodified_rule(tmp_path: Path) -> None:
    result = run_guard([], {}, {"HoldExpiryScheduleState": "ENABLED"}, {HOLD: "DISABLED"}, [], tmp_path)
    assert result.returncode == 0
    assert f"WARNING: {HOLD} is DISABLED live but HoldExpiryScheduleState=ENABLED" in result.stdout


def test_outbox_dispatch_is_parameterized(tmp_path: Path) -> None:
    template = {OUTBOX: {"State": {"Ref": "OutboxDispatchScheduleState"}}}
    kept = run_guard([modify(OUTBOX)], template, {"OutboxDispatchScheduleState": "DISABLED"}, {OUTBOX: "DISABLED"}, [], tmp_path)
    assert kept.returncode == 0, kept.stdout
    assert f"{OUTBOX} [Modify]: DISABLED -> DISABLED" in kept.stdout
    flipped = run_guard([modify(OUTBOX)], template, {"OutboxDispatchScheduleState": "ENABLED"}, {OUTBOX: "DISABLED"}, [], tmp_path)
    assert flipped.returncode == 1
    assert "REFUSED: state would change without --param OutboxDispatchScheduleState" in flipped.stdout
    allowed = run_guard(
        [modify(OUTBOX)], template, {"OutboxDispatchScheduleState": "ENABLED"}, {OUTBOX: "DISABLED"},
        ["OutboxDispatchScheduleState=ENABLED"], tmp_path,
    )
    assert allowed.returncode == 0, allowed.stdout


def mapping_change(action: str = "Modify") -> dict[str, str]:
    return {"Action": action, "LogicalResourceId": SENDER, "PhysicalResourceId": SENDER, "Mapping": "yes"}


SENDER_TEMPLATE: dict[str, dict[str, object]] = {SENDER: {"Enabled": {"Fn::If": ["SmsSenderMappingEnabled", True, False]}}}


@pytest.mark.parametrize(("live", "param", "explicit", "code"), [
    ("Disabled", "DISABLED", [], 0),
    ("Enabled", "ENABLED", [], 0),
    ("Disabled", "ENABLED", [], 1),
    ("Enabled", "DISABLED", [], 1),
    ("Disabled", "ENABLED", ["SmsSenderMappingState=ENABLED"], 0),
    ("Enabled", "DISABLED", ["SmsSenderMappingState=DISABLED"], 0),
])
def test_sender_mapping_guard(live: str, param: str, explicit: list[str], code: int, tmp_path: Path) -> None:
    result = run_guard(
        [mapping_change()], SENDER_TEMPLATE, {"SmsSenderMappingState": param}, {}, explicit, tmp_path,
        mappings={SENDER: live},
    )
    assert result.returncode == code, result.stdout
    if code:
        assert "REFUSED: state would change without --param SmsSenderMappingState" in result.stdout


def test_sender_mapping_literal_enabled_is_refused(tmp_path: Path) -> None:
    # The condition is dropped and Enabled hard-coded true: must not pass while live is Disabled.
    result = run_guard(
        [mapping_change()], {SENDER: {"Enabled": True}}, {"SmsSenderMappingState": "DISABLED"}, {}, [], tmp_path,
        mappings={SENDER: "Disabled"},
    )
    assert result.returncode == 1


def test_sender_mapping_drift_warning(tmp_path: Path) -> None:
    result = run_guard([], {}, {"SmsSenderMappingState": "ENABLED"}, {}, [], tmp_path, mappings={SENDER: "Disabled"})
    assert result.returncode == 0, result.stdout
    assert f"WARNING: {SENDER} is DISABLED live but SmsSenderMappingState=ENABLED" in result.stdout
