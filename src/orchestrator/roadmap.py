"""Scan a target repo's docs for its product backlog + FP-### roadmap.

The orchestrator fixes *bugs*; this surfaces the *plans* alongside them so a
preflight audit shows one unified picture: bugs + orchestrator-backlog +
product roadmap. Best-effort markdown scraping -- if the docs aren't present
(non-commander-builder target), `found=False` and the audit omits the section.

Source preference: `docs/HANDOFF.md` (a maintained FP status table) first,
then `STATUS.md` (the ranked backlog + parked FP headings). First mention of
each FP id wins, so the maintained table beats stale prose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Files to scan, in priority order (first mention of each FP id wins).
_ROADMAP_FILES = ("docs/HANDOFF.md", "STATUS.md", "docs/future-plans-action.md")

_FP_RE = re.compile(r"FP-\d+")

# Status classification from the text around an FP mention. Order matters:
# more-specific / "done" states are checked before "parked".
_STATUS_RULES = (
    ("shipped", ("shipped", "✅")),
    ("concluded", ("concluded", "not viable", "not-viable")),
    ("active", ("active", "🟡", "substrate", "in progress", "in-progress")),
    ("parked", ("parked", "🔭", "do not promote", "do-not-promote", "deferred")),
)


def _classify(ctx: str) -> str:
    low = ctx.lower()
    for label, needles in _STATUS_RULES:
        if any(n in low for n in needles):
            return label
    return "listed"


@dataclass
class Roadmap:
    found: bool = False
    source: Optional[str] = None
    fps: dict = field(default_factory=dict)       # {"FP-003": "shipped", ...}
    by_status: dict = field(default_factory=dict)  # {"shipped": ["FP-003"], ...}
    open_backlog: Optional[int] = None             # rough count from STATUS.md


def _count_open_backlog(repo_dir: Path) -> Optional[int]:
    """Rough count of un-done items in STATUS.md's 'Open backlog' section:
    numbered list items NOT struck through (~~) and NOT marked shipped (✅)."""
    status = repo_dir / "STATUS.md"
    if not status.exists():
        return None
    try:
        text = status.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Restrict to the "Open backlog" section if present (up to "Parked plans").
    low = text.lower()
    start = low.find("open backlog")
    if start == -1:
        return None
    end = low.find("parked plans", start)
    section = text[start:end if end != -1 else len(text)]
    count = 0
    for ln in section.splitlines():
        s = ln.strip()
        if re.match(r"^\d+[.)]\s", s) and "✅" not in s and "~~" not in s:
            count += 1
    return count


def scan_roadmap(repo_dir) -> Roadmap:
    repo_dir = Path(repo_dir)
    text = None
    source = None
    for rel in _ROADMAP_FILES:
        f = repo_dir / rel
        if f.exists():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                source = rel
                break
            except OSError:
                continue
    if text is None:
        return Roadmap(found=False)

    fps: dict = {}
    for m in _FP_RE.finditer(text):
        fid = m.group(0)
        prev = fps.get(fid)
        if prev is not None and prev != "listed":
            continue  # already have a definitive status; first one wins
        # Classify from the rest of the CURRENT line first (covers table rows,
        # where the status is in the same row). Only fall back to a short
        # forward window (covers '### FP-001' headings with status in the body)
        # when the line itself has no status keyword -- otherwise the window
        # bleeds into the NEXT FP's row and mis-classifies.
        line_end = text.find("\n", m.end())
        line_rest = text[m.end(): line_end if line_end != -1 else len(text)]
        status = _classify(line_rest)
        if status == "listed":
            status = _classify(text[m.end(): m.end() + 300])
        # A definitive status upgrades an earlier statusless ("listed") prose
        # mention (e.g. "FP-002 features" appears before its status row).
        if prev is None or status != "listed":
            fps[fid] = status

    by_status: dict = {}
    for fid, st in fps.items():
        by_status.setdefault(st, []).append(fid)
    for v in by_status.values():
        v.sort(key=lambda s: int(s.split("-")[1]))

    return Roadmap(
        found=bool(fps),
        source=source,
        fps=fps,
        by_status=by_status,
        open_backlog=_count_open_backlog(repo_dir),
    )


def format_roadmap(r: Roadmap) -> list:
    """Lines for the audit's roadmap section (empty list if nothing found)."""
    if not r.found:
        return []
    L = ["--- target roadmap (from %s) ---" % (r.source or "?")]
    if r.open_backlog is not None:
        L.append(f"  open backlog items      : {r.open_backlog}")
    order = ["active", "shipped", "concluded", "parked", "listed"]
    for st in order:
        ids = r.by_status.get(st)
        if ids:
            L.append(f"  {st:9}: {len(ids):>2}  ({', '.join(ids)})")
    return L
