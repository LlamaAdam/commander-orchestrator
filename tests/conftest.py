"""Shared fixtures + factories for the orchestrator test suite.

All tests are offline: no real Ollama, Claude CLI, or network. The git
fixtures use a real local repo in tmp_path (git is fast + deterministic),
created with an INLINE identity so they never depend on (or mutate) the
host's git config.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from orchestrator.harness.runner import TestFailure
from orchestrator.harness.failure import FailureBundle


# --- failure / bundle factories --------------------------------------------

def make_failure(**overrides) -> TestFailure:
    """A plausible TestFailure; override any field via kwargs."""
    base = dict(
        nodeid="tests/test_mod.py::test_thing",
        classname="tests.test_mod",
        name="test_thing",
        file="tests/test_mod.py",
        line=12,
        failure_type="failure",
        message="AssertionError: nope",
        traceback="Traceback (most recent call last):\n  ...\nAssertionError: nope",
    )
    base.update(overrides)
    return TestFailure(**base)


def make_bundle(prompt: str = "FAILURE BUNDLE PROMPT", **failure_overrides) -> FailureBundle:
    return FailureBundle(
        failure=make_failure(**failure_overrides),
        test_source="def test_thing(): assert False",
        related_sources={},
        prompt=prompt,
    )


@pytest.fixture
def failure():
    return make_failure()


@pytest.fixture
def bundle():
    return make_bundle()


# --- fake model results (duck-typed) ---------------------------------------

def fake_claude_result(*, success=True, text="ok", error="", error_type="",
                       input_tokens=10, output_tokens=20, total_cost_usd=0.01,
                       duration_seconds=0.5, retry_after_seconds=None):
    """Duck-typed stand-in for claude_cli.ClaudeResult."""
    return SimpleNamespace(
        success=success, text=text, error=error, error_type=error_type,
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_cost_usd=total_cost_usd, duration_seconds=duration_seconds,
        retry_after_seconds=retry_after_seconds,
    )


def fake_local_result(*, success=True, text="ok", error="",
                      prompt_eval_count=5, eval_count=7, duration_seconds=0.2):
    """Duck-typed stand-in for local_model.GenerateResult."""
    return SimpleNamespace(
        success=success, text=text, error=error,
        prompt_eval_count=prompt_eval_count, eval_count=eval_count,
        duration_seconds=duration_seconds,
    )


# --- git repo fixture -------------------------------------------------------

def _git(args, cwd, check=True):
    # Inline identity so these tests never touch the host's git config.
    ident = ["-c", "user.email=test@local", "-c", "user.name=test"]
    proc = subprocess.run(
        ["git", *ident, *args], cwd=str(cwd),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with one committed file (src.py) on branch 'main'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n",
                                              encoding="utf-8")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


@pytest.fixture
def git_helper():
    """Expose the inline-identity git runner to tests that need it."""
    return _git
