"""Project Manager — plan synthesis + schtasks scheduling (no real schtasks)."""
from __future__ import annotations

import pytest

from orchestrator import pm


_BACKLOG = """# Agent backlog

| ID | Priority | Status | Scope | Title |
|----|----------|--------|-------|-------|
| [#001](#001) | LOW | done | ~5m | Old shipped thing |
| [#020](#020) | HIGH | open | ~1h | Data-driven bucketing |
| [#011](#011) | MEDIUM | open | ~2h | Batch mode |
"""

_HANDOFF = """# HANDOFF
| FP | Title | Status |
|----|-------|--------|
| FP-003 | sims | SHIPPED |
| FP-008 | images | active |
"""


def _mk_repo(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AGENT_BACKLOG.md").write_text(_BACKLOG, encoding="utf-8")
    (tmp_path / "docs" / "HANDOFF.md").write_text(_HANDOFF, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text(
        "import pytest\n@pytest.mark.skip(reason='x')\ndef test_one(): pass\n",
        encoding="utf-8")
    return tmp_path


# ---- analysis -------------------------------------------------------------

def test_plan_without_test_scan_leaves_failing_none(tmp_path):
    plan = pm.build_plan(_mk_repo(tmp_path), with_tests=False)
    assert plan.failing_test_count is None
    assert plan.auto_fixable == []
    # backlog (HIGH first) + FP both surfaced as needs-human
    idents = [i.ident for i in plan.needs_human]
    assert idents[0] == "#020"            # HIGH ranked first
    assert "FP-008" in idents
    assert "#001" not in idents           # done excluded
    assert plan.deferred_test_count == 1


def test_plan_with_test_scan_surfaces_auto_fixable(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_count_failing", lambda repo: 3)
    plan = pm.build_plan(_mk_repo(tmp_path), with_tests=True)
    assert plan.failing_test_count == 3
    assert plan.has_auto_work
    assert plan.auto_fixable and "3 failing" in plan.auto_fixable[0].title
    assert "auto-fixable now" in plan.recommended


def test_recommended_points_at_human_task_when_green(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_count_failing", lambda repo: 0)
    plan = pm.build_plan(_mk_repo(tmp_path), with_tests=True)
    assert plan.failing_test_count == 0
    assert "#020" in plan.recommended  # highest-value human task


def test_count_failing_degrades_to_none_on_error(tmp_path, monkeypatch):
    import orchestrator.harness as h
    def boom(*a, **k):
        raise RuntimeError("no pytest")
    monkeypatch.setattr(h, "run_pytest", boom)
    assert pm._count_failing(tmp_path) is None


def test_format_plan_renders_sections(tmp_path):
    text = pm.format_plan(pm.build_plan(_mk_repo(tmp_path)))
    assert "Project Manager: work plan" in text
    assert "auto-fixable now" in text
    assert "needs a human" in text
    assert "deferred tests" in text


# ---- scheduling -----------------------------------------------------------

def test_build_fix_command_quotes_paths():
    cmd = pm.build_fix_command(r"C:\dev\x", python=r"C:\py\python.exe")
    assert cmd == r'"C:\py\python.exe" -m orchestrator.cli fix --repo-dir "C:\dev\x"'


def test_build_schtasks_argv_daily_with_time():
    argv = pm.build_schtasks_argv("NightlyFix", "CMD", "daily", "02:30")
    assert argv[:4] == ["schtasks", "/Create", "/TN", "Orchestrator-NightlyFix"]
    assert "/SC" in argv and argv[argv.index("/SC") + 1] == "DAILY"
    assert argv[argv.index("/ST") + 1] == "02:30"
    assert "/F" in argv


def test_build_schtasks_argv_keeps_existing_prefix():
    argv = pm.build_schtasks_argv("Orchestrator-Foo", "CMD", "ONLOGON")
    assert argv[argv.index("/TN") + 1] == "Orchestrator-Foo"
    assert "/ST" not in argv  # ONLOGON takes no time


def test_build_schtasks_argv_rejects_bad_schedule():
    with pytest.raises(ValueError):
        pm.build_schtasks_argv("X", "CMD", "EVERY_FULL_MOON")


def test_create_scheduled_fix_invokes_schtasks(monkeypatch):
    seen = {}
    monkeypatch.setattr(pm.subprocess, "run",
                        lambda argv, **k: seen.update(argv=argv) or
                        type("P", (), {"returncode": 0, "stdout": "OK", "stderr": ""})())
    ok, out = pm.create_scheduled_fix(r"C:\dev\x", "DAILY", "01:00", name="NightlyFix")
    assert ok and "OK" in out
    argv = seen["argv"]
    assert argv[0] == "schtasks" and "/Create" in argv
    assert "orchestrator.cli fix" in argv[argv.index("/TR") + 1]


def test_schtasks_missing_is_handled(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(pm.subprocess, "run", boom)
    ok, out = pm.create_scheduled_fix(r"C:\dev\x", "DAILY", "01:00")
    assert ok is False and "not found" in out


def test_list_scheduled_filters_to_prefix(monkeypatch):
    sample = (
        "TaskName: \\Other-Thing\nStatus: Ready\n\n"
        "TaskName: \\Orchestrator-NightlyFix\nStatus: Ready\n"
    )
    monkeypatch.setattr(pm.subprocess, "run",
                        lambda argv, **k: type("P", (), {
                            "returncode": 0, "stdout": sample, "stderr": ""})())
    ok, out = pm.list_scheduled()
    assert ok
    assert "Orchestrator-NightlyFix" in out
    assert "Other-Thing" not in out
