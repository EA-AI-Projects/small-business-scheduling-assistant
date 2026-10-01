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
    mappings: dict[str, str] | None = None,
    conditions: dict[str, object] | None = None,
    auth: bool = False,
    live_params: dict[str, str] | None = None,
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
    processed = {
        "Resources": {k: {"Type": "AWS::Events::Rule", "Properties": v} for k, v in template_rules.items()},
        "Conditions": GOOD_CONDITIONS if conditions is None else conditions,
    }
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
LIVE_SMS_AUTH={1 if auth else 0}
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
stack_parameter() {{
  local v
  v="$(jq -r --arg k "$1" '.[$k] // empty' {tmp_path}/live_params.json)"
  [[ -n "$v" ]] || die "stack parameter $1 not found on synthetic"
  printf '%s' "$v"
}}
stack_query() {{
  jq -rn --arg q "$1" --slurpfile p {tmp_path}/live_params.json \
    '($q | capture("ParameterKey==.(?<k>[A-Za-z0-9]+).").k) as $k | $p[0][$k] // "None"'
}}
"""
    (tmp_path / "live_params.json").write_text(json.dumps(params if live_params is None else live_params))
    return subprocess.run(
        ["bash", "-c", prelude + drift_warning_block() + guard_block() + "warn_schedule_drift\n"],
        capture_output=True, text=True, check=False
    )


GOOD_CONDITIONS: dict[str, object] = {
    "SmsSenderMappingEnabled": {"Fn::Equals": [{"Ref": "SmsSenderMappingState"}, "ENABLED"]}
}
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
    script = SCRIPT.read_text()
    warning_call = script.index("warn_schedule_drift\n")
    assert warning_call < script.index('if [[ -z "${CHANGESET}" ]]')
    assert warning_call < script.index('if [[ "${CS_STATUS}" == "FAILED" ]]')
    assert warning_call < script.index('if [[ "${COUNT}" -eq 0 ]]')
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
        ["OutboxDispatchScheduleState=ENABLED"], tmp_path, auth=True,
    )
    assert allowed.returncode == 0, allowed.stdout
    no_auth = run_guard(
        [modify(OUTBOX)], template, {"OutboxDispatchScheduleState": "ENABLED"}, {OUTBOX: "DISABLED"},
        ["OutboxDispatchScheduleState=ENABLED"], tmp_path,
    )
    assert no_auth.returncode == 1
    assert "--i-have-live-sms-authorization" in no_auth.stdout


def test_wrong_param_value_is_not_authorization(tmp_path: Path) -> None:
    # --param is given, but its value is not the target the change set resolves to.
    template = {HOLD: {"State": {"Ref": "HoldExpiryScheduleState"}}}
    result = run_guard(
        [modify(HOLD)], template, {"HoldExpiryScheduleState": "DISABLED"}, {HOLD: "ENABLED"},
        ["HoldExpiryScheduleState=ENABLED"], tmp_path,
    )
    assert result.returncode == 1
    assert "REFUSED" in result.stdout


@pytest.mark.parametrize("state", [
    {"State": "ENABLED"},
    {"State": "DISABLED"},
    {},
    {"State": {"Ref": "HoldExpiryScheduleState"}},
    {"State": {"Fn::If": ["X", "ENABLED", "DISABLED"]}},
])
def test_parameterized_rule_must_be_wired_to_its_own_parameter(state: dict[str, object], tmp_path: Path) -> None:
    result = run_guard(
        [modify(OUTBOX)], {OUTBOX: state}, {"HoldExpiryScheduleState": "ENABLED", "OutboxDispatchScheduleState": "DISABLED"},
        {OUTBOX: "DISABLED"}, ["OutboxDispatchScheduleState=DISABLED"], tmp_path, auth=True,
    )
    assert result.returncode == 1
    assert "cannot resolve the target State" in result.stdout


def mapping_change(action: str = "Modify") -> dict[str, str]:
    return {"Action": action, "LogicalResourceId": SENDER, "PhysicalResourceId": SENDER, "Mapping": "yes"}


SENDER_TEMPLATE: dict[str, dict[str, object]] = {SENDER: {"Enabled": {"Fn::If": ["SmsSenderMappingEnabled", True, False]}}}
RECEIPTS = "SmsConversationFunctionReceipts"


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
        mappings={SENDER: live}, auth=param == "ENABLED" and (code == 0 or bool(explicit)),
    )
    assert result.returncode == code, result.stdout
    if code:
        assert "REFUSED" in result.stdout


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


def test_sender_mapping_enabled_needs_authorization_flag(tmp_path: Path) -> None:
    result = run_guard(
        [mapping_change()], SENDER_TEMPLATE, {"SmsSenderMappingState": "ENABLED"}, {},
        ["SmsSenderMappingState=ENABLED"], tmp_path, mappings={SENDER: "Disabled"},
    )
    assert result.returncode == 1
    assert "--i-have-live-sms-authorization" in result.stdout


def test_sender_mapping_wrong_param_value_is_not_authorization(tmp_path: Path) -> None:
    result = run_guard(
        [mapping_change()], SENDER_TEMPLATE, {"SmsSenderMappingState": "DISABLED"}, {},
        ["SmsSenderMappingState=ENABLED"], tmp_path, mappings={SENDER: "Enabled"}, auth=True,
    )
    assert result.returncode == 1


@pytest.mark.parametrize("template", [
    {SENDER: {"Enabled": {"Fn::If": ["SmsSenderMappingEnabled", False, True]}}},
    {SENDER: {"Enabled": {"Fn::If": ["OtherCondition", True, False]}}},
    {SENDER: {"Enabled": "false"}},
    {SENDER: {}},
])
def test_sender_mapping_miswired_enabled_is_refused(template: dict[str, dict[str, object]], tmp_path: Path) -> None:
    result = run_guard(
        [mapping_change()], template, {"SmsSenderMappingState": "DISABLED"}, {}, ["SmsSenderMappingState=DISABLED"],
        tmp_path, mappings={SENDER: "Disabled"}, auth=True,
    )
    assert result.returncode == 1
    assert "cannot resolve the target state" in result.stdout


@pytest.mark.parametrize("conditions", [
    {},
    {"SmsSenderMappingEnabled": {"Fn::Equals": [{"Ref": "SmsSenderMappingState"}, "DISABLED"]}},
    {"SmsSenderMappingEnabled": {"Fn::Equals": [{"Ref": "OtherParameter"}, "ENABLED"]}},
    {"SmsSenderMappingEnabled": {"Fn::Not": [{"Fn::Equals": [{"Ref": "SmsSenderMappingState"}, "ENABLED"]}]}},
])
def test_sender_mapping_miswired_condition_is_refused(conditions: dict[str, object], tmp_path: Path) -> None:
    result = run_guard(
        [mapping_change()], SENDER_TEMPLATE, {"SmsSenderMappingState": "DISABLED"}, {}, ["SmsSenderMappingState=DISABLED"],
        tmp_path, mappings={SENDER: "Disabled"}, conditions=conditions, auth=True,
    )
    assert result.returncode == 1
    assert "cannot resolve the target state" in result.stdout


@pytest.mark.parametrize("state", ["Enabling", "Disabling", "Updating", "Creating", ""])
def test_sender_mapping_unsettled_live_state_is_refused(state: str, tmp_path: Path) -> None:
    result = run_guard(
        [mapping_change()], SENDER_TEMPLATE, {"SmsSenderMappingState": "DISABLED"}, {}, [], tmp_path,
        mappings={SENDER: state},
    )
    assert result.returncode == 1


def test_other_mapping_must_be_disabled(tmp_path: Path) -> None:
    other = {"Action": "Add", "LogicalResourceId": "NewFunctionQueue", "Mapping": "yes"}
    refused = run_guard([other], {"NewFunctionQueue": {"Enabled": True}}, {}, {}, [], tmp_path)
    assert refused.returncode == 1
    assert "other mappings must stay DISABLED" in refused.stdout
    missing = run_guard([other], {"NewFunctionQueue": {}}, {}, {}, [], tmp_path)
    assert missing.returncode == 1
    ok = run_guard([other], {"NewFunctionQueue": {"Enabled": False}}, {}, {}, [], tmp_path)
    assert ok.returncode == 0, ok.stdout


def test_conversation_mapping_exact_wiring_is_governed_by_its_condition(tmp_path: Path) -> None:
    change = {"Action": "Modify", "LogicalResourceId": RECEIPTS, "PhysicalResourceId": RECEIPTS, "Mapping": "yes"}
    template = {RECEIPTS: {"Enabled": {"Fn::If": ["SmsConversationRequested", True, False]}}}
    result = run_guard([change], template, {}, {}, [], tmp_path)
    assert result.returncode == 0, result.stdout
    assert "governed by SmsConversationRequested" in result.stdout


@pytest.mark.parametrize("enabled", [
    {"Fn::If": ["SmsConversationRequested", False, True]},
    {"Fn::If": ["OtherCondition", True, False]},
    True,
    False,
    "true",
])
def test_conversation_mapping_other_wiring_is_refused(enabled: object, tmp_path: Path) -> None:
    change = {"Action": "Modify", "LogicalResourceId": RECEIPTS, "PhysicalResourceId": RECEIPTS, "Mapping": "yes"}
    result = run_guard([change], {RECEIPTS: {"Enabled": enabled}}, {}, {}, [], tmp_path)
    assert result.returncode == 1
    assert "cannot resolve the target state" in result.stdout


def test_conversation_mapping_missing_enabled_is_refused(tmp_path: Path) -> None:
    change = {"Action": "Modify", "LogicalResourceId": RECEIPTS, "PhysicalResourceId": RECEIPTS, "Mapping": "yes"}
    result = run_guard([change], {RECEIPTS: {}}, {}, {}, [], tmp_path)
    assert result.returncode == 1


def test_no_change_drift_warning_covers_outbox_dispatch(tmp_path: Path) -> None:
    result = run_guard([], {}, {"OutboxDispatchScheduleState": "ENABLED"}, {OUTBOX: "DISABLED"}, [], tmp_path)
    assert result.returncode == 0, result.stdout
    assert f"WARNING: {OUTBOX} is DISABLED live but OutboxDispatchScheduleState=ENABLED" in result.stdout


def test_first_deploy_with_new_parameters_missing_live_proceeds(tmp_path: Path) -> None:
    # Neither new parameter is on the live stack yet; both are passed as DISABLED, which matches
    # the live DISABLED rule and mapping. The pre-change-set drift check must not die.
    result = run_guard(
        [modify(OUTBOX), mapping_change()],
        {OUTBOX: {"State": {"Ref": "OutboxDispatchScheduleState"}}, **SENDER_TEMPLATE},
        {"OutboxDispatchScheduleState": "DISABLED", "SmsSenderMappingState": "DISABLED"},
        {OUTBOX: "DISABLED"},
        ["OutboxDispatchScheduleState=DISABLED", "SmsSenderMappingState=DISABLED"],
        tmp_path,
        mappings={SENDER: "Disabled"},
        live_params={"HoldExpiryScheduleState": "ENABLED"},
    )
    assert result.returncode == 0, result.stdout
    assert "WARNING" not in result.stdout
    assert f"{OUTBOX} [Modify]: DISABLED -> DISABLED" in result.stdout
