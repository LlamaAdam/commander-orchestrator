"""Offline smoke test for verify-then-graduate.

Stubs pytest / pip / git / Router. Verifies:
  1. verify approves -> local fix applies -> graduation counter bumps
  2. verify rejects -> escalated -> tier-2 Claude takes over
  3. graduated action-type -> verification call is skipped
  4. quota-blocked Claude -> verification skipped, applies best-effort
  5. record_verified_success crosses the threshold exactly once

Run: python scripts/smoke_verify_graduate.py
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestrator import auto_fix
from orchestrator.harness import TestFailure
from orchestrator.router import TaskResult


def _fake_failure(nodeid="tests.fake::test_x"):
    return TestFailure(
        nodeid=nodeid, classname="tests.fake", name=nodeid.split("::")[-1],
        file="", line=None, failure_type="error",
        message='ModuleNotFoundError: No module named "pandas"',
        traceback="missing pandas",
    )


class _FakeBundle:
    def __init__(self, failure):
        self.failure = failure
        self.prompt = "## Test failure: missing pandas\n"


def _pytest(n_failed=0, n_errors=0, n_passed=0):
    return types.SimpleNamespace(n_failed=n_failed, n_errors=n_errors,
                                 n_passed=n_passed, n_skipped=0,
                                 duration_seconds=1.0, failures=[])


def _make_router(*, local_text, verify_text=None, tier2_text=None, blocked=False):
    calls = {"handle": 0, "verify": 0, "tier2": 0}

    class _Q:
        def is_blocked(self, now=None):
            return (blocked, 0)

    class _R:
        def __init__(self):
            self.quota = _Q()

        def handle(self, prompt):
            calls["handle"] += 1
            return TaskResult(success=True, handler="local", text=local_text)

        def handle_claude_only(self, prompt, *, reason=""):
            if reason == "verify local proposal":
                calls["verify"] += 1
                return TaskResult(success=True, handler="claude", text=verify_text or "")
            calls["tier2"] += 1
            return TaskResult(success=True, handler="claude", text=tier2_text or "")

    return _R(), calls


def _run(failure, proj, repo, router, *, install_ok=True, pytest_seq, graduation=None):
    install_iter = iter([install_ok] if isinstance(install_ok, bool) else install_ok)
    pytest_iter = iter(pytest_seq)
    gp = proj / "data" / "graduation_state.json"
    with patch.object(auto_fix, "bundle_failure", lambda f, r: _FakeBundle(failure)), \
         patch.object(auto_fix, "run_pytest", lambda *a, **k: _pytest(*next(pytest_iter))), \
         patch.object(auto_fix, "apply_install_package",
                      lambda spec, **k: auto_fix.ApplyResult(success=next(install_iter))), \
         patch.object(auto_fix, "current_branch", lambda r: "main"):
        return auto_fix.auto_fix_one(
            failure, repo, project_root=proj, router=router,
            danger_patterns=auto_fix.DEFAULT_DANGER_PATTERNS, dry_run=False,
            verify_mode=True, graduation=graduation if graduation is not None else {},
            graduation_path=gp,
        )


def _tmp():
    d = Path(tempfile.mkdtemp(prefix="verifygrad_"))
    (d / "data").mkdir()
    (d / "repo").mkdir()
    return d, d / "repo"


def case_verify_approve_bumps_graduation():
    proj, repo = _tmp()
    grad = {}
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,"reasoning":"x","package":"pandas"}',
        verify_text='{"approve": true, "reason": "safe install"}',
    )
    att = _run(_fake_failure(), proj, repo, router,
               install_ok=True, pytest_seq=[(0, 6), (0, 0)], graduation=grad)
    assert att.status == "fixed", att
    assert calls["verify"] == 1, "verify should have been called once"
    assert grad["install_package"]["successes"] == 1, grad
    assert grad["install_package"]["graduated"] is False
    print("  case 1 PASS: verify approved -> fixed -> graduation counter = 1")


def case_verify_reject_escalates_to_tier2():
    proj, repo = _tmp()
    grad = {}
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,"reasoning":"x","package":"badpkg"}',
        verify_text='{"approve": false, "reason": "wrong package"}',
        tier2_text='{"action":"install_package","confidence":0.95,"reasoning":"real one","package":"pandas"}',
    )
    att = _run(_fake_failure(), proj, repo, router,
               install_ok=[True], pytest_seq=[(0, 6), (0, 0)], graduation=grad)
    assert att.status == "fixed", att
    assert att.handler == "claude", "tier-2 should have handled it"
    assert att.claude_retry_used is True
    assert calls["verify"] == 1 and calls["tier2"] == 1
    # graduation NOT bumped (the successful fix came from Claude, not verified-local)
    assert grad.get("install_package", {}).get("successes", 0) == 0, grad
    print("  case 2 PASS: verify rejected -> tier-2 Claude fixed it; no graduation bump")


def case_graduated_skips_verify():
    proj, repo = _tmp()
    grad = {"install_package": {"successes": 10, "graduated": True}}
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,"reasoning":"x","package":"pandas"}',
        verify_text='{"approve": true}',  # should NOT be called
    )
    att = _run(_fake_failure(), proj, repo, router,
               install_ok=True, pytest_seq=[(0, 6), (0, 0)], graduation=grad)
    assert att.status == "fixed", att
    assert calls["verify"] == 0, "graduated type must skip verification"
    print("  case 3 PASS: graduated action-type skipped verification")


def case_quota_blocked_skips_verify():
    proj, repo = _tmp()
    grad = {}
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,"reasoning":"x","package":"pandas"}',
        verify_text='{"approve": true}',
        blocked=True,
    )
    att = _run(_fake_failure(), proj, repo, router,
               install_ok=True, pytest_seq=[(0, 6), (0, 0)], graduation=grad)
    assert att.status == "fixed", att
    assert calls["verify"] == 0, "blocked Claude -> no verify call"
    # best-effort apply, no verification -> no graduation bump
    assert grad.get("install_package", {}).get("successes", 0) == 0
    print("  case 4 PASS: quota-blocked -> verify skipped, applied best-effort")


def case_threshold_crossing():
    state = {}
    crossed_at = None
    for i in range(1, 12):
        just = auto_fix.record_verified_success(state, "apply_diff", threshold=10)
        if just:
            crossed_at = i
    assert crossed_at == 10, f"should graduate on the 10th success, got {crossed_at}"
    assert state["apply_diff"]["graduated"] is True
    # further calls don't re-trigger
    assert auto_fix.record_verified_success(state, "apply_diff", threshold=10) is False
    print("  case 5 PASS: graduation triggers exactly once at threshold=10")


def main():
    print("[smoke_verify_graduate]")
    case_verify_approve_bumps_graduation()
    case_verify_reject_escalates_to_tier2()
    case_graduated_skips_verify()
    case_quota_blocked_skips_verify()
    case_threshold_crossing()
    print("[smoke_verify_graduate] all cases PASS")


if __name__ == "__main__":
    main()
