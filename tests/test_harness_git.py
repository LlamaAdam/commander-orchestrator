"""harness.git_ops.clone_or_update — repo acquisition (real local git)."""
from __future__ import annotations

import subprocess

import pytest

from orchestrator.harness.git_ops import clone_or_update, short_sha


def _git(args, cwd):
    ident = ["-c", "user.email=test@local", "-c", "user.name=test"]
    p = subprocess.run(["git", *ident, *args], cwd=str(cwd),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p


@pytest.fixture
def source_repo(tmp_path):
    """A normal (non-bare) repo on branch 'main' with one commit, usable as a
    clone source via its filesystem path."""
    src = tmp_path / "source"
    src.mkdir()
    _git(["init", "-b", "main"], cwd=src)
    (src / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], cwd=src)
    _git(["commit", "-m", "init"], cwd=src)
    return src


def test_short_sha():
    assert short_sha("abcdef0123456789", 8) == "abcdef01"
    assert short_sha(None) == ""
    assert short_sha("") == ""


def test_fresh_clone(source_repo, tmp_path):
    target = tmp_path / "clone"
    r = clone_or_update(str(source_repo), target, branch="main")
    assert r.success, r.error
    assert r.operation == "clone"
    assert (target / ".git").exists()
    assert (target / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert r.head_sha and len(r.head_sha) >= 7


def test_idempotent_update_on_existing_clone(source_repo, tmp_path):
    target = tmp_path / "clone"
    first = clone_or_update(str(source_repo), target, branch="main")
    assert first.success

    # A new commit on the source...
    (source_repo / "README.md").write_text("hello world\n", encoding="utf-8")
    _git(["add", "-A"], cwd=source_repo)
    _git(["commit", "-m", "update"], cwd=source_repo)

    # ...is pulled by re-running clone_or_update (existing-clone fetch+pull path).
    second = clone_or_update(str(source_repo), target, branch="main")
    assert second.success, second.error
    assert second.operation == "pull"
    assert (target / "README.md").read_text(encoding="utf-8") == "hello world\n"
    assert second.head_sha != first.head_sha


def test_clone_failure_surfaces_error(tmp_path):
    target = tmp_path / "clone"
    r = clone_or_update(str(tmp_path / "does_not_exist"), target, branch="main")
    assert r.success is False
    assert r.operation == "clone"
    assert r.error and "clone" in r.error.lower()
