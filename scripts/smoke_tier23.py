"""Offline smoke test for tier-2 Claude fallback and tier-3 retry caps.

Stubs out pytest, pip install, file I/O, and the Router. Verifies that
auto_fix_one routes through the expected paths for several scenarios.

Run: python scripts/smoke_tier23.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

# Allow `python scripts/...` to import the package without `pip install -e .`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestrator import auto_fix
from orchestrator.harness import TestFailure
from orchestrator.router import TaskResult


def _fake_failure(nodeid: str = "tests.fake::test_x") -> TestFailure:
    return TestFailure(
        nodeid=nodeid,
        classname="tests.fake",
        name=nodeid.split("::")[-1],
        file="",
        line=None,
        failure_type="error",
        message='ModuleNotFoundError: No module named "flask"',
        traceback="Traceback: missing flask",
    )


class _FakeBundle:
    """Drop-in replacement for harness.bundle_failure's return value."""
    def __init__(self, failure):
        self.failure = failure
        self.prompt = (
            "## Test: tests.fake::test_x\n## Error: ModuleNotFoundError flask\n"
        )


def _fake_pytest_run(n_failed=0, n_errors=0, n_passed=0):
    return types.SimpleNamespace(
        n_failed=n_failed, n_errors=n_errors, n_passed=n_passed,
        n_skipped=0, duration_seconds=1.0, failures=[],
    )


def _make_router(local_text: str, claude_text: str | None = None,
                 *, claude_blocked: bool = False):
    """Return a fake Router with controllable handle / handle_claude_only."""
    calls = {"handle": 0, "claude": 0}

    class _FakeQuota:
        def is_blocked(self, now=None):
            return (claude_blocked, 0)

    class _FakeRouter:
        def __init__(self):
            self.quota = _FakeQuota()

        def handle(self, prompt):
            calls["handle"] += 1
            return TaskResult(success=True, handler="local", text=local_text)

        def handle_claude_only(self, prompt, *, reason=""):
            calls["claude"] += 1
            return TaskResult(success=True, handler="claude",
                              text=claude_text or "")

    return _FakeRouter(), calls


def _run_with_stubs(failure, project_root, repo_dir, router,
                    *, install_outcomes, pytest_outcomes,
                    enable_claude_retry=True):
    """Call auto_fix.auto_fix_one with all the slow bits stubbed out.

    install_outcomes: list[bool] consumed in order; True -> success.
    pytest_outcomes: list[tuple[n_failed, n_errors]] consumed in order.
    """
    install_iter = iter(install_outcomes)
    pytest_iter = iter(pytest_outcomes)

    def stub_install(spec, *, python_exe=None, timeout=600):
        ok = next(install_iter)
        if ok:
            return auto_fix.ApplyResult(success=True)
        return auto_fix.ApplyResult(success=False,
                                    error=f"could not find {spec}")

    def stub_pytest(*args, **kwargs):
        nf, ne = next(pytest_iter)
        return _fake_pytest_run(n_failed=nf, n_errors=ne)

    with patch.object(auto_fix, "bundle_failure",
                      lambda f, r: _FakeBundle(failure)), \
         patch.object(auto_fix, "run_pytest", stub_pytest), \
         patch.object(auto_fix, "apply_install_package", stub_install), \
         patch.object(auto_fix, "current_branch", lambda r: "main"):
        return auto_fix.auto_fix_one(
            failure, repo_dir,
            project_root=project_root, router=router,
            danger_patterns=auto_fix.DEFAULT_DANGER_PATTERNS,
            dry_run=False, enable_claude_retry=enable_claude_retry,
        )


def _setup_tmp() -> tuple[Path, Path]:
    td = Path(tempfile.mkdtemp(prefix="autofix_smoke_"))
    (td / "data").mkdir()
    repo = td / "repo"
    repo.mkdir()
    return td, repo


# ---------------------------------------------------------------------------

