"""Offline test of the private-parameter handling in scripts/dev (issue #91).

Covers the format validators in lib.sh, that --param refuses the private keys, that
--prompt-param accepts only those keys, and that the parameter-drift check skips NoEcho
values masked as ****. Nothing calls AWS.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts/dev/lib.sh"
SCRIPT = ROOT / "scripts/dev/deploy-backend.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq")

SID = "AC" + "0123456789abcdef" * 2


def validate(key: str, value: str) -> bool:
    script = f'set -euo pipefail\naws_cli() {{ return 1; }}\nsource {LIB}\nvalid_private_value "$1" "$2"'
    return subprocess.run(["bash", "-c", script, "x", key, value], capture_output=True, check=False).returncode == 0


@pytest.mark.parametrize(("key", "value", "ok"), [
    ("OwnerNumber", "+14155550101", True),
    ("TwilioBusinessNumber", "+14155550102", True),
    ("OwnerNumber", "4155550101", False),
    ("OwnerNumber", "+1 415 555 0101", False),
    ("OwnerNumber", "+0415555010", False),
    ("OwnerNumber", "", False),
    ("OwnerNumber", "+14155550101,+14155550102", False),
    ("AuthorizedSmsRecipients", "+14155550101", True),
    ("AuthorizedSmsRecipients", "+14155550101,+14155550102", True),
    ("AuthorizedSmsRecipients", "", True),
    ("AuthorizedSmsRecipients", "+14155550101,", False),
    ("AuthorizedSmsRecipients", ",+14155550101", False),
    ("AuthorizedSmsRecipients", "+14155550101,,+14155550102", False),
    ("AuthorizedSmsRecipients", "+14155550101, +14155550102", False),
    ("AuthorizedSmsRecipients", "+14155550101;+14155550102", False),
    ("AuthorizedSmsRecipients", "+14155550101\n+14155550102", False),
    ("AuthorizedSmsRecipients", "+14155550101 ", False),
    ("TwilioAccountSid", SID, True),
    ("TwilioAccountSid", "placeholder", False),
    ("TwilioAccountSid", SID[:-1], False),
    ("PermissionsBoundaryArn", "+14155550101", False),
])
def test_validators(key: str, value: str, ok: bool) -> None:
    assert validate(key, value) is ok


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--no-profile", *args], capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL
    )


def test_param_refuses_private_keys() -> None:
    for key in ("OwnerNumber", "AuthorizedSmsRecipients", "TwilioAccountSid", "TwilioBusinessNumber"):
        result = run_script("--dry-run", "--param", f"{key}=+14155550123")
        assert result.returncode == 1
        assert "use --prompt-param" in result.stderr
        assert "+14155550123" not in result.stdout + result.stderr


def test_prompt_param_accepts_only_private_keys() -> None:
    result = run_script("--dry-run", "--prompt-param", "SmsSendEnabled")
    assert result.returncode == 1
    assert "--prompt-param accepts only" in result.stderr


def test_drift_check_skips_masked_noecho_values(tmp_path: Path) -> None:
    text = SCRIPT.read_text()
    block = text[text.index("LIVE_PARAMS=") : text.index('info "Parameters: unchanged from the live stack."')]
    live = [
        {"ParameterKey": "OwnerNumber", "ParameterValue": "****"},
        {"ParameterKey": "AuthorizedSmsRecipients", "ParameterValue": "+14155550101"},
        {"ParameterKey": "SmsSendEnabled", "ParameterValue": "disabled"},
        {"ParameterKey": "BusinessId", "ParameterValue": "biz"},
    ]
    # Change set: OwnerNumber masked (NoEcho), AuthorizedSmsRecipients now masked, one real change.
    description = {
        "Parameters": [
            {"ParameterKey": "OwnerNumber", "ParameterValue": "****"},
            {"ParameterKey": "AuthorizedSmsRecipients", "ParameterValue": "****"},
            {"ParameterKey": "SmsSendEnabled", "ParameterValue": "authorized"},
            {"ParameterKey": "BusinessId", "ParameterValue": "biz"},
        ]
    }
    (tmp_path / "live.json").write_text(json.dumps(live))
    (tmp_path / "d.json").write_text(json.dumps(description))

    def run(extra: str, prompt: str) -> subprocess.CompletedProcess[str]:
        prelude = f"""
set -euo pipefail
die() {{ echo "error: $*"; exit 1; }}
info() {{ echo "$*"; }}
STACK_NAME=s
DESCRIPTION="$(cat {tmp_path}/d.json)"
EXTRA_PARAMS=({extra})
PROMPT_KEYS=({prompt})
aws_cli() {{ cat {tmp_path}/live.json; }}
"""
        return subprocess.run(["bash", "-c", prelude + block], capture_output=True, text=True, check=False)

    refused = run("", "")
    assert refused.returncode == 1
    assert "SmsSendEnabled" in refused.stdout
    assert "OwnerNumber" not in refused.stdout
    assert "AuthorizedSmsRecipients" not in refused.stdout
    assert run("SmsSendEnabled=authorized", "").returncode == 0
    # A prompted key counts as given even when its value would differ.
    assert run("SmsSendEnabled=authorized", "AuthorizedSmsRecipients OwnerNumber").returncode == 0


def test_empty_override_is_quoted_for_sam() -> None:
    script = f'aws_cli() {{ return 1; }}\nsource {LIB}\nprivate_override AuthorizedSmsRecipients ""\nprivate_override OwnerNumber +14155550101'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False).stdout
    assert out == 'AuthorizedSmsRecipients=""OwnerNumber=+14155550101'


@pytest.mark.parametrize("arg", [
    "ParameterKey=OwnerNumber,ParameterValue=+14155550123",
    "SmsSendEnabled=a b",
    "OwnerNumber =+14155550123",
    "=x",
    "Key",
    "Some-Key=x",
])
def test_param_format_is_strict(arg: str) -> None:
    result = run_script("--dry-run", "--param", arg)
    assert result.returncode == 1
    assert "use Key=Value" in result.stderr or "--param needs Key=Value" in result.stderr
    assert "+14155550123" not in result.stdout + result.stderr


def test_dry_run_prompt_param_prints_stars_only() -> None:
    # aws and sam are stubbed on PATH to fail, so nothing reaches AWS.
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        for tool in ("aws", "sam"):
            stub = Path(d) / tool
            stub.write_text("#!/bin/sh\nexit 255\n")
            stub.chmod(0o755)
        env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}
        result = subprocess.run(
            ["bash", str(SCRIPT), "--no-profile", "--dry-run", "--prompt-param", "OwnerNumber",
             "--prompt-param", "AuthorizedSmsRecipients", "--prompt-param", "TwilioAccountSid"],
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, env=env,
        )
    assert result.returncode == 0, result.stderr
    assert "OwnerNumber=\\*\\*\\*\\*" in result.stdout
    assert "AuthorizedSmsRecipients=\\*\\*\\*\\*" in result.stdout
    assert "Value for" not in result.stdout + result.stderr
