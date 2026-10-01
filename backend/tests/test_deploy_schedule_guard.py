"""Offline test of the schedule guard in scripts/dev/deploy-backend.sh (issue #97).

The guard block is extracted from the script and run with stubbed AWS helpers and a
synthetic change set and processed template. Nothing calls AWS.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev/deploy-backend.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq")


def guard_block() -> str:
    text = SCRIPT.read_text()
    start = text.index("# Schedule guard: a deploy")
    end = text.index('if [[ "${COUNT}" -eq 0')
    return text[start:end]


def drift_warning_block() -> str:
    text = SCRIPT.read_text()
    start = text.index("schedule_parameter() {")
    end = text.index('if [[ "${SMOKE_ONLY}" -eq 1 ]]', start)
    return text[start:end]


def run_guard(
    changes: list[dict[str, str]],
    template_rules: dict[str, dict[str, object]],
    params: dict[str, str],
    live: dict[str, str],
    extra: list[str],
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    description = {
        "Changes": [{"ResourceChange": {"ResourceType": "AWS::Events::Rule", **c}} for c in changes],
        "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()],
    }
    processed = {"Resources": {k: {"Type": "AWS::Events::Rule", "Properties": v} for k, v in template_rules.items()}}
    (tmp_path / "live.json").write_text(json.dumps(live))
    (tmp_path / "processed.json").write_text(json.dumps(processed))
    (tmp_path / "description.json").write_text(json.dumps(description))
    prelude = f"""
set -euo pipefail
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
    *) echo "unexpected aws call: $*" >&2; return 1 ;;
  esac
}}
stack_resources() {{ jq -r 'keys[] | [., .] | @tsv' {tmp_path}/live.json; }}
stack_parameter() {{ jq -r --arg k "$1" '.[$k]' {tmp_path}/params.json; }}
"""
    (tmp_path / "params.json").write_text(json.dumps(params))
    return subprocess.run(
        ["bash", "-c", prelude + drift_warning_block() + guard_block() + "warn_schedule_drift\n"],
        capture_output=True, text=True, check=False
    )


HOLD = "HoldExpiryFunctionSweep"
OUTBOX = "OutboxDispatchFunctionSweep"
PARAMS = {"HoldExpiryScheduleState": "ENABLED"}


def modify(logical: str) -> dict[str, str]:
    return {"Action": "Modify", "LogicalResourceId": logical, "PhysicalResourceId": logical}


def test_unchanged_states_pass(tmp_path: Path) -> None:
    result = run_guard(
        [modify(HOLD), modify(OUTBOX)],
        {HOLD: {"State": {"Ref": "HoldExpiryScheduleState"}}, OUTBOX: {"State": "DISABLED"}},
        PARAMS,
        {HOLD: "ENABLED", OUTBOX: "DISABLED"},
        [],
        tmp_path,
    )
    assert result.returncode == 0, result.stdout
    assert f"{HOLD} [Modify]: ENABLED -> ENABLED" in result.stdout


@pytest.mark.parametrize("state", [{"State": "ENABLED"}, {}, {"State": {"Ref": "HoldExpiryScheduleState"}}])
def test_outbox_flip_is_refused_even_with_params(state: dict[str, object], tmp_path: Path) -> None:
    # The outbox rule loses "Enabled: false" (missing State = ENABLED), becomes a literal
    # ENABLED, or is wired to a parameter value of ENABLED: all must be refused.
    result = run_guard(
        [modify(OUTBOX)],
        {OUTBOX: state},
        {"HoldExpiryScheduleState": "ENABLED"},
        {OUTBOX: "DISABLED"},
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
    script = SCRIPT.read_text()
    warning_call = script.index("warn_schedule_drift\n")
    assert warning_call < script.index('if [[ -z "${CHANGESET}" ]]')
    assert warning_call < script.index('if [[ "${CS_STATUS}" == "FAILED" ]]')
    assert warning_call < script.index('if [[ "${COUNT}" -eq 0 ]]')
    result = run_guard([], {}, {"HoldExpiryScheduleState": "ENABLED"}, {HOLD: "DISABLED"}, [], tmp_path)
    assert result.returncode == 0
    assert f"WARNING: {HOLD} is DISABLED live but HoldExpiryScheduleState=ENABLED" in result.stdout