def case_1_local_fixes_directly():
    """Happy path: local proposes flask, install succeeds, tests improve."""
    proj, repo = _setup_tmp()
    failure = _fake_failure()
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,'
                   '"reasoning":"missing flask","package":"flask"}',
    )
    attempt = _run_with_stubs(
        failure, proj, repo, router,
        install_outcomes=[True],
        pytest_outcomes=[(0, 6), (0, 0)],  # baseline 6 errors -> 0
    )
    assert attempt.status == "fixed", attempt
    assert attempt.handler == "local"
    assert attempt.claude_retry_used is False
    assert calls["handle"] == 1 and calls["claude"] == 0
    print(f"  case 1 PASS: local fixed directly, no Claude retry")


def case_2_local_bad_package_then_claude_rescues():
    """Local proposes a bad pkg, install fails; tier-2 -> Claude proposes flask; succeeds."""
    proj, repo = _setup_tmp()
    failure = _fake_failure()
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,'
                   '"reasoning":"missing module","package":"flsk-nonexistent"}',
        claude_text='{"action":"install_package","confidence":0.95,'
                    '"reasoning":"actual name is flask","package":"flask"}',
    )
    attempt = _run_with_stubs(
        failure, proj, repo, router,
        install_outcomes=[False, True],  # 1st fails (local), 2nd succeeds (claude)
        pytest_outcomes=[(0, 6), (0, 0)],  # baseline; after-Claude
    )
    assert attempt.status == "fixed", attempt
    assert attempt.handler == "claude"
    assert attempt.claude_retry_used is True
    assert calls["handle"] == 1 and calls["claude"] == 1
    print(f"  case 2 PASS: tier-2 Claude rescue succeeded (handler={attempt.handler})")


def case_3_local_regressed_then_claude_also_regressed():
    """Local installs something that doesn't help; Claude tries flask, also no help; final = regressed."""
    proj, repo = _setup_tmp()
    failure = _fake_failure()
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,'
                   '"reasoning":"missing module","package":"flask"}',
        claude_text='{"action":"install_package","confidence":0.9,'
                    '"reasoning":"try a different angle","package":"requests"}',
    )
    attempt = _run_with_stubs(
        failure, proj, repo, router,
        install_outcomes=[True, True],
        # baseline=6 -> after-local=6 (no improvement) -> after-claude=6 (still no improvement)
        pytest_outcomes=[(0, 6), (0, 6), (0, 6)],
    )
    assert attempt.status == "regressed", attempt
    assert attempt.handler == "claude"
    assert attempt.claude_retry_used is True
    assert calls["claude"] == 1
    print(f"  case 3 PASS: both attempts regressed, final handler={attempt.handler}")


def case_4_quota_blocked_no_retry():
    """Local fails to apply, Claude is quota-blocked -> no retry, stays as apply_failed."""
    proj, repo = _setup_tmp()
    failure = _fake_failure()
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,'
                   '"reasoning":"bad pkg","package":"foo-nope"}',
        claude_blocked=True,
    )
    attempt = _run_with_stubs(
        failure, proj, repo, router,
        install_outcomes=[False],
        pytest_outcomes=[(0, 6)],  # only baseline; no after-call since install failed
    )
    assert attempt.status == "apply_failed", attempt
    assert attempt.handler == "local"
    assert attempt.claude_retry_used is False
    assert calls["claude"] == 0, "Claude was blocked, should not have been called"
    print(f"  case 4 PASS: quota-blocked Claude correctly skipped")


def case_5_retry_disabled():
    """enable_claude_retry=False -> local failure stays as apply_failed, no Claude call."""
    proj, repo = _setup_tmp()
    failure = _fake_failure()
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,'
                   '"reasoning":"bad","package":"nope"}',
    )
    attempt = _run_with_stubs(
        failure, proj, repo, router,
        install_outcomes=[False],
        pytest_outcomes=[(0, 6)],
        enable_claude_retry=False,
    )
    assert attempt.status == "apply_failed", attempt
    assert attempt.claude_retry_used is False
    assert calls["claude"] == 0
    print(f"  case 5 PASS: retry disabled -> no Claude call")


