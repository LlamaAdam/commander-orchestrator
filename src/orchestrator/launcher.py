"""Easy-run launcher for the orchestrator.

This is the "lightweight launcher" entry point: a thin, dependency-free
wrapper over the `orch` CLI + `scripts/run_continuous.py` so the loop can
be driven without remembering flags. It is exposed as a console script
(`orch-launcher`), so `pip install -e .` generates a real, double-clickable
`.venv/Scripts/orch-launcher.exe` that re-uses this machine's venv Python.
A repo-root `Orchestrator.cmd` points at it for discoverability.

Two modes:

  * No subcommand  -> interactive text menu (set target repo, audit, work,
    selftest, status, single fix pass, continuous run).
  * A subcommand   -> run it directly (scriptable / testable), e.g.::

        orch-launcher set-repo C:\\dev\\commander-builder
        orch-launcher show
        orch-launcher audit
        orch-launcher run --hours 1

The chosen target repo (and a few run defaults) persist to
``data/launcher_config.json`` so the selection sticks between runs.

NOTE: this only *launches* the orchestrator; the CLI itself enforces the
subscription-auth invariant (it scrubs ANTHROPIC_API_KEY before invoking
Claude), so the launcher passes the environment through unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# The package lives at <project_root>/src/orchestrator/launcher.py, so the
# project root (which holds data/, scripts/, the venv) is two parents up.
# Allow an override for unusual layouts (e.g. a future bundled build).
PROJECT_ROOT = Path(
    os.environ.get("ORCH_PROJECT_ROOT", Path(__file__).resolve().parents[2])
)
CONFIG_PATH = PROJECT_ROOT / "data" / "launcher_config.json"

_DEFAULT_CONFIG = {
    "repo_dir": "data/repos/commander-builder",
    "branch": "feature/2026-04-28-session",
    "repo_url": "https://github.com/LlamaAdam/commander-builder.git",
    "hours": 1.0,
    "max_failures": None,
    "burn_ceiling": 5.0,
}


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass  # missing/corrupt -> defaults
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def resolve_repo_dir(cfg: dict) -> Path:
    """Absolute path to the configured target repo.

    Relative paths are resolved against the project root (matching the CLI
    and run_continuous behavior)."""
    rd = Path(cfg.get("repo_dir") or _DEFAULT_CONFIG["repo_dir"])
    return rd if rd.is_absolute() else (PROJECT_ROOT / rd)


def describe_repo(cfg: dict) -> str:
    rd = resolve_repo_dir(cfg)
    if not rd.exists():
        state = "MISSING - clone it or pick another (menu option 1)"
    elif not (rd / ".git").exists():
        state = "exists (not a git repo)"
    else:
        state = "ready"
    return f"{rd}  [{state}]"


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _run(args: list[str]) -> int:
    """Run a child process from the project root, streaming its output."""
    print(f"\n$ {' '.join(args)}\n", flush=True)
    try:
        return subprocess.call(args, cwd=str(PROJECT_ROOT))
    except FileNotFoundError as exc:
        print(f"[launcher] failed to launch: {exc}", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        print("\n[launcher] interrupted.", file=sys.stderr)
        return 130


def _orch(cfg: dict, command: str, *extra: str) -> int:
    """Run an `orch <command>` subcommand against the configured repo."""
    repo = str(resolve_repo_dir(cfg))
    return _run([sys.executable, "-m", "orchestrator.cli", command,
                 "--repo-dir", repo, *extra])


def run_continuous(cfg: dict, hours: Optional[float] = None) -> int:
    repo = str(resolve_repo_dir(cfg))
    hrs = hours if hours is not None else float(cfg.get("hours", 1.0))
    args = [sys.executable, str(PROJECT_ROOT / "scripts" / "run_continuous.py"),
            "--hours", str(hrs), "--repo-dir", repo,
            "--burn-ceiling", str(cfg.get("burn_ceiling", 5.0))]
    if cfg.get("max_failures"):
        args += ["--max-failures", str(cfg["max_failures"])]
    return _run(args)


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

_MENU = """
=== Commander Orchestrator ===
target repo: {repo}

  1) Set target repo to work on
  2) Preflight audit (subsystems + bug/backlog state)
  3) Show work list (open backlog + future plans + skipped tests)
  4) Self-test (own suite + audit -> health verdict)
  5) Status (quota / Ollama / recent activity)
  6) Single fix pass (one cycle, then stop)
  7) Run continuous fix loop (N hours)
  8) Project Manager (plan work + schedule runs)
  0) Quit
