"""Offline test of the commit guards in scripts/dev/lib.sh (issue #102).

lib.sh is sourced in a temporary git repository whose remote is a local bare repository, with
aws_cli stubbed. Nothing calls AWS or the network.
"""

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts/dev/lib.sh"
# Keep the tests independent of the developer's global git config (signing, fetch.prune).
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, env=GIT_ENV,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    git(tmp_path, "init", "-b", "main", str(work))
    (work / "file.txt").write_text("one\n")
    git(work, "add", "file.txt")
    git(work, "commit", "-m", "first")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-u", "origin", "main")
    return work


def run_lib(repo: Path, body: str) -> subprocess.CompletedProcess[str]:
    script = f"""
set -euo pipefail
aws_cli() {{ echo "unexpected aws call: $*" >&2; return 1; }}
source {LIB}
cd {repo}
{body}
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=GIT_ENV)


def test_clean_and_pushed(repo: Path) -> None:
    result = run_lib(repo, "report_commit 0 1; warn_if_unpushed")
    assert result.returncode == 0, result.stderr
    assert "Working tree is clean." in result.stdout
    assert "WARNING" not in result.stdout


def test_dirty_is_refused(repo: Path) -> None:
    (repo / "file.txt").write_text("changed\n")
    result = run_lib(repo, "report_commit 0 1")
    assert result.returncode == 1
    assert "working tree is dirty" in result.stderr


def test_dirty_allowed_warns(repo: Path) -> None:
    (repo / "file.txt").write_text("changed\n")
    result = run_lib(repo, 'report_commit 1 1; echo "DIRTY=${DIRTY}"')
    assert result.returncode == 0, result.stderr
    assert "uncommitted changes will ship" in result.stdout
    assert "DIRTY=1" in result.stdout


def test_dirty_not_enforced_for_dry_run(repo: Path) -> None:
    (repo / "file.txt").write_text("changed\n")
    result = run_lib(repo, "report_commit 0 0")
    assert result.returncode == 0, result.stderr
    assert "uncommitted changes will ship" in result.stdout


def test_unpushed_warns_without_blocking(repo: Path) -> None:
    (repo / "file.txt").write_text("two\n")
    git(repo, "commit", "-am", "second")
    result = run_lib(repo, "report_commit 0 1; warn_if_unpushed; echo reached")
    assert result.returncode == 0, result.stderr
    assert "is not on GitHub yet" in result.stdout
    assert "reached" in result.stdout


def test_fetch_failure_warns_and_uses_local_refs(repo: Path) -> None:
    git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    pushed = run_lib(repo, "warn_if_unpushed")
    assert pushed.returncode == 0, pushed.stderr
    assert "git fetch failed" in pushed.stdout
    assert "is not on GitHub yet" not in pushed.stdout
    (repo / "file.txt").write_text("two\n")
    git(repo, "commit", "-am", "second")
    unpushed = run_lib(repo, "warn_if_unpushed")
    assert unpushed.returncode == 0
    assert "git fetch failed" in unpushed.stdout
    assert "is not on GitHub yet" in unpushed.stdout


def test_no_remote_warns_without_blocking(tmp_path: Path) -> None:
    work = tmp_path / "solo"
    git(tmp_path, "init", "-b", "main", str(work))
    (work / "f").write_text("x\n")
    git(work, "add", "f")
    git(work, "commit", "-m", "first")
    result = run_lib(work, "warn_if_unpushed; echo reached")
    assert result.returncode == 0, result.stderr
    assert "no git remote is configured" in result.stdout
    assert "is not on GitHub yet" in result.stdout
    assert "reached" in result.stdout


@pytest.mark.skipif(shutil.which("perl") is None, reason="needs perl for the process group")
def test_hanging_fetch_is_killed_with_its_transport(repo: Path, tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    stub = tmp_path / "hang-ssh.sh"
    stub.write_text(f"#!/bin/sh\necho $$ >> {pids}\nsleep 300 &\necho $! >> {pids}\nwait\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    git(repo, "remote", "set-url", "origin", "ssh://git@example.invalid/repo.git")
    started = time.monotonic()
    script = f"export GIT_SSH_COMMAND={stub}; GIT_FETCH_TIMEOUT=1 warn_if_unpushed"
    result = run_lib(repo, script)
    assert time.monotonic() - started < 6
    assert result.returncode == 0, result.stderr
    assert "git fetch failed or timed out" in result.stdout
    recorded = [int(p) for p in pids.read_text().split()]
    assert len(recorded) == 2, "the ssh stub did not start"
    time.sleep(0.5)
    for pid in recorded:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


needs_tools = pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")


@pytest.fixture
def scripted(repo: Path, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A copy of scripts/dev inside the temp repo, with stub aws, sam and curl on PATH."""
    shutil.copytree(ROOT / "scripts/dev", repo / "scripts/dev")
    git(repo, "add", "scripts")
    git(repo, "commit", "-m", "scripts")
    git(repo, "push", "origin", "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(
        '#!/bin/sh\ncase "$*" in *"sts get-caller-identity"*) echo 214965372605 ;; *) echo "stub aws: $*" >&2; exit 1 ;; esac\n'
    )
    for name in ("sam", "curl"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 1\n")
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    env = {**GIT_ENV, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("AWS_REGION", None)
    env.pop("AWS_DEFAULT_REGION", None)
    (repo / "file.txt").write_text("dirty\n")
    return repo, env


def run_script(repo: Path, env: dict[str, str], name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "scripts/dev" / name), "--no-profile", *args], capture_output=True, text=True, check=False, env=env
    )


