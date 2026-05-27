"""Project Manager: survey a target repo, classify actionable work, and
schedule orchestrator runs from the CLI.

The fix loop only acts on *failing tests*. Everything else a repo could
work on (open backlog items, the FP roadmap, deferred tests) needs a human
or a deliberate Claude task. The PM pulls all of those signals together
into one prioritized plan, says what the orchestrator can do *now* versus
what needs a person, and can register an unattended run via Windows Task
Scheduler.

Pure analysis (`build_plan` / `format_plan`) is offline and deterministic
-- it reuses `worklist.scan_worklist` plus an optional live pytest scan to
count auto-fixable failures. Scheduling (`build_schtasks_argv` /
`create_scheduled_fix` / `list_scheduled` / `delete_scheduled`) is a thin,
testable wrapper over `schtasks.exe`.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .worklist import scan_worklist

# Prefix for every task this tool registers, so list/delete can scope to ours.
TASK_PREFIX = "Orchestrator"


@dataclass
class PlanItem:
    category: str            # "auto-fixable" | "needs-human" | "deferred"
    ident: str               # "#011", "FP-008", "tests"
    title: str
    priority: str = ""       # HIGH | MEDIUM | LOW | "" | status (for FPs)
    action: str = ""         # recommended next step (human-readable)


@dataclass
class PMPlan:
    repo_dir: str
    auto_fixable: list = field(default_factory=list)   # PlanItem (failing tests)
    needs_human: list = field(default_factory=list)    # PlanItem (backlog + FPs)
    deferred_test_count: int = 0
    failing_test_count: Optional[int] = None           # None = not scanned
    backlog_source: Optional[str] = None

    @property
    def has_auto_work(self) -> bool:
        return bool(self.failing_test_count)

    @property
    def recommended(self) -> str:
        """One-line headline recommendation."""
        if self.failing_test_count is None:
            return ("Run a test scan (or a fix pass) to discover what the "
                    "orchestrator can auto-fix right now.")
        if self.failing_test_count > 0:
            return (f"{self.failing_test_count} failing test(s) are "
                    f"auto-fixable now -- run or schedule a fix pass.")
        if self.needs_human:
            top = self.needs_human[0]
            return (f"Suite is green; no auto-work. Highest-value human/Claude "
                    f"task: {top.ident} {top.title}".rstrip())
        return "Suite is green and the backlog is empty -- nothing queued."


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _count_failing(repo_dir: Path) -> Optional[int]:
    """Run the fast-lane suite once and return failed+errored count.

    Returns None if the suite can't be run (so the PM degrades gracefully
    rather than blocking)."""
    try:
        from . import harness
        run = harness.run_pytest(repo_dir, lane="fast")
    except Exception:
        return None
    return int(getattr(run, "n_failed", 0)) + int(getattr(run, "n_errors", 0))


def build_plan(repo_dir, project_root=None, *, with_tests: bool = False) -> PMPlan:
    """Survey the target repo and classify actionable work.

    with_tests=True runs the fast-lane suite once to count auto-fixable
    failures (slow, ~tens of seconds); otherwise failing_test_count is None.
    """
    repo_dir = Path(repo_dir)
    w = scan_worklist(repo_dir)

    needs_human: list = []
    for it in w.backlog_open:
        needs_human.append(PlanItem(
            category="needs-human", ident=it.ident, title=it.title,
            priority=it.priority,
            action="deliberate Claude/human task (not the auto-fixer)",
        ))
    for it in w.fps:
        needs_human.append(PlanItem(
            category="needs-human", ident=it.ident, title=it.title,
            priority=it.status,
            action="future-plan slice -- needs a human to scope/seed",
        ))

    failing = _count_failing(repo_dir) if with_tests else None
    auto_fixable: list = []
    if failing:
        auto_fixable.append(PlanItem(
            category="auto-fixable", ident="tests",
            title=f"{failing} failing test(s)",
            action="orchestrator fix loop can attempt these now",
        ))

    return PMPlan(
        repo_dir=str(repo_dir),
        auto_fixable=auto_fixable,
        needs_human=needs_human,
        deferred_test_count=w.skipped_tests,
        failing_test_count=failing,
        backlog_source=w.backlog_source,
    )


def format_plan(plan: PMPlan) -> str:
    L = ["=== Project Manager: work plan ===",
         f"target repo: {plan.repo_dir}",
         ""]

    L.append("[1] auto-fixable now (failing tests -> orchestrator fix loop)")
    if plan.failing_test_count is None:
        L.append("    (not scanned -- choose 'scan tests' to detect)")
    elif not plan.auto_fixable:
        L.append("    none -- suite is green")
    else:
        for it in plan.auto_fixable:
            L.append(f"    {it.title}: {it.action}")

    L.append("")
    src = f" ({plan.backlog_source})" if plan.backlog_source else ""
    L.append(f"[2] needs a human / Claude task{src}")
    if not plan.needs_human:
        L.append("    none")
    else:
        for it in plan.needs_human:
            tag = f"{it.priority:8}" if it.priority else " " * 8
            title = f" {it.title}" if it.title else ""
            L.append(f"    {tag} {it.ident}{title}")

    L.append("")
    L.append(f"[3] deferred tests (skip/xfail): {plan.deferred_test_count}")
    L.append("")
    L.append(f">> {plan.recommended}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Scheduling (Windows Task Scheduler)
# ---------------------------------------------------------------------------

_VALID_SCHED = {"DAILY", "HOURLY", "WEEKLY", "ONCE", "ONLOGON", "ONSTART"}


def build_fix_command(repo_dir, python: Optional[str] = None) -> str:
    """The command Task Scheduler should run: one orchestrator fix pass."""
    py = python or sys.executable
    return f'"{py}" -m orchestrator.cli fix --repo-dir "{repo_dir}"'


def build_schtasks_argv(name: str, command: str, schedule: str,
                        time: Optional[str] = None) -> list:
    """Build the `schtasks /Create` argv (no shell). Raises on bad schedule."""
    sched = schedule.upper()
    if sched not in _VALID_SCHED:
        raise ValueError(f"schedule must be one of {sorted(_VALID_SCHED)}, got {schedule!r}")
    tn = name if name.startswith(TASK_PREFIX) else f"{TASK_PREFIX}-{name}"
    argv = ["schtasks", "/Create", "/TN", tn, "/TR", command, "/SC", sched, "/F"]
    if time and sched in {"DAILY", "WEEKLY", "ONCE"}:
        argv += ["/ST", time]
    return argv


def _run_schtasks(argv: list) -> tuple:
    """Run a schtasks argv; return (ok, combined_output)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, "schtasks.exe not found (Windows-only feature)."
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out.strip()


