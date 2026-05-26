"""Run `orch fix` continuously for N hours.

Behavior:
  - Wakes every --interval seconds.
  - Polls git HEAD on the target repo (unless --skip-poll). If HEAD didn't
    move AND we have no untriaged failures from last cycle, skips the cycle.
  - Each active cycle: invokes `python -m orchestrator.cli fix --skip-clone
    --max-failures N` and tees output to a log file.
  - Periodic health check every N cycles (default every 8 = ~2h with 15-min
    cycles).
  - Exits cleanly after --hours, or on Ctrl+C.
  - Burn ceiling is enforced by the `fix` subcommand itself (exit code 2 =
    burn-stop); we just respect it and skip Claude-routing for the rest of
    the day.

Run from project root with the venv active:

    python scripts\run_continuous.py --hours 12

Optional flags:
    --interval 1800        # seconds between cycles (default 900 = 15 min)
    --max-failures 3       # cap per-cycle (default unlimited)
    --health-every 8       # cycle interval for orch health (default 8)
    --burn-ceiling 5.0     # passed through to `orch fix`
    --no-health            # disable periodic health check
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

# How many consecutive cycles with no useful fix work before we alert the
# user that the loop has nothing to chew on. Banner fires ONCE per idle streak.
IDLE_STREAK_THRESHOLD = 3

# Result-summary regex, e.g.:
#   [fix] result: fixed=1 already_fixed=0 escalated=0 regressed=0 \
#       skipped_dedup=0 skipped_capped=0 claude_retried=0 (total 3 attempts, ...)
# Idle = "the loop did no useful work this cycle" = fixed + already_fixed +
# escalated + regressed == 0. (skipped_dedup / skipped_capped don't count as
# useful work; they're the loop politely declining.)
_FIX_RESULT_RE = re.compile(
    r"\[fix\]\s+result:\s+"
    r"fixed=(\d+)\s+already_fixed=(\d+)\s+escalated=(\d+)\s+regressed=(\d+)"
)


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, log_file: Path) -> None:
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    try:
        with log_file.open("a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _run_subproc(args, log_file: Path) -> tuple[int, list[str]]:
    """Run a subcommand, stream output to console + log file.

    Returns (return_code, captured_lines). Caller can inspect captured_lines
    to drive idle-streak detection (without re-parsing the log file).
    """
    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        _log(f"  subprocess failed to launch: {exc}", log_file)
        return -1, captured

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            captured.append(line)
            print("  " + line, flush=True)
            try:
                with log_file.open("a", encoding="utf-8", errors="replace") as f:
                    f.write("  " + line + "\n")
            except OSError:
                pass
        proc.wait(timeout=3600)
    except KeyboardInterrupt:
        proc.terminate()
        raise
    return proc.returncode, captured


def _did_useful_work(lines: list[str]) -> bool | None:
    """Did this cycle do any useful fix work?

    Parses the `[fix] result:` line. Useful work = fixed + already_fixed +
    escalated + regressed > 0. A cycle that only dedup-skips or cap-skips
    counts as idle (no useful work) -- which is the case the test-count
    based detector missed.

    Also treats the explicit "no failures -- nothing to fix" line as idle.
    Returns None if neither marker is present (e.g. fix subprocess crashed),
    so the caller can decide not to touch the streak.
    """
    for line in lines:
        if "no failures -- nothing to fix" in line:
            return False
        m = _FIX_RESULT_RE.search(line)
        if m:
            fixed, already_fixed, escalated, regressed = (int(g) for g in m.groups())
            return (fixed + already_fixed + escalated + regressed) > 0
    return None


def _emit_idle_event(project_root: Path, *, streak: int, cycle: int) -> None:
    """Append an idle_streak event to data/events.jsonl so post-run analysis
    can see exactly when the loop went quiet."""
    events_path = project_root / "data" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event": "idle_streak",
        "timestamp": time.time(),
        "streak": streak,
        "cycle": cycle,
    }
    try:
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass


def _git_head(repo_dir: Path) -> str:
    """Best-effort current HEAD sha. Empty string on failure."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir), text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
        return out.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _git_fetch(repo_dir: Path) -> bool:
    try:
        subprocess.check_call(
            ["git", "fetch", "--quiet"],
            cwd=str(repo_dir),
            timeout=60,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("--interval", type=int, default=900,
                    help="seconds between cycles (default 900 = 15min)")
    ap.add_argument("--repo-dir", default="data/repos/commander-builder")
    ap.add_argument("--max-failures", type=int, default=None)
    ap.add_argument("--health-every", type=int, default=8,
                    help="run orch health every N cycles")
    ap.add_argument("--no-health", action="store_true")
    ap.add_argument("--health-model", default="haiku")
    ap.add_argument("--burn-ceiling", type=float, default=5.0)
    ap.add_argument("--poll-head", action="store_true",
                    help="only run a cycle when git HEAD changed (pull-based "
                         "workflow). DEFAULT is to run every interval -- pytest "
                         "itself is the work-detector, and `orch fix` runs with "
                         "--skip-clone so HEAD never moves on its own.")
    # Back-compat no-ops: running every cycle is now the default.
    ap.add_argument("--skip-poll", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--always-run", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--log-dir", default="data/continuous")
    ap.add_argument("--stop-when-idle", type=int, default=0, metavar="N",
                    help="exit after N consecutive idle cycles (everything "
                         "fixable is fixed -- no point idling out the full "
                         "--hours). 0 (default) = run the whole window.")
    ap.add_argument("--no-preflight", action="store_true",
                    help="skip the start-up subsystem audit (not recommended)")
    args = ap.parse_args()

    log_dir = PROJECT_ROOT / args.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    repo_dir = (PROJECT_ROOT / args.repo_dir).resolve()
    end_time = time.time() + args.hours * 3600

    _log("=" * 68, log_file)
    _log("continuous run starting", log_file)
    _log(f"  hours:         {args.hours}", log_file)
    _log(f"  interval:      {args.interval}s", log_file)
    _log(f"  repo_dir:      {repo_dir}", log_file)
    _log(f"  max_failures:  {args.max_failures}", log_file)
    _log(f"  burn_ceiling:  ${args.burn_ceiling}", log_file)
    _log(f"  health_every:  {args.health_every} (enabled: {not args.no_health})", log_file)
    _log(f"  log_file:      {log_file}", log_file)
    _log("=" * 68, log_file)

    if not repo_dir.exists():
        _log(f"ERROR: repo_dir does not exist: {repo_dir}", log_file)
        return 1

    # --- Preflight audit: verify subsystems + surface bug/backlog state before
    # committing to a long run. Aborts on a hard FAIL (e.g. claude CLI missing,
    # target not a repo). WARNs (e.g. Ollama down) are logged but don't block. ---
    if not args.no_preflight:
        try:
            from orchestrator.audit import run_audit, format_audit
            report = run_audit(PROJECT_ROOT, repo_dir)
            for line in format_audit(report).splitlines():
                _log(line, log_file)
            if not report.ok:
                _log("ABORT: preflight found blocker(s); fix them or pass "
                     "--no-preflight to override.", log_file)
                return 1
            _log("preflight OK -- starting run.", log_file)
        except Exception as exc:  # noqa: BLE001
            _log(f"WARN: preflight audit errored ({type(exc).__name__}: {exc}); "
                 f"continuing without it.", log_file)

    cycle = 0
    last_head = _git_head(repo_dir)
    _log(f"initial HEAD: {last_head[:8]}", log_file)
    burn_stopped = False  # set True if burn ceiling tripped today
    idle_streak = 0       # consecutive cycles with zero failures
    idle_banner_shown = False  # so we don't repeat the banner every cycle

    try:
        while time.time() < end_time:
            cycle += 1
            _log("", log_file)
            _log(f"===== cycle {cycle} =====", log_file)

            # Default: run a fix cycle every interval (pytest is the work
            # detector). Only gate on HEAD when --poll-head is set for a
            # pull-based workflow.
            should_run = True
            if args.poll_head and not (args.skip_poll or args.always_run):
                _git_fetch(repo_dir)
                head = _git_head(repo_dir)
                if head and head != last_head:
                    _log(f"HEAD moved {last_head[:8]} -> {head[:8]}", log_file)
                    last_head = head
                    should_run = True
                elif cycle == 1:
                    should_run = True  # always run the first cycle
                else:
                    _log(f"HEAD unchanged ({head[:8]}) -- poll-head gating", log_file)
                    should_run = False

            if not should_run:
                _log("skipping cycle (no change)", log_file)
            else:
                fix_cmd = [
                    sys.executable, "-m", "orchestrator.cli", "fix",
                    "--repo-dir", str(repo_dir),
                    "--skip-clone",
                    "--burn-ceiling", str(args.burn_ceiling),
                ]
                if args.max_failures is not None:
                    fix_cmd += ["--max-failures", str(args.max_failures)]
                _log(f"running: {' '.join(fix_cmd)}", log_file)
                rc, fix_lines = _run_subproc(fix_cmd, log_file)
                _log(f"fix exit code: {rc}", log_file)
                if rc == 2:
                    burn_stopped = True
                    _log("burn ceiling tripped -- pausing remaining cycles "
                         "for the next 2h", log_file)
                    # Sleep extra to let the day's reported burn naturally roll
                    # off via your existing quota.py rate-limit mechanics.
                    time.sleep(7200)
                    burn_stopped = False
                    continue

                # Idle-streak detection. A cycle is "idle" when it did no
                # useful fix work (fixed/already_fixed/escalated/regressed all
                # zero) -- this covers both "no failures at all" AND "failures
                # present but all dedup/cap-skipped". After IDLE_STREAK_THRESHOLD
                # in a row, surface a banner so a watching user knows to add
                # tests or push commits.
                did_work = _did_useful_work(fix_lines)
                if did_work is None:
                    # Couldn't parse the fix output (crash?) -- leave streak as is.
                    pass
                elif not did_work:
                    idle_streak += 1
                    _emit_idle_event(PROJECT_ROOT, streak=idle_streak, cycle=cycle)
                    if args.stop_when_idle and idle_streak >= args.stop_when_idle:
                        _log("=" * 68, log_file)
                        _log(f"  [DONE] {idle_streak} consecutive idle cycle(s) -- "
                             f"everything fixable is fixed. Stopping early "
                             f"(--stop-when-idle={args.stop_when_idle}).", log_file)
                        _log("=" * 68, log_file)
                        break
                    if idle_streak >= IDLE_STREAK_THRESHOLD and not idle_banner_shown:
                        banner = "=" * 68
                        _log(banner, log_file)
                        _log(f"  [IDLE] No useful fix work in {idle_streak} consecutive cycles.", log_file)
                        _log("  Everything fixable is fixed; remaining failures are dedup/cap-skipped.", log_file)
                        _log("  Add failing tests or push a commit to exercise the loop.", log_file)
                        _log("  Will not repeat this banner until a non-idle cycle.", log_file)
                        _log(banner, log_file)
                        idle_banner_shown = True
                else:
                    # Useful-work cycle -- reset the streak so a future idle
                    # stretch can re-fire the banner.
                    if idle_streak > 0 or idle_banner_shown:
                        _log(f"  (idle streak reset after {idle_streak} cycle(s))", log_file)
                    idle_streak = 0
                    idle_banner_shown = False

                # Periodic health check
                if (not args.no_health) and cycle % args.health_every == 0:
                    _log("--- periodic health check ---", log_file)
                    health_cmd = [
                        sys.executable, "-m", "orchestrator.cli", "health",
                        "--model", args.health_model,
                    ]
                    _log(f"running: {' '.join(health_cmd)}", log_file)
                    _run_subproc(health_cmd, log_file)

            # Sleep until next cycle (or until end_time).
            remaining = end_time - time.time()
            if remaining <= 0:
                break
            sleep_for = min(args.interval, max(1, int(remaining)))
            _log(f"sleeping {sleep_for}s", log_file)
            for _ in range(sleep_for):
                time.sleep(1)
                if time.time() >= end_time:
                    break

    except KeyboardInterrupt:
        _log("KeyboardInterrupt -- shutting down cleanly", log_file)

    _log("=" * 68, log_file)
    _log(f"continuous run finished after {cycle} cycle(s)", log_file)
    _log(f"  log: {log_file}", log_file)
    _log("=" * 68, log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
