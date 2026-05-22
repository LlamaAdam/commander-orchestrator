"""orch CLI -- status, health, events tail, triage, fix, pending.

Usage:
  python -m orchestrator.cli status [--json]
  python -m orchestrator.cli health [--model haiku|sonnet|opus] [--dry-run]
  python -m orchestrator.cli events tail -n 30
  python -m orchestrator.cli triage --skip-clone [--max-failures N]
  python -m orchestrator.cli fix --skip-clone [--max-failures N] [--dry-run]
  python -m orchestrator.cli pending             # show data/needs_human.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional


def _cmd_status(args):
    from .status import get_status_snapshot, format_status_human
    snap = get_status_snapshot(project_root=args.project_root)
    if args.json:
        print(json.dumps(asdict(snap), indent=2, default=str))
    else:
        print(format_status_human(snap))
    return 0


def _cmd_health(args):
    from .health import generate_health_report
    if args.dry_run:
        print(f"[health] dry-run -- building prompt only, no Claude call")
    else:
        print(f"[health] reviewing last {args.max_events} events with model={args.model}...")
    report = generate_health_report(
        project_root=args.project_root, max_events=args.max_events,
        model=args.model, write_to=Path(args.out) if args.out else None,
        dry_run=args.dry_run,
    )
    if not report.success:
        print(f"[health] FAILED: {report.error}", file=sys.stderr)
        return 1
    if report.written_to:
        print(f"[health] wrote {report.written_to}")
    print(
        f"[health] events_reviewed={report.n_events_reviewed} "
        f"in/out_toks={report.input_tokens}/{report.output_tokens} "
        f"cost=${report.cost_usd:.4f} duration={report.duration_seconds:.1f}s"
    )
    if args.print_report:
        print()
        print(report.markdown)
    return 0


def _cmd_report(args):
    from .report import build_report, format_report
    r = build_report(project_root=args.project_root)
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        print(format_report(r))
    return 0


def _cmd_events_tail(args):
    path = Path(args.project_root) / "data" / "events.jsonl"
    if not path.exists():
        print(f"events log not found: {path}", file=sys.stderr)
        return 1
    lines = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                lines.append(line.rstrip())
    for line in lines[-args.n:]:
        if args.json:
            print(line); continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            print(line); continue
        ts = ev.get("timestamp")
        when = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                if isinstance(ts, (int, float)) else "?")
        ev_type = ev.get("event", "?")
        rest = {k: v for k, v in ev.items() if k not in ("event", "timestamp")}
        print(f"{when}  {ev_type:18s}  {json.dumps(rest, default=str)}")
    return 0


def _cmd_triage(args):
    from .triage_failures import triage_failures
    from .router import Router
    repo_dir = Path(args.repo_dir)
    if args.skip_clone and not repo_dir.exists():
        print(f"--skip-clone set but {repo_dir} does not exist", file=sys.stderr)
        return 1
    router_kwargs = {}
    if args.claude_model:
        router_kwargs["claude_model"] = args.claude_model
    router = Router(**router_kwargs)
    print(f"[triage] repo={repo_dir} lane={args.lane} skip_clone={args.skip_clone} "
          f"max_failures={args.max_failures}")
    result = triage_failures(
        repo_dir=repo_dir, repo_url=args.repo_url, branch=args.branch,
        lane=args.lane,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        skip_clone=args.skip_clone, update_clone=args.update_clone,
        max_failures=args.max_failures, router=router,
    )
    if not result.success:
        print(f"[triage] FAILED: {result.error}", file=sys.stderr)
        return 1
    tr = result.test_run
    print(f"[triage] tests: {tr.n_passed}/{tr.n_failed}/{tr.n_errors}/{tr.n_skipped} "
          f"({tr.duration_seconds:.1f}s)")
    print(f"[triage] failures: {result.total_failures} "
          f"(local={result.routed_to_local}, claude={result.routed_to_claude}, "
          f"blocked={result.blocked_by_quota})")
    for tf in result.triaged[:5]:
        print(f"  - {tf.failure.nodeid[:60]:60s} -> {tf.task_result.handler}")
    if len(result.triaged) > 5:
        print(f"  ... ({len(result.triaged) - 5} more)")
    return 0


def _cmd_fix(args):
    """Autonomous fix loop: bundle -> route -> JSON action -> apply -> verify."""
    from .triage_failures import triage_failures  # to reuse clone+test step
    from .auto_fix import auto_fix_failures, load_danger_list
    from .router import Router
    from .harness import run_pytest

    repo_dir = Path(args.repo_dir)
    project_root = Path(args.project_root)

    if not repo_dir.exists():
        print(f"repo_dir does not exist: {repo_dir}", file=sys.stderr)
        return 1

    # Burn ceiling guard
    if args.burn_ceiling > 0:
        today_burn = _today_burn(project_root)
        if today_burn >= args.burn_ceiling:
            print(f"[fix] today's reported burn ${today_burn:.2f} >= "
                  f"ceiling ${args.burn_ceiling:.2f} -- forcing local-only "
                  f"by setting Router's claude_model to a sentinel won't work; "
                  f"instead, just stopping this cycle.", file=sys.stderr)
            # Soft stop: skip this cycle's Claude routing by hard-stopping the run.
            # (Caller -- run_continuous.py -- catches return code 2 as 'burn-stop'.)
            return 2

    router_kwargs = {}
    if args.claude_model:
        router_kwargs["claude_model"] = args.claude_model
    router = Router(**router_kwargs)

    # 1. Run pytest to collect failures (uses --skip-clone if set).
    if not args.skip_clone:
        # Use triage_failures' clone path for consistency, but discard its
        # triage results -- we want auto_fix to do the routing.
        # Simplest: just call run_pytest directly; user can run `orch triage`
        # separately if they want a non-fixing pass.
        pass
    print(f"[fix] running pytest fast lane on {repo_dir}...")
    test_run = run_pytest(repo_dir, lane=args.lane)
    print(f"[fix] tests: {test_run.n_passed}/{test_run.n_failed}/"
          f"{test_run.n_errors}/{test_run.n_skipped} "
          f"({test_run.duration_seconds:.1f}s)")
    if not test_run.failures:
        print("[fix] no failures -- nothing to fix.")
        return 0

    danger = load_danger_list(project_root)
    print(f"[fix] danger_list: {len(danger)} patterns")
    print(f"[fix] attempting fix on {len(test_run.failures)} failure(s) "
          f"(max_failures={args.max_failures}, dry_run={args.dry_run})...")

    run = auto_fix_failures(
        failures=test_run.failures,
        repo_dir=repo_dir,
        project_root=project_root,
        router=router,
        danger_patterns=danger,
        dry_run=args.dry_run,
        max_failures=args.max_failures,
        enable_claude_retry=not args.no_claude_retry,
        verify_mode=args.verify_mode,
    )

    print(f"[fix] result: fixed={run.n_fixed} already_fixed={run.n_already_fixed} "
          f"escalated={run.n_escalated} regressed={run.n_regressed} "
          f"skipped_dedup={run.n_skipped_dedup} skipped_capped={run.n_skipped_capped} "
          f"claude_retried={run.n_claude_retried} graduated={run.n_graduated} "
          f"(total {len(run.attempts)} attempts, {run.duration_seconds:.1f}s)")
    for a in run.attempts[:8]:
        line = f"  - {a.failure_nodeid[:55]:55s} {a.status:16s} handler={a.handler or '-'}"
        if a.claude_retry_used:
            line += " [tier2]"
        if a.action and a.action.action:
            line += f"  action={a.action.action}"
            if a.action.action == "install_package" and a.action.package:
                line += f"  pkg={a.action.package}"
        if a.reason:
            line += f"  ({a.reason[:60]})"
        print(line)
    if len(run.attempts) > 8:
        print(f"  ... ({len(run.attempts) - 8} more)")
    return 0


def _cmd_pending(args):
    path = Path(args.project_root) / "data" / "needs_human.md"
    if not path.exists():
        print("data/needs_human.md does not exist -- nothing pending.")
        return 0
    if args.count:
        text = path.read_text(encoding="utf-8", errors="replace")
        n = text.count("\n## ")
        print(f"{n} pending escalations in {path}")
        return 0
    print(path.read_text(encoding="utf-8", errors="replace"))
    return 0


def _today_burn(project_root: Path) -> float:
    """Sum cost_usd_reported across claude_call events from today (local time)."""
    events_path = Path(project_root) / "data" / "events.jsonl"
    if not events_path.exists():
        return 0.0
    today_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    total = 0.0
    try:
        with events_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") != "claude_call":
                    continue
                ts = ev.get("timestamp", 0)
                if not (isinstance(ts, (int, float)) and ts >= today_start):
                    continue
                total += float(ev.get("cost_usd_reported", 0.0) or 0.0)
    except OSError:
        pass
    return total


def build_parser():
    p = argparse.ArgumentParser(prog="orch",
                                description="Orchestrator CLI")
    p.add_argument("--project-root", default=".")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="Quota/Ollama/event summary")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_status)

    rp = sub.add_parser("report", help="Roll up events.jsonl into activity metrics")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(func=_cmd_report)

    h = sub.add_parser("health", help="Claude self-review of routing")
    h.add_argument("--max-events", type=int, default=100)
    h.add_argument("--model", default="haiku")
    h.add_argument("--out", default=None)
    h.add_argument("--print-report", action="store_true")
    h.add_argument("--dry-run", action="store_true")
    h.set_defaults(func=_cmd_health)

    e = sub.add_parser("events", help="Inspect events.jsonl")
    es = e.add_subparsers(dest="subcommand", required=True)
    et = es.add_parser("tail", help="Last N events")
    et.add_argument("-n", type=int, default=20)
    et.add_argument("--json", action="store_true")
    et.set_defaults(func=_cmd_events_tail)

    t = sub.add_parser("triage", help="Harness + route each failure")
    t.add_argument("--repo-url",
                   default="https://github.com/LlamaAdam/commander-builder.git")
    t.add_argument("--branch", default="feature/2026-04-28-session")
    t.add_argument("--repo-dir", default="data/repos/commander-builder")
    t.add_argument("--lane", choices=["fast", "slow"], default="fast")
    t.add_argument("--output-dir", default=None)
    t.add_argument("--skip-clone", action="store_true")
    t.add_argument("--update-clone", action="store_true")
    t.add_argument("--max-failures", type=int, default=None)
    t.add_argument("--claude-model", default=None)
    t.set_defaults(func=_cmd_triage)

    fx = sub.add_parser("fix", help="Autonomous fix loop (apply + verify)")
    fx.add_argument("--repo-dir", default="data/repos/commander-builder")
    fx.add_argument("--lane", choices=["fast", "slow"], default="fast")
    fx.add_argument("--skip-clone", action="store_true", default=True,
                    help="(default) don't touch the repo's git state")
    fx.add_argument("--max-failures", type=int, default=None)
    fx.add_argument("--dry-run", action="store_true",
                    help="Get the action plan; do not apply")
    fx.add_argument("--claude-model", default=None)
    fx.add_argument("--burn-ceiling", type=float, default=5.0,
                    help="Soft cap on today's reported Claude burn (default $5)")
    fx.add_argument("--no-claude-retry", action="store_true",
                    help="Disable tier-2 Claude fallback after local fails")
    fx.add_argument("--verify-mode", action="store_true",
                    help="Have Claude verify each local proposal before applying; "
                         "auto-graduate an action-type to local-only after N "
                         "verified successes (see VERIFY_GRADUATION_THRESHOLD)")
    fx.set_defaults(func=_cmd_fix)

    pe = sub.add_parser("pending", help="Show data/needs_human.md")
    pe.add_argument("--count", action="store_true",
                    help="Just count escalations")
    pe.set_defaults(func=_cmd_pending)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
