"""worklist.scan_worklist — actionable to-do surfacing (backlog + FPs + skips)."""
from __future__ import annotations

from orchestrator.worklist import scan_worklist, format_worklist


_BACKLOG = """# Agent backlog

| ID | Priority | Status | Scope | Title |
|----|----------|--------|-------|-------|
| [#001](#001) | LOW | done | ~5 min | Old shipped thing |
| [#011](#011) | MEDIUM | open | ~2h | Batch mode for curate |
| [#020](#020) | HIGH | open | ~1h | Data-driven bucketing |
| [#021](#021) | LOW | open | ~30m | Tidy logs |
"""

_HANDOFF = """# HANDOFF
| FP | Title | Status |
|----|-------|--------|
| FP-003 | sims | ✅ SHIPPED |
| FP-008 | images | 🟡 active |
| FP-011 | token | 🔭 Parked |
"""


def _mk(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AGENT_BACKLOG.md").write_text(_BACKLOG, encoding="utf-8")
    (tmp_path / "docs" / "HANDOFF.md").write_text(_HANDOFF, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text(
        "import pytest\n"
        "@pytest.mark.skip(reason='x')\ndef test_one(): pass\n"
        "@pytest.mark.xfail\ndef test_two(): assert False\n",
        encoding="utf-8")
    return tmp_path


def test_open_backlog_items_ranked_by_priority(tmp_path):
    w = scan_worklist(_mk(tmp_path))
    idents = [it.ident for it in w.backlog_open]
    # done item excluded; open items sorted HIGH -> MEDIUM -> LOW
    assert idents == ["#020", "#011", "#021"]
    assert all(it.priority in ("HIGH", "MEDIUM", "LOW") for it in w.backlog_open)
    assert w.backlog_open[0].title == "Data-driven bucketing"


def test_excludes_done_items(tmp_path):
    w = scan_worklist(_mk(tmp_path))
    assert all(it.ident != "#001" for it in w.backlog_open)


def test_fps_are_active_and_parked_not_shipped(tmp_path):
    w = scan_worklist(_mk(tmp_path))
    fp_ids = {it.ident for it in w.fps}
    assert "FP-008" in fp_ids and "FP-011" in fp_ids  # active + parked
    assert "FP-003" not in fp_ids                      # shipped -> not work


def test_counts_skipped_tests(tmp_path):
    w = scan_worklist(_mk(tmp_path))
    assert w.skipped_tests == 2  # one skip + one xfail


def test_empty_repo_is_safe(tmp_path):
    w = scan_worklist(tmp_path)
    assert w.backlog_open == [] and w.fps == [] and w.total == 0
    assert "none" in format_worklist(w)


def test_format_worklist_renders(tmp_path):
    text = format_worklist(scan_worklist(_mk(tmp_path)))
    assert "work available" in text
    assert "#020" in text and "FP-008" in text
    assert "deferred tests" in text
