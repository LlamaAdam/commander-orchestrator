"""Time-boxed curation batch driver -- generates labeled knowledge_log rows.

Round-robins the original [USER] decks, rotating advisor source and curation
mode for proposal diversity, running commander-auto-curate --run-sim each
iteration. Each successful run appends one labeled iteration row (proposal +
empirical Forge A/B verdict) to the knowledge_log, which unblocks FP-002 (ML
predictor, needs 200+ rows / 5+ decks) and FP-012 (autonomous agent, >=150).

The curator runs under the Claude Max subscription via commander-builder's
CLI adapter (no API key needed).

Resumable: progress is the knowledge_log row count itself; just relaunch.
Robust: per-run timeout + continue-on-error so one bad iteration can't stall
the batch. Time-boxed: stops cleanly at --minutes.

Usage (from the orchestrator project root, venv active):
    python scripts/curate_batch.py --minutes 60 --sim-games 2

Flags:
    --minutes 60          wall-clock budget for this slot
    --target-rows N       stop early once knowledge_log reaches N rows
    --sim-games 2         games per Forge A/B sim (more = less noise, slower)
    --per-run-timeout 420 seconds before killing a single auto-curate run
    --repo-dir            commander-builder checkout (default C:/dev/commander-builder)
    --decks-subdir        deck dir under repo (default vendor/forge/userdata/decks/commander)
    --sleep 5             polite seconds between runs
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

# Advisor-source / curation-mode combos, rotated for proposal diversity. We
# keep claude OUT of the advisor rotation (the *curator* is already Claude via
# the subscription CLI); heuristic + bracket_peers give cheap, varied candidate
# pools, and polish/overhaul vary how aggressively the curator swaps.
_COMBOS = [
    ("heuristic", "polish"),
    ("bracket_peers", "polish"),
    ("heuristic", "overhaul"),
    ("bracket_peers", "overhaul"),
]

_BRACKET_RE = re.compile(r"\[B([1-5])\]\.dck$")
_VERSION_RE = re.compile(r"\sv\d+\s")  # excludes generated "... v2 ..." decks


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


def _row_count(db_path: Path) -> int:
    try:
        c = sqlite3.connect(str(db_path))
        try:
            return c.execute("SELECT COUNT(*) FROM iterations").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return -1


def _discover_decks(deck_dir: Path) -> list[tuple[Path, int]]:
    """Original [USER] decks (excludes generated 'v2/v3' variants), with bracket."""
    out: list[tuple[Path, int]] = []
    for p in sorted(deck_dir.glob("[[]USER[]]*.dck")):
        if _VERSION_RE.search(p.name) or "DETUNE" in p.name:
            continue  # skip generated variants (curated 'v2', detuner scratch)
        m = _BRACKET_RE.search(p.name)
        bracket = int(m.group(1)) if m else 3
        out.append((p, bracket))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--target-rows", type=int, default=None)
    ap.add_argument("--sim-games", type=int, default=2)
    ap.add_argument("--per-run-timeout", type=int, default=420)
    ap.add_argument("--repo-dir", default=r"C:\dev\commander-builder")
    ap.add_argument("--decks-subdir",
                    default=r"vendor\forge\userdata\decks\commander")
    ap.add_argument("--sleep", type=float, default=5.0)
    ap.add_argument("--log-dir", default="data/curate_runs")
    args = ap.parse_args()

    repo_dir = Path(args.repo_dir)
    deck_dir = repo_dir / args.decks_subdir
    db_path = repo_dir / "knowledge_log.sqlite"

    log_dir = PROJECT_ROOT / args.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"curate_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    curate_exe = shutil.which("commander-auto-curate")
    if curate_exe is None:
        # Fall back to the venv Scripts dir relative to the running interpreter.
        cand = Path(sys.executable).parent / "commander-auto-curate.exe"
        curate_exe = str(cand) if cand.exists() else None
    if curate_exe is None:
        print("ERROR: commander-auto-curate not found on PATH", file=sys.stderr)
        return 1

    if not deck_dir.exists():
        print(f"ERROR: deck dir not found: {deck_dir}", file=sys.stderr)
        return 1

    decks = _discover_decks(deck_dir)
    if not decks:
        print(f"ERROR: no [USER] decks in {deck_dir}", file=sys.stderr)
        return 1

    end_time = time.time() + args.minutes * 60
    start_rows = _row_count(db_path)

    _log("=" * 68, log_file)
    _log("curation batch starting", log_file)
    _log(f"  minutes:        {args.minutes}", log_file)
    _log(f"  target_rows:    {args.target_rows}", log_file)
    _log(f"  sim_games:      {args.sim_games}", log_file)
    _log(f"  decks:          {len(decks)}", log_file)
    _log(f"  db:             {db_path}", log_file)
    _log(f"  start_rows:     {start_rows}", log_file)
    _log(f"  log_file:       {log_file}", log_file)
    _log("=" * 68, log_file)

    n_runs = 0
    n_ok = 0
    n_fail = 0
    verdicts: dict[str, int] = {}
    deck_idx = 0
    combo_idx = 0

    try:
        while time.time() < end_time:
            if args.target_rows is not None:
                cur = _row_count(db_path)
                if cur >= args.target_rows:
                    _log(f"target rows reached ({cur} >= {args.target_rows}) -- stopping", log_file)
                    break

            deck_path, bracket = decks[deck_idx % len(decks)]
            source, mode = _COMBOS[combo_idx % len(_COMBOS)]
            deck_idx += 1
            combo_idx += 1
            n_runs += 1

            rows_before = _row_count(db_path)
            _log("", log_file)
            _log(f"--- run {n_runs}: {deck_path.name} (B{bracket}) "
                 f"source={source} mode={mode} ---", log_file)

            cmd = [
                curate_exe, str(deck_path),
                "--bracket", str(bracket),
                "--source", source,
                "--mode", mode,
                "--run-sim",
                "--sim-games", str(args.sim_games),
                "--db-path", str(db_path),
            ]
            t0 = time.time()
            try:
                proc = subprocess.run(
                    cmd, cwd=str(repo_dir), capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=args.per_run_timeout,
                )
                dur = time.time() - t0
                rc = proc.returncode
                tail = "\n".join((proc.stdout or "").splitlines()[-6:])
                # Pull the verdict line if present.
                vmatch = re.search(r"verdict:\s*(\w+)", proc.stdout or "")
                verdict = vmatch.group(1) if vmatch else "?"
            except subprocess.TimeoutExpired:
                dur = time.time() - t0
                rc = -1
                tail = f"(timed out after {args.per_run_timeout}s)"
                verdict = "timeout"

            rows_after = _row_count(db_path)
            added = rows_after - rows_before if rows_after >= 0 and rows_before >= 0 else 0

            if rc == 0 and added > 0:
                n_ok += 1
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
                _log(f"  OK rc=0 dur={dur:.0f}s verdict={verdict} "
                     f"rows {rows_before}->{rows_after}", log_file)
            else:
                n_fail += 1
                _log(f"  FAIL rc={rc} dur={dur:.0f}s rows {rows_before}->{rows_after}", log_file)
                _log(f"  tail: {tail[:500]}", log_file)

            if time.time() >= end_time:
                break
            time.sleep(args.sleep)

    except KeyboardInterrupt:
        _log("KeyboardInterrupt -- stopping batch", log_file)

    end_rows = _row_count(db_path)
    _log("", log_file)
    _log("=" * 68, log_file)
    _log(f"batch finished: runs={n_runs} ok={n_ok} fail={n_fail}", log_file)
    _log(f"  rows: {start_rows} -> {end_rows}  (+{end_rows - start_rows})", log_file)
    _log(f"  verdict breakdown: {verdicts}", log_file)
    _log("=" * 68, log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
