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


def _replace_file_action(path, new_content, conf=0.9):
    return json.dumps({"action": "replace_file", "confidence": conf,
                       "reasoning": "rewrite the buggy file",
                       "path": path, "new_content": new_content})


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
    monkeypatch.setattr(af, "bundle_failure", lambda failure, repo_dir, **kw: make_bundle())
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


def test_fix_bundle_requests_full_file_budgets(tmp_path, monkeypatch):
    """replace_file needs the COMPLETE file, so the fix path must build the
    bundle with the large source budgets (not the small triage default)."""
    captured = {}

    def _spy(failure, repo_dir, **kw):
        captured.update(kw)
        return make_bundle()
    monkeypatch.setattr(af, "bundle_failure", _spy)
    monkeypatch.setattr(af, "run_pytest",
                        lambda repo_dir, lane="fast": _pytest_result(n_failed=0))

    router = FakeRouter(handle=TaskResult(success=True, handler="local", text=_install_action()))
    af.auto_fix_one(make_failure(), tmp_path, project_root=tmp_path, router=router)

    assert captured.get("related_source_chars") == af.FIX_RELATED_SOURCE_CHARS
    assert captured.get("test_source_chars") == af.FIX_TEST_SOURCE_CHARS
    assert af.FIX_RELATED_SOURCE_CHARS >= 20000  # full target files fit


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
    # dry_run skips baseline pytest entirely; stub_seams keeps run_pytest's
    # result queue empty so a stray call would IndexError -- proving none happens.
    router = FakeRouter(handle=TaskResult(success=True, handler="local", text=_install_action()))
    attempt = af.auto_fix_one(make_failure(), tmp_path, project_root=tmp_path,
                              router=router, dry_run=True)
    assert attempt.status == "would_apply"
    assert attempt.action.action == "install_package"


def test_tier2_replace_file_fixes_source(git_repo, monkeypatch):
    """The diff-tar-pit sidestep, end to end: local escalates a source bug,
    tier-2 Claude returns the COMPLETE corrected file via replace_file, it is
    written directly (no git apply), pytest improves -> fixed and committed on
    an auto-fix branch."""
    monkeypatch.setattr(af, "bundle_failure", lambda failure, repo_dir, **kw: make_bundle())
    results = [_pytest_result(n_failed=1), _pytest_result(n_failed=0)]
    monkeypatch.setattr(af, "run_pytest", lambda repo_dir, lane="fast": results.pop(0))

    router = FakeRouter(
        handle=TaskResult(success=True, handler="local", text=_escalate_action()),
        claude=TaskResult(success=True, handler="claude",
                          text=_replace_file_action("src.py", "x = 2  # corrected\n")),
    )
    attempt = af.auto_fix_one(make_failure(), git_repo, project_root=git_repo, router=router)

    assert attempt.status == "fixed", attempt.reason
    assert attempt.claude_retry_used is True
    assert attempt.action.action == "replace_file"
    # The file holds the full corrected contents.
    assert (git_repo / "src.py").read_text(encoding="utf-8") == "x = 2  # corrected\n"
    # And it landed as a commit (HEAD touches src.py on the auto-fix branch).
    log = af._git(["show", "--name-only", "--format=", "HEAD"], cwd=git_repo).stdout
    assert "src.py" in log


def test_replace_file_regression_is_reverted(git_repo, monkeypatch):
    """If the rewrite does NOT reduce failures, the patched file is reverted to
    its committed state (WIP-safe) and the attempt is recorded as regressed."""
    monkeypatch.setattr(af, "bundle_failure", lambda failure, repo_dir, **kw: make_bundle())
    # baseline=1, after-apply still 1 -> no improvement -> regressed/reverted.
    results = [_pytest_result(n_failed=1), _pytest_result(n_failed=1)]
    monkeypatch.setattr(af, "run_pytest", lambda repo_dir, lane="fast": results.pop(0))

    router = FakeRouter(
        handle=TaskResult(success=True, handler="claude",  # claude-handled: no tier-2 retry
                          text=_replace_file_action("src.py", "x = 999  # wrong\n")),
    )
    attempt = af.auto_fix_one(make_failure(), git_repo, project_root=git_repo, router=router)

    assert attempt.status == "regressed"
    # Reverted: src.py is back to its committed content, on the original branch.
    assert (git_repo / "src.py").read_text(encoding="utf-8") == "x = 1\n"
    assert af.current_branch(git_repo) == "main"


def test_local_test_weakening_is_refused_and_reverted(git_repo, monkeypatch):
    """A local apply_diff that guts the failing test's assertion must be refused
    (escalated) and reverted -- the orchestrator never makes a suite green by
    weakening a test."""
    monkeypatch.setattr(af, "bundle_failure", lambda failure, repo_dir, **kw: make_bundle())
    # Only the baseline pytest runs; the guard fires before the after-run.
    monkeypatch.setattr(af, "run_pytest",
                        lambda repo_dir, lane="fast": _pytest_result(n_failed=1))

    weakening_diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def test_x():\n"
        "-    assert True\n"
        "+    pass\n"
    )
    action = json.dumps({"action": "apply_diff", "confidence": 0.9,
                         "reasoning": "make it pass", "diff": weakening_diff,
                         "files_touched": ["tests/test_x.py"]})
    router = FakeRouter(handle=TaskResult(success=True, handler="local", text=action))

    attempt = af.auto_fix_one(make_failure(nodeid="tests/test_x.py::test_x"),
                              git_repo, project_root=git_repo, router=router,
                              enable_claude_retry=False)
    assert attempt.status == "escalated"
    assert "weakens test" in attempt.reason
    # Reverted to the committed, still-asserting form.
    assert "assert True" in (git_repo / "tests" / "test_x.py").read_text(encoding="utf-8")


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