# ---------------------------------------------------------------------------
# Tier 3: cap enforcement uses auto_fix_failures (the seen-state is checked there)

def case_6_tier3_cap_skips_failure():
    """Pre-seed seen.json with attempt_count=3; cap should kick in and skip."""
    proj, repo = _setup_tmp()
    failure = _fake_failure()
    seen_path = proj / "data" / "auto_fix_seen.json"
    h = auto_fix._dedup_hash(failure)
    seen_path.write_text(json.dumps({
        h: {"nodeid": failure.nodeid, "last_attempt_at": 0,
            "last_status": "regressed", "attempt_count": 3, "regressions": 0,
            "cap_escalated": True},
    }))

    router, calls = _make_router(local_text="{}")  # won't be called

    # auto_fix_failures path
    with patch.object(auto_fix, "bundle_failure",
                      lambda f, r: _FakeBundle(failure)), \
         patch.object(auto_fix, "run_pytest", lambda *a, **k: _fake_pytest_run(0, 6)):
        run = auto_fix.auto_fix_failures(
            failures=[failure], repo_dir=repo, project_root=proj,
            router=router, danger_patterns=auto_fix.DEFAULT_DANGER_PATTERNS,
            dry_run=False, max_failures=1,
        )

    assert run.n_skipped_capped == 1, f"expected n_skipped_capped=1, got {run}"
    assert calls["handle"] == 0 and calls["claude"] == 0, "router must not be called when capped"
    print(f"  case 6 PASS: tier-3 cap prevented attempt (skipped_capped={run.n_skipped_capped})")


def case_7_tier3_counters_bump_on_regression():
    """Each regression bumps attempt_count and (when worse than baseline) regressions."""
    proj, repo = _setup_tmp()
    failure = _fake_failure("tests.fake::test_y")
    router, calls = _make_router(
        local_text='{"action":"install_package","confidence":1.0,'
                   '"reasoning":"x","package":"flask"}',
        claude_text='{"action":"install_package","confidence":1.0,'
                    '"reasoning":"y","package":"requests"}',
    )

    # Drive baseline=6, after-local=8 (worse), after-claude=8 (still worse).
    # Both attempts count as 1 attempt and 1 regression (after > baseline).
    with patch.object(auto_fix, "bundle_failure",
                      lambda f, r: _FakeBundle(failure)), \
         patch.object(auto_fix, "run_pytest",
                      side_effect=[_fake_pytest_run(0, 6),
                                   _fake_pytest_run(0, 8),
                                   _fake_pytest_run(0, 8)]) as p, \
         patch.object(auto_fix, "apply_install_package",
                      side_effect=lambda spec, **k: auto_fix.ApplyResult(success=True)), \
         patch.object(auto_fix, "current_branch", lambda r: "main"):
        run = auto_fix.auto_fix_failures(
            failures=[failure], repo_dir=repo, project_root=proj,
            router=router, danger_patterns=auto_fix.DEFAULT_DANGER_PATTERNS,
            dry_run=False, max_failures=1,
        )

    seen = json.loads((proj / "data" / "auto_fix_seen.json").read_text())
    h = auto_fix._dedup_hash(failure)
    rec = seen[h]
    assert rec["attempt_count"] == 1, rec
    assert rec["regressions"] == 1, rec
    assert run.attempts[0].status == "regressed"
    assert run.attempts[0].claude_retry_used is True
    print(f"  case 7 PASS: counters bumped (attempt_count={rec['attempt_count']}, regressions={rec['regressions']})")


# ---------------------------------------------------------------------------

def main():
    print("[smoke_tier23]")
    case_1_local_fixes_directly()
    case_2_local_bad_package_then_claude_rescues()
    case_3_local_regressed_then_claude_also_regressed()
    case_4_quota_blocked_no_retry()
    case_5_retry_disabled()
    case_6_tier3_cap_skips_failure()
    case_7_tier3_counters_bump_on_regression()
    print("[smoke_tier23] all cases PASS")


if __name__ == "__main__":
    main()
