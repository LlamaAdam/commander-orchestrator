"""Preflight audit — subsystem checks + backlog roll-up (probes stubbed)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from orchestrator import audit as ad
from orchestrator.audit import run_audit, format_audit, OK, WARN, FAIL


def _status(report, name):
    return next(c.status for c in report.checks if c.name == name)


def _healthy(monkeypatch, tmp_path):
    """Stub every probe to a healthy state; return (project_root, repo_dir)."""
    monkeypatch.setattr(ad.local_model, "ping", lambda url, **k: True)
    monkeypatch.setattr(ad.local_model, "list_models",
                        lambda url, **k: ["qwen2.5-coder:14b"])
    monkeypatch.setattr(ad.claude_cli, "find_claude_binary", lambda: r"C:\x\claude.CMD")
    monkeypatch.setattr(ad.quota, "QuotaTracker",
                        lambda path: SimpleNamespace(is_blocked=lambda: (False, 0.0)))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    proj = tmp_path / "proj"
    (proj / "data").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return proj, repo


def test_all_healthy_is_ready(monkeypatch, tmp_path):
    proj, repo = _healthy(monkeypatch, tmp_path)
    r = run_audit(proj, repo)
    assert r.ok is True and r.n_fail == 0
    assert _status(r, "ollama") == OK
    assert _status(r, "claude_cli") == OK
    assert _status(r, "billing_safety") == OK
    assert _status(r, "target_repo") == OK
    assert "READY" in format_audit(r)


def test_ollama_down_is_warn_not_blocker(monkeypatch, tmp_path):
    proj, repo = _healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(ad.local_model, "ping", lambda url, **k: False)
    r = run_audit(proj, repo)
    assert _status(r, "ollama") == WARN
    assert r.ok is True  # Claude fallback still works -> not a hard blocker


def test_missing_claude_cli_is_blocker(monkeypatch, tmp_path):
    proj, repo = _healthy(monkeypatch, tmp_path)
    def _raise():
        raise FileNotFoundError("no claude")
    monkeypatch.setattr(ad.claude_cli, "find_claude_binary", _raise)
    r = run_audit(proj, repo)
    assert _status(r, "claude_cli") == FAIL
    assert r.ok is False
    assert "NOT READY" in format_audit(r)


def test_live_api_key_warns(monkeypatch, tmp_path):
    proj, repo = _healthy(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-live")
    r = run_audit(proj, repo)
    assert _status(r, "billing_safety") == WARN
    assert r.ok is True  # scrubbed at call time; warn only


def test_quota_blocked_warns(monkeypatch, tmp_path):
    proj, repo = _healthy(monkeypatch, tmp_path)
    monkeypatch.setattr(ad.quota, "QuotaTracker",
                        lambda path: SimpleNamespace(is_blocked=lambda: (True, 1800.0)))
    r = run_audit(proj, repo)
    assert _status(r, "quota") == WARN


def test_missing_target_repo_is_blocker(monkeypatch, tmp_path):
    proj, _repo = _healthy(monkeypatch, tmp_path)
    r = run_audit(proj, tmp_path / "does_not_exist")
    assert _status(r, "target_repo") == FAIL
    assert r.ok is False


def test_non_git_target_warns(monkeypatch, tmp_path):
    proj, _repo = _healthy(monkeypatch, tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    r = run_audit(proj, plain)
    assert _status(r, "target_repo") == WARN
    assert r.ok is True


def test_backlog_counts(monkeypatch, tmp_path):
    proj, repo = _healthy(monkeypatch, tmp_path)
    (proj / "data" / "needs_human.md").write_text(
        "# escalations\n\n## 2026-01-01 -- t::a\n- reason: x\n\n## 2026-01-02 -- t::b\n- reason: y\n",
        encoding="utf-8")
    from orchestrator.auto_fix import MAX_FAILED_ATTEMPTS
    (proj / "data" / "auto_fix_seen.json").write_text(json.dumps({
        "h1": {"attempt_count": MAX_FAILED_ATTEMPTS},   # capped
        "h2": {"attempt_count": 1},                     # not capped
    }), encoding="utf-8")
    r = run_audit(proj, repo)
    assert r.backlog["needs_human"] == 2
    assert r.backlog["capped_failures"] == 1
    text = format_audit(r)
    assert "needs-human escalations : 2" in text
    assert "tier-3-capped failures  : 1" in text


def test_stale_branches_and_freshness_checks(monkeypatch, tmp_path):
    proj, repo = _healthy(monkeypatch, tmp_path)

    def fake_git(args, cwd):
        if args[:2] == ["branch", "--list"]:
            return [f"  auto-fix/{i}" for i in range(12)]  # >10 -> WARN
        if args[:1] == ["rev-list"]:
            return ["3"]  # 3 commits behind upstream
        return []
    monkeypatch.setattr(ad, "_git_lines", fake_git)

    r = run_audit(proj, repo)
    assert _status(r, "stale_branches") == WARN
    assert _status(r, "repo_freshness") == WARN


def test_fresh_repo_no_stale_is_ok(monkeypatch, tmp_path):
    proj, repo = _healthy(monkeypatch, tmp_path)

    def fake_git(args, cwd):
        if args[:1] == ["rev-list"]:
            return ["0"]  # up to date
        return []  # no auto-fix branches
    monkeypatch.setattr(ad, "_git_lines", fake_git)

    r = run_audit(proj, repo)
    assert _status(r, "repo_freshness") == OK
    assert r.ok is True


def test_deep_runs_target_suite(monkeypatch, tmp_path):
    proj, repo = _healthy(monkeypatch, tmp_path)
    fake = SimpleNamespace(n_passed=10, n_failed=2, n_errors=1, n_total=13)
    import orchestrator.harness as h
    monkeypatch.setattr(h, "run_pytest", lambda repo_dir, lane="fast": fake)
    r = run_audit(proj, repo, deep=True)
    assert _status(r, "target_suite") == OK
    assert r.backlog["failing_tests"] == 3  # 2 failed + 1 error
