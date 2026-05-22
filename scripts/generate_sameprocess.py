"""Same-process FP-002 dataset generator (the confound fix).

The earlier FP-002 dataset was confounded: 'kept' rows came from detune-RESTORE
(remove game-changers, sim detuned-vs-original) while 'reverted' rows came from
curating already-tuned decks. Two different *processes* => the classifier
learned "which generator made this row" (cards_added/cards_removed dominate),
not deck quality.

Fix: make BOTH verdicts come from the SAME process — curation. We moderately
*detune* a [USER] deck (remove a handful of game-changers so there's room to
improve, but the deck isn't gutted), then run the normal
``commander-auto-curate --run-sim`` on the detuned deck. The pipeline sims
detuned(A)-vs-curated(B) and logs the verdict:
  * kept     => the curator's swaps beat the detuned baseline
  * reverted => they didn't
Both labels are produced by the curator + the same A/B sim, so the swap *shape*
no longer encodes the label.

Varying detune depth spans easy-to-recover (=> more kept) to hard-to-recover
(=> more reverted), giving a naturally balanced mix.

Resumable + time-boxed like curate_batch. Concurrency (a 2nd Forge profile via
COMMANDER_FORGE_DIR) is layered on separately once the methodology is validated.

Usage:
  python scripts/generate_sameprocess.py --minutes 30 --sim-games 4
  python scripts/generate_sameprocess.py --decks 2 --depths 5,8  # quick validate
"""
from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = Path(r"C:\dev\commander-builder")
_BRACKET_RE = re.compile(r"\[B(\d)\]")
_SP_MARKER = "SPDET"  # same-process detune marker in scratch deck filenames


def _import_detuner(repo: Path):
    sys.path.insert(0, str(repo / "scripts"))
    import detune_deck  # noqa: E402
    return detune_deck


def _user_decks(deck_dir: Path) -> list[Path]:
    """Original [USER] decks only — skip generated variants (v2 / SPDET)."""
    out = []
    for p in sorted(deck_dir.glob("*.dck")):
        if "[USER]" not in p.name:
            continue
        if _SP_MARKER in p.name or re.search(r"\sv\d+\s", p.name):
            continue
        out.append(p)
    return out


def _bracket_of(name: str, default: int = 3) -> int:
    m = _BRACKET_RE.search(name)
    return int(m.group(1)) if m else default


_LOGGED_RE = re.compile(r"logged iteration #\d+\s*\(([a-z]+)\)")


def _verdict_from_output(text: str) -> str:
    for line in text.splitlines():
        s = line.strip().lower()
        if s.startswith("verdict:"):           # the "  verdict: kept" summary line
            return s.split(":", 1)[1].strip()
    m = _LOGGED_RE.search(text.lower())          # "Logged iteration #N (kept)"
    if m:
        return m.group(1)
    return "unknown"


