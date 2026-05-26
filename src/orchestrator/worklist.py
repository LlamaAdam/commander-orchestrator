"""Scan a target repo for actionable WORK beyond failing tests.

The fix loop only acts on red tests; when the suite is green it has "nothing
to do". This surfaces the real to-do list so a run/`orch work` always shows
what *could* be picked up:

  - open items from `docs/AGENT_BACKLOG.md` (its machine-readable table marks
    each `open`/`done` with a priority + scope + title -- the agent queue),
  - the FP-### roadmap (active + parked future plans), via `roadmap.py`,
  - deferred test work (skipped / xfail / slow-gated tests).

Read-only, best-effort markdown/grep scraping. Empty for repos without the
docs. NOTE: the orchestrator only *surfaces* these -- backlog/FP items need a
human (or a deliberate Claude task), not the auto-fix loop.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .roadmap import scan_roadmap

_PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "MED": 1, "LOW": 2, "": 3}
_SKIP_RE = re.compile(r"@pytest\.mark\.skip|pytest\.skip\(|@pytest\.mark\.xfail")


@dataclass
class WorkItem:
    ident: str       # "#011" or "FP-008"
    title: str
    priority: str = ""   # HIGH | MEDIUM | LOW | ""
    scope: str = ""      # e.g. "~2h"
    status: str = ""     # for FPs: active | parked | ...


@dataclass
class WorkList:
    backlog_open: list = field(default_factory=list)   # WorkItem (from AGENT_BACKLOG)
    fps: list = field(default_factory=list)            # WorkItem (active/parked FPs)
    skipped_tests: int = 0
    backlog_source: Optional[str] = None

    @property
    def total(self) -> int:
        return len(self.backlog_open) + len(self.fps)


def _parse_agent_backlog(repo_dir: Path) -> tuple:
    """Parse docs/AGENT_BACKLOG.md's status table -> (open WorkItems, source).

    Table rows look like:
      | [#011](#...) | MEDIUM | open | ~2h | Batch mode for ... |
    """
    f = repo_dir / "docs" / "AGENT_BACKLOG.md"
    if not f.exists():
        return [], None
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], None
    items: list = []
    for ln in text.splitlines():
        if not ln.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        m = re.search(r"#\d+", cells[0])
        if not m:
            continue  # header/separator rows
        ident, priority, status, scope, title = m.group(0), cells[1].upper(), cells[2].lower(), cells[3], cells[4]
        if status == "open":
            items.append(WorkItem(ident=ident, title=title, priority=priority, scope=scope))
    items.sort(key=lambda w: (_PRIORITY_RANK.get(w.priority, 3), w.ident))
    return items, "docs/AGENT_BACKLOG.md"


def _count_skipped_tests(repo_dir: Path) -> int:
    tests = repo_dir / "tests"
    if not tests.is_dir():
        return 0
    n = 0
    for py in tests.rglob("test_*.py"):
        try:
            n += len(_SKIP_RE.findall(py.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return n


def scan_worklist(repo_dir) -> WorkList:
    repo_dir = Path(repo_dir)
    backlog_open, source = _parse_agent_backlog(repo_dir)

    # Future plans worth picking up = active + parked FPs (not shipped/concluded).
    roadmap = scan_roadmap(repo_dir)
    fps: list = []
    for st in ("active", "parked", "listed"):
        for fid in roadmap.by_status.get(st, []):
            fps.append(WorkItem(ident=fid, title="", status=st))

    return WorkList(
        backlog_open=backlog_open,
        fps=fps,
        skipped_tests=_count_skipped_tests(repo_dir),
        backlog_source=source,
    )


def format_worklist(w: WorkList) -> str:
    L = ["=== work available (beyond failing tests) ==="]
    if w.backlog_open:
        L.append(f"open backlog items ({w.backlog_source}), by priority:")
        for it in w.backlog_open:
            scope = f" [{it.scope}]" if it.scope else ""
            L.append(f"  {it.priority:6} {it.ident:6} {it.title}{scope}")
    else:
        L.append("open backlog items: none (or no AGENT_BACKLOG.md)")
    if w.fps:
        L.append("")
        L.append("future plans not yet done (active/parked):")
        for it in w.fps:
            L.append(f"  {it.status:8} {it.ident}")
    L.append("")
    L.append(f"deferred tests (skip/xfail): {w.skipped_tests}")
    L.append("")
    L.append("NOTE: the fix loop auto-fixes failing tests only. These items "
             "need a human or a deliberate Claude task, not the auto-fixer.")
    return "\n".join(L)
