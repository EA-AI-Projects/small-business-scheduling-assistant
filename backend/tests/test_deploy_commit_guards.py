"""Offline test of the commit guards in scripts/dev/lib.sh (issue #102).

lib.sh is sourced in a temporary git repository whose remote is a local bare repository, with
aws_cli stubbed. Nothing calls AWS or the network.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "scripts/dev/lib.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True,
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
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


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