def create_scheduled_fix(repo_dir, schedule: str, time: Optional[str] = None,
                         name: str = "NightlyFix", python: Optional[str] = None) -> tuple:
    """Register an unattended orchestrator fix run. Returns (ok, output)."""
    cmd = build_fix_command(repo_dir, python=python)
    argv = build_schtasks_argv(name, cmd, schedule, time)
    return _run_schtasks(argv)


def list_scheduled() -> tuple:
    """List this tool's scheduled tasks (best-effort filter by prefix)."""
    ok, out = _run_schtasks(["schtasks", "/Query", "/FO", "LIST"])
    if not ok:
        return ok, out
    blocks, keep = [], []
    for line in out.splitlines():
        if line.startswith("TaskName:"):
            if keep:
                blocks.append("\n".join(keep))
            keep = [line] if TASK_PREFIX in line else []
        elif keep:
            keep.append(line)
    if keep:
        blocks.append("\n".join(keep))
    return True, ("\n\n".join(blocks) if blocks else "(no Orchestrator-* scheduled tasks)")


def delete_scheduled(name: str) -> tuple:
    tn = name if name.startswith(TASK_PREFIX) else f"{TASK_PREFIX}-{name}"
    return _run_schtasks(["schtasks", "/Delete", "/TN", tn, "/F"])
