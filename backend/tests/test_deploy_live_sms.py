"""Offline deployment authorization checks; every AWS helper is replaced locally."""
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/deploy-backend.sh"
LIB = ROOT / "scripts/dev/lib.sh"
GATES = ("VisitReminderDeliveryEnabled", "BookingInvitationDeliveryEnabled",
         "ManualInvitationDeliveryEnabled")
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq")


def delivery_guard(tmp_path: Path, live: dict[str, str], target: dict[str, str],
                   *, authorized: bool, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    text = SCRIPT.read_text()
    block = text[text.index("LIVE_PARAMS="):text.index('info "Parameters: unchanged from the live stack."')]
    live_file = tmp_path / "live.json"
    target_file = tmp_path / "target.json"
    live_file.write_text(json.dumps([{"ParameterKey": k, "ParameterValue": v} for k, v in live.items()]))
    target_file.write_text(json.dumps({"Parameters": [
        {"ParameterKey": k, "ParameterValue": v} for k, v in target.items()]}))
    prelude = f"""
set -euo pipefail
source {shlex.quote(str(LIB))}
die() {{ echo "error: $*"; exit 1; }}
STACK_NAME=synthetic
DESCRIPTION="$(cat {shlex.quote(str(target_file))})"
EXTRA_PARAMS=({' '.join(map(shlex.quote, extra))})
PROMPT_KEYS=()
LIVE_SMS_AUTH={int(authorized)}
aws_cli() {{ cat {shlex.quote(str(live_file))}; }}
"""
    return subprocess.run(["bash", "-c", prelude + block],
                          capture_output=True, text=True, check=False)


def test_one_authorization_preserves_all_existing_delivery_gates(tmp_path: Path) -> None:
    settings = {**dict.fromkeys(GATES, "authorized"), "SmsRetentionScheduleState": "ENABLED"}
    result = delivery_guard(tmp_path, settings, settings, authorized=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("gate", GATES)
def test_authorized_delivery_still_requires_live_sms_flag(gate: str, tmp_path: Path) -> None:
    settings = {gate: "authorized", "SmsRetentionScheduleState": "ENABLED"}
    result = delivery_guard(tmp_path, settings, settings, authorized=False)
    assert result.returncode == 1
    assert "authorization" in result.stdout


@pytest.mark.parametrize("gate", GATES)
def test_live_sms_does_not_authorize_parameter_changes(gate: str, tmp_path: Path) -> None:
    live = {gate: "disabled", "SmsRetentionScheduleState": "ENABLED"}
    target = {**live, gate: "authorized"}
    refused = delivery_guard(tmp_path, live, target, authorized=True)
    assert refused.returncode == 1
    assert "not given with --param" in refused.stdout
    allowed = delivery_guard(tmp_path, live, target, authorized=True,
                             extra=(f"{gate}=authorized",))
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


def test_disabled_delivery_needs_no_authorization(tmp_path: Path) -> None:
    settings = dict.fromkeys(GATES, "disabled")
    assert delivery_guard(tmp_path, settings, settings, authorized=False).returncode == 0
    # The flag alone leaves disabled settings untouched.
    assert delivery_guard(tmp_path, settings, settings, authorized=True).returncode == 0


def test_manual_delivery_still_requires_retention(tmp_path: Path) -> None:
    settings = {"ManualInvitationDeliveryEnabled": "authorized", "SmsRetentionScheduleState": "DISABLED"}
    result = delivery_guard(tmp_path, settings, settings, authorized=True)
    assert result.returncode == 1
    assert "retention schedule enabled" in result.stdout