@needs_tools
@pytest.mark.parametrize("flags", [["--dry-run"], ["--smoke-only", "--dry-run"], ["--allow-dirty", "--dry-run"]])
def test_backend_dry_run_and_smoke_only_skip_dirty_check(
    scripted: tuple[Path, dict[str, str]], flags: list[str]
) -> None:
    repo, env = scripted
    result = run_script(repo, env, "deploy-backend.sh", *flags)
    assert result.returncode == 0, result.stderr
    assert "uncommitted changes will ship" in result.stdout


@needs_tools
def test_backend_smoke_only_is_not_blocked_by_dirty_tree(scripted: tuple[Path, dict[str, str]]) -> None:
    repo, env = scripted
    result = run_script(repo, env, "deploy-backend.sh", "--smoke-only")
    assert "working tree is dirty; commit or stash" not in result.stderr
    assert "uncommitted changes will ship" in result.stdout


@needs_tools
def test_backend_refuses_dirty_tree_unless_allowed(scripted: tuple[Path, dict[str, str]]) -> None:
    repo, env = scripted
    result = run_script(repo, env, "deploy-backend.sh")
    assert result.returncode == 1
    assert "working tree is dirty; commit or stash" in result.stderr


@needs_tools
def test_schedules_passes_allow_dirty_through(scripted: tuple[Path, dict[str, str]]) -> None:
    repo, env = scripted
    with_flag = run_script(repo, env, "schedules.sh", "enable", "HoldExpiryFunctionSweep", "--allow-dirty", "--dry-run")
    assert with_flag.returncode == 0, with_flag.stderr
    assert "deploy-backend.sh --param HoldExpiryScheduleState=ENABLED --dry-run --allow-dirty" in with_flag.stdout
    without = run_script(repo, env, "schedules.sh", "enable", "HoldExpiryFunctionSweep", "--dry-run")
    assert "--allow-dirty" not in without.stdout


@needs_tools
def test_frontend_dry_run_is_not_blocked_by_dirty_tree(scripted: tuple[Path, dict[str, str]]) -> None:
    repo, env = scripted
    (repo / "frontend").mkdir()
    (repo / "frontend/package.json").write_text("{}\n")
    for name in ("zip", "npm"):
        (Path(env["PATH"].split(":")[0]) / name).write_text("#!/bin/sh\nexit 0\n")
        (Path(env["PATH"].split(":")[0]) / name).chmod(0o755)
    result = run_script(repo, env, "deploy-frontend.sh", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "uncommitted changes will ship" in result.stdout