def _row_count(db_path: Path) -> int:
    import sqlite3
    try:
        con = sqlite3.connect(str(db_path))
        n = con.execute("SELECT COUNT(*) FROM iterations").fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-dir", default=str(DEFAULT_REPO))
    ap.add_argument("--decks-subdir",
                    default=r"vendor\forge\userdata\decks\commander")
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--sim-games", type=int, default=4)
    ap.add_argument("--per-run-timeout", type=int, default=600)
    ap.add_argument("--depths", default="3,5,7,9",
                    help="comma list of detune depths to rotate through")
    ap.add_argument("--decks", type=int, default=0,
                    help="cap number of base decks (0 = all); for quick validation")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--keep-scratch", action="store_true",
                    help="don't delete the detuned + curated scratch decks afterward")
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--sim-fillers", default=None,
                    help="comma-separated filler deck filenames (relative to "
                         "deck_dir); pass weak fillers so the A-vs-B USER decks "
                         "decide games instead of strong fillers winning all")
    ap.add_argument("--sim-margin", type=int, default=1)
    args = ap.parse_args()

    repo = Path(args.repo_dir)
    deck_dir = repo / args.decks_subdir
    db_path = repo / "knowledge_log.sqlite"
    detune_deck = _import_detuner(repo)

    curate_exe = shutil.which("commander-auto-curate")
    if not curate_exe:
        cand = Path(sys.executable).parent / "commander-auto-curate.exe"
        curate_exe = str(cand) if cand.exists() else None
    if not curate_exe:
        print("ERROR: commander-auto-curate not found on PATH", file=sys.stderr)
        return 2

    depths = [int(d) for d in args.depths.split(",") if d.strip()]
    rng = random.Random(args.seed)
    base_decks = _user_decks(deck_dir)
    if args.decks:
        base_decks = base_decks[:args.decks]
    if not base_decks:
        print(f"ERROR: no [USER] decks in {deck_dir}", file=sys.stderr)
        return 2

    print(f"same-process generator")
    print(f"  repo        : {repo}")
    print(f"  base decks  : {len(base_decks)}")
    print(f"  depths      : {depths}")
    print(f"  sim-games   : {args.sim_games}")
    print(f"  minutes     : {args.minutes}")
    print(f"  start rows  : {_row_count(db_path)}")

    scratch: list[Path] = []       # detuned scratch decks (depth>0) to delete
    cleanup_stems: list[str] = []  # stems whose curated v2 sibling to delete
    tally = {"kept": 0, "reverted": 0, "neutral": 0, "pending": 0,
             "unknown": 0, "error": 0}
    end_time = time.time() + args.minutes * 60
    job = 0

    try:
        # Round-robin (deck, depth) so we span depths evenly within the budget.
        plan = [(d, depth) for depth in depths for d in base_decks]
        rng.shuffle(plan)
        for base, depth in plan:
            if time.time() >= end_time:
                print("time budget exhausted -- stopping")
                break
            job += 1
            bracket = _bracket_of(base.name)
            seed = rng.randint(1, 10_000_000)
            # depth 0 = curate the ORIGINAL deck (no detune). Curation on an
            # already-strong deck usually can't improve it -> neutral/reverted,
            # which gives the negative class. depth>0 detunes first so the
            # curator has room to recover -> kept-leaning. Same process for both.
            if depth == 0:
                curate_path = base
                n_removed = 0
            else:
                try:
                    detuned_text, removed, n_removed = detune_deck.detune(
                        base.read_text(encoding="utf-8"), n=depth, seed=seed)
                except ValueError as exc:
                    print(f"[{job}] skip {base.name} depth={depth}: {exc}")
                    tally["error"] += 1
                    continue
                stem = _BRACKET_RE.sub("", base.stem).strip()
                scratch_name = f"{stem} {_SP_MARKER}{depth}-{seed % 1000} [B{bracket}].dck"
                curate_path = deck_dir / scratch_name
                curate_path.write_text(detuned_text, encoding="utf-8")
                scratch.append(curate_path)
            cleanup_stems.append(_BRACKET_RE.sub("", curate_path.stem).strip())

            cmd = [
                curate_exe, str(curate_path),
                "--bracket", str(bracket),
                "--source", "heuristic",
                "--mode", "polish",
                "--force",
                "--run-sim",
                "--sim-games", str(args.sim_games),
                "--sim-margin", str(args.sim_margin),
                "--db-path", str(db_path),
            ]
            if args.sim_fillers:
                cmd += ["--sim-fillers", args.sim_fillers]
            t0 = time.time()
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=args.per_run_timeout,
                )
                out = (proc.stdout or "") + "\n" + (proc.stderr or "")
                verdict = _verdict_from_output(out) if proc.returncode == 0 else "error"
            except subprocess.TimeoutExpired:
                verdict = "error"
                out = f"(timed out after {args.per_run_timeout}s)"
            dt = time.time() - t0

            key = verdict if verdict in tally else (
                "unknown" if verdict != "error" else "error")
            # Map the summary-derived verdicts onto the tally buckets.
            if verdict in ("kept", "reverted", "neutral", "pending"):
                key = verdict
            tally[key] = tally.get(key, 0) + 1
            print(f"[{job}] {base.name[:34]:34}  depth={depth:>2}  "
                  f"removed={n_removed:>2}  verdict={verdict:<9}  {dt:5.0f}s")
            time.sleep(args.sleep)
    finally:
        if not args.keep_scratch:
            removed_files = 0
            # Delete curated v2 siblings for every curated deck (depth 0 and >0).
            # NB: only matches "<stem> v<N>" so the ORIGINAL [USER] decks (no
            # v-suffix) are never touched.
            for stem in set(cleanup_stems):
                for victim in deck_dir.glob(f"{stem} v*.dck"):
                    try:
                        victim.unlink(); removed_files += 1
                    except OSError:
                        pass
            # Delete the detuned scratch decks we created (depth>0 only).
            for p in scratch:
                try:
                    if p.exists():
                        p.unlink(); removed_files += 1
                except OSError:
                    pass
            print(f"cleaned {removed_files} scratch/v2 file(s)")

    print("\n=== summary ===")
    for k in ("kept", "reverted", "neutral", "pending", "unknown", "error"):
        print(f"  {k:9}: {tally.get(k, 0)}")
    print(f"  end rows : {_row_count(db_path)}")
    decisive = tally["kept"] + tally["reverted"]
    if decisive:
        print(f"  kept rate (kept/decisive): {tally['kept']/decisive:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