"""


def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{msg}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return ans or default


def _set_repo(cfg: dict) -> None:
    print(f"\nCurrent: {resolve_repo_dir(cfg)}")
    new = _prompt("New target repo path (blank to keep)")
    if not new:
        return
    p = Path(new).expanduser()
    if not p.exists():
        keep = _prompt(f"'{p}' does not exist yet. Save anyway? (y/N)", "n")
        if keep.lower() not in ("y", "yes"):
            print("Unchanged.")
            return
    elif not (p / ".git").exists():
        print("Warning: that folder is not a git repo (no .git). Saved anyway.")
    cfg["repo_dir"] = str(p)
    save_config(cfg)
    print(f"Saved. Target repo -> {resolve_repo_dir(cfg)}")


_SCHED_CHOICES = {
    "1": ("DAILY", "02:00"),
    "2": ("DAILY", None),    # ask time
    "3": ("WEEKLY", "02:00"),
    "4": ("ONLOGON", None),
}


def _project_manager(cfg: dict) -> None:
    """Survey the repo, show a classified work plan, and offer to run or
    schedule the auto-fixable work (option 8 / `pm` subcommand)."""
    from . import pm

    repo = resolve_repo_dir(cfg)
    if not repo.exists():
        print(f"Target repo does not exist: {repo}  (set it via menu option 1)")
        return

    scan = _prompt("Scan the test suite now to find auto-fixable work? "
                   "(slower) (y/N)", "n").lower() in ("y", "yes")
    if scan:
        print("Running the fast-lane suite once to count failing tests ...")
    plan = pm.build_plan(repo, PROJECT_ROOT, with_tests=scan)
    print()
    print(pm.format_plan(plan))

    print("\n--- what would you like to do? ---")
    print("  1) Run a fix pass now (attempt the auto-fixable tests)")
    print("  2) Schedule an unattended nightly/at-time fix run")
    print("  3) List scheduled orchestrator tasks")
    print("  4) Remove a scheduled orchestrator task")
    print("  0) Back")
    choice = _prompt("Choose", "0")

    if choice == "1":
        _orch(cfg, "fix")
    elif choice == "2":
        _schedule_flow(cfg, pm, repo)
    elif choice == "3":
        ok, out = pm.list_scheduled()
        print(out)
    elif choice == "4":
        name = _prompt("Task name to remove (e.g. NightlyFix)")
        if name:
            ok, out = pm.delete_scheduled(name)
            print(out or ("removed" if ok else "failed"))


def _schedule_flow(cfg: dict, pm, repo: Path) -> None:
    print("\nWhen should the fix pass run?")
    print("  1) Daily at 02:00")
    print("  2) Daily at a time you choose")
    print("  3) Weekly at 02:00")
    print("  4) At each logon")
    pick = _prompt("Choose", "1")
    if pick not in _SCHED_CHOICES:
        print("Cancelled.")
        return
    sched, time = _SCHED_CHOICES[pick]
    if time is None and sched == "DAILY":
        time = _prompt("Time (HH:MM, 24h)", "02:00")
    name = _prompt("Task name", "NightlyFix") or "NightlyFix"
    ok, out = pm.create_scheduled_fix(str(repo), sched, time, name=name)
    print(out or ("scheduled" if ok else "failed"))
    if ok:
        print(f"-> registered '{pm.TASK_PREFIX}-{name}'. It runs:\n   "
              f"{pm.build_fix_command(str(repo))}")


def interactive(cfg: dict) -> int:
    while True:
        print(_MENU.format(repo=describe_repo(cfg)))
        choice = _prompt("Choose")
        if choice in ("0", "q", "quit", "exit"):
            print("Bye.")
            return 0
        elif choice == "1":
            _set_repo(cfg)
        elif choice == "2":
            _orch(cfg, "audit")
        elif choice == "3":
            _orch(cfg, "work")
        elif choice == "4":
            _orch(cfg, "selftest")
        elif choice == "5":
            _run([sys.executable, "-m", "orchestrator.cli", "status"])
        elif choice == "6":
            _orch(cfg, "fix", "--max-failures", "1")
        elif choice == "7":
            hrs = _prompt("How many hours", str(cfg.get("hours", 1.0)))
            try:
                hours = float(hrs)
            except ValueError:
                print("Not a number; skipping.")
                continue
            cfg["hours"] = hours
            save_config(cfg)
            run_continuous(cfg, hours)
        elif choice == "8":
            _project_manager(cfg)
        else:
            print("Unknown choice.")
        input("\n(press Enter to return to menu) ")


# ---------------------------------------------------------------------------
# Argument parsing / dispatch
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orch-launcher",
        description="Easy-run launcher for the orchestrator. "
                    "Run with no subcommand for the interactive menu.")
    sub = p.add_subparsers(dest="command")

    sr = sub.add_parser("set-repo", help="Set the target repo to work on")
    sr.add_argument("path", help="Path to the target git repo")

    sub.add_parser("show", help="Print the current launcher config")
    sub.add_parser("audit", help="Preflight audit against the target repo")
    sub.add_parser("work", help="Show the actionable work list")
    sub.add_parser("selftest", help="Run the orchestrator self-test")
    sub.add_parser("status", help="Quota / Ollama / activity summary")
    sub.add_parser("fix", help="Run a single fix pass (one cycle)")

    rn = sub.add_parser("run", help="Run the continuous fix loop")
    rn.add_argument("--hours", type=float, default=None)

    pmp = sub.add_parser("pm", help="Project Manager: plan work + schedule runs")
    pmp.add_argument("--scan-tests", action="store_true",
                     help="run the suite once to count auto-fixable failures")
    pmsub = pmp.add_subparsers(dest="pm_command")
    psd = pmsub.add_parser("schedule", help="Register an unattended fix run")
    psd.add_argument("--schedule", default="DAILY",
                     help="DAILY | WEEKLY | ONCE | ONLOGON | ONSTART | HOURLY")
    psd.add_argument("--time", default="02:00", help="HH:MM (for DAILY/WEEKLY/ONCE)")
    psd.add_argument("--name", default="NightlyFix")
    pmsub.add_parser("list", help="List scheduled orchestrator tasks")
    pdel = pmsub.add_parser("unschedule", help="Remove a scheduled task")
    pdel.add_argument("name")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()

    cmd = args.command
    if cmd is None:
        return interactive(cfg)
    if cmd == "set-repo":
        p = Path(args.path).expanduser()
        cfg["repo_dir"] = str(p)
        save_config(cfg)
        print(f"Target repo -> {resolve_repo_dir(cfg)}")
        return 0
    if cmd == "show":
        print(f"project root: {PROJECT_ROOT}")
        print(f"config file : {CONFIG_PATH}")
        print(f"target repo : {describe_repo(cfg)}")
        for k in ("branch", "repo_url", "hours", "max_failures", "burn_ceiling"):
            print(f"  {k:13}: {cfg.get(k)}")
        return 0
    if cmd == "status":
        return _run([sys.executable, "-m", "orchestrator.cli", "status"])
    if cmd == "fix":
        return _orch(cfg, "fix", "--max-failures", "1")
    if cmd == "run":
        return run_continuous(cfg, args.hours)
    if cmd in ("audit", "work", "selftest"):
        return _orch(cfg, cmd)
    if cmd == "pm":
        from . import pm
        repo = resolve_repo_dir(cfg)
        pm_cmd = getattr(args, "pm_command", None)
        if pm_cmd == "schedule":
            ok, out = pm.create_scheduled_fix(
                str(repo), args.schedule, args.time, name=args.name)
            print(out or ("scheduled" if ok else "failed"))
            return 0 if ok else 1
        if pm_cmd == "list":
            ok, out = pm.list_scheduled()
            print(out)
            return 0 if ok else 1
        if pm_cmd == "unschedule":
            ok, out = pm.delete_scheduled(args.name)
            print(out or ("removed" if ok else "failed"))
            return 0 if ok else 1
        # No pm subcommand -> print the plan.
        print(pm.format_plan(pm.build_plan(repo, PROJECT_ROOT,
                                           with_tests=args.scan_tests)))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
