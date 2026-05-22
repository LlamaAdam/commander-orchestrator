"""auto_fix tier orchestration: tier-1 local, tier-2 Claude fallback,
tier-3 caps, and the already_fixed early-exit. Stubs run_pytest + the
apply step + the router so no real pytest/pip/Ollama/Claude is invoked."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator import auto_fix as af
from orchestrator.router import TaskResult
from conftest import make_failure, make_bundle


# --- stubs ------------------------------------------------------------------

def _pytest_result(n_failed=0, n_errors=0):
    return SimpleNamespace(n_failed=n_failed, n_errors=n_errors)


def _install_action(conf=0.9, pkg="flask"):
    return json.dumps({"action": "install_package", "confidence": conf,
                       "reasoning": "missing module", "package": pkg})


def _escalate_action():
    return json.dumps({"action": "escalate", "confidence": 0.2,
                       "escalate_reason": "no idea"})


class FakeRouter:
    def __init__(self, *, handle, claude=None, blocked=False):
        self._handle = handle
        self._claude = claude
        self.quota = SimpleNamespace(is_blocked=lambda: (blocked, 0.0))
        self.handle_calls = 0
        self.claude_calls = 0

    def handle(self, prompt):
        self.handle_calls += 1
        return self._handle

    def handle_claude_only(self, prompt, reason=""):
        self.claude_calls += 1
        if self._claude is None:
            raise AssertionError("handle_claude_only called unexpectedly")
        return self._claude


@pytest.fixture
def stub_seams(monkeypatch):
    """Stub the I/O seams of auto_fix: bundle build, pytest runs, pip apply."""
    monkeypatch.setattr(af, "bundle_failure", lambda failure, repo_dir: make_bundle())
    # Successful pip install without touching the network.
    monkeypatch.setattr(af, "apply_install_package",
                        lambda *a, **k: af.ApplyResult(success=True))

    seq = {"results": []}

    def fake_run_pytest(repo_dir, lane="fast"):
        return seq["results"].pop(0)

    monkeypatch.setattr(af, "run_pytest", fake_run_pytest)
    return seq


def test_already_fixed_when_baseline_clean(tmp_path, stub_seams):
    stub_seams["results"] = [_pytest_result(n_failed=0)]  # clean baseline
    router = FakeRouter(handle=TaskResult(success=True, handler="local", text=_install_action()))
    attempt = af.auto_fix_one(make_failure(), tmp_path, project_root=tmp_path, router=router)
    assert attempt.status == "already_fixed"
    assert router.handle_calls == 0  # never even asked the model


def test_tier1_local_install_fixes(tmp_path, stub_seams):
    # baseline=1 failure, after-apply=0 -> fixed.
    stub_seams["results"] = [_pytest_result(n_failed=1), _pytest_result(n_failed=0)]
    router = FakeRouter(handle=TaskResult(success=True, handler="local", text=_install_action()))
    attempt = af.auto_fix_one(make_failure(), tmp_path, project_root=tmp_path, router=router)
    assert attempt.status == "fixed"
    assert attempt.handler == "local"
    assert attempt.claude_retry_used is False
    assert router.claude_calls == 0


def test_tier2_claude_retry_after_local_escalates(tmp_path, stub_seams):
    # Local escalates (no pytest run on that path); Claude then proposes a
    # working install -> after-apply pytest improves -> fixed via tier 2.
    stub_seams["results"] = [_pytest_result(n_failed=1), _pytest_result(n_failed=0)]
    router = FakeRouter(
        handle=TaskResult(success=True, handler="local", text=_escalate_action()),
        claude=TaskResult(success=True, handler="claude", text=_install_action()),
    )
    attempt = af.auto_fix_one(make_failure(), tmp_path, project_root=tmp_path, router=router)
    assert attempt.status == "fixed"
    assert attempt.claude_retry_used is True
    assert router.claude_calls == 1


def test_tier2_skipped_when_quota_blocked(tmp_path, stub_seams):
    stub_seams["results"] = [_pytest_result(n_failed=1)]  # only baseline runs
    router = FakeRouter(
        handle=TaskResult(success=True, handler="local", text=_escalate_action()),
        claude=TaskResult(success=True, handler="claude", text=_install_action()),
        blocked=True,
    )
    attempt = af.auto_fix_one(make_failure(), tmp_path, project_root=tmp_path, router=router)
    assert attempt.status == "escalated"
    assert router.claude_calls == 0  # blocked -> no tier-2 call
    # Escalation was recorded for a human.
    assert (tmp_path / "data" / "needs_human.md").exists()


def test_dry_run_reports_would_apply_without_applying(tmp_path, stub_seams):
    # dry_run skips baseline pytest entirely; no apply happens.
    monkeypatch_marker = stub_seams  # results stay empty -> run_pytest must NOT be called
    router = FakeRouter(handle=TaskResult(success=True, handler="local", text=_install_action()))
    attempt = af.auto_fix_one(make_failure(), tmp_path, project_root=tmp_path,
                              router=router, dry_run=True)
    assert attempt.status == "would_apply"
    assert attempt.action.action == "install_package"


def test_tier3_cap_skips_without_attempting(tmp_path, monkeypatch):
    """auto_fix_failures hard-skips a failure that already hit the attempt cap,
    without invoking auto_fix_one at all."""
    failure = make_failure()
    seen_path = tmp_path / "data" / "auto_fix_seen.json"
    seen_path.parent.mkdir(parents=True)
    seen_path.write_text(json.dumps({
        af._dedup_hash(failure): {"attempt_count": af.MAX_FAILED_ATTEMPTS,
                                  "regressions": 0},
    }), encoding="utf-8")

    # If auto_fix_one were reached it would call this -> make it explode.
    monkeypatch.setattr(af, "auto_fix_one",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be capped")))
    router = FakeRouter(handle=None)  # never used on the capped path

    run = af.auto_fix_failures([failure], tmp_path, project_root=tmp_path, router=router)
    assert run.n_skipped_capped == 1
    assert run.attempts[0].status == "skipped_capped"
    # Cap escalation noted once for the human.
    assert (tmp_path / "data" / "needs_human.md").exists()
