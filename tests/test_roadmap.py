"""roadmap.scan_roadmap — scrape FP-### statuses + open-backlog from target docs."""
from __future__ import annotations

from orchestrator.roadmap import scan_roadmap, format_roadmap


_HANDOFF = """# HANDOFF

| FP | Title | Status |
|----|-------|--------|
| FP-001 | Python engine | 🔭 Parked |
| FP-002 | ML predictor | 🔭 Concluded NOT VIABLE this session |
| FP-003 | Concurrent sims | ✅ SHIPPED this session |
| FP-008 | Card images | 🟡 Substrate landed; active backlog |
"""

_STATUS = """# Status

## Open backlog (ranked)

### Tier 1
1. ~~Shipped thing.~~ ✅ done
2. A real open item.
3. Another open item.

## Parked plans

### FP-001 — engine
**Status: PARKED.**
"""


def _mk(tmp_path, handoff=None, status=None):
    if handoff is not None:
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / "HANDOFF.md").write_text(handoff, encoding="utf-8")
    if status is not None:
        (tmp_path / "STATUS.md").write_text(status, encoding="utf-8")
    return tmp_path


def test_scan_classifies_fp_statuses(tmp_path):
    r = scan_roadmap(_mk(tmp_path, handoff=_HANDOFF, status=_STATUS))
    assert r.found is True
    assert r.source == "docs/HANDOFF.md"
    assert r.fps["FP-003"] == "shipped"
    assert r.fps["FP-002"] == "concluded"
    assert r.fps["FP-001"] == "parked"
    assert r.fps["FP-008"] == "active"
    assert r.by_status["shipped"] == ["FP-003"]


def test_open_backlog_counts_undone_items(tmp_path):
    r = scan_roadmap(_mk(tmp_path, handoff=_HANDOFF, status=_STATUS))
    # 2 open items (the ✅/~~ one is excluded).
    assert r.open_backlog == 2


def test_handoff_first_mention_wins_over_status(tmp_path):
    # FP-001 appears in BOTH; the HANDOFF (priority file) status should win.
    r = scan_roadmap(_mk(tmp_path, handoff=_HANDOFF, status=_STATUS))
    assert r.fps["FP-001"] == "parked"


def test_falls_back_to_status_when_no_handoff(tmp_path):
    r = scan_roadmap(_mk(tmp_path, status=_STATUS))
    assert r.found is True
    assert r.source == "STATUS.md"
    assert "FP-001" in r.fps


def test_no_docs_means_not_found(tmp_path):
    r = scan_roadmap(tmp_path)
    assert r.found is False
    assert format_roadmap(r) == []


def test_format_roadmap_renders(tmp_path):
    r = scan_roadmap(_mk(tmp_path, handoff=_HANDOFF, status=_STATUS))
    lines = "\n".join(format_roadmap(r))
    assert "target roadmap" in lines
    assert "shipped" in lines and "FP-003" in lines
    assert "open backlog items" in lines
