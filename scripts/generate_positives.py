"""Generate 'kept' (positive-class) knowledge_log rows via detune-vs-original.

The overnight curation batch produced 198 labeled rows that are all
reverted/neutral -- zero 'kept'. An FP-002 kept-vs-reverted classifier can't
learn with no positive class. This script manufactures positives reliably:

  1. Detune a deck (remove N strong cards -> basics) -- pilot confirmed the
     full original beats its detuned version 5-0 in a 6-game pod sim.
  2. Sim detuned(old) vs original(new). The original wins decisively.
  3. Log a knowledge_log row whose proposal is the RESTORE (adds = removed
     cards, cuts = added basics) with the empirical verdict (= 'kept').

Each iteration uses a different random detune (seeded) for variety, and a
distinct deck_id (`<stem>-detune`) so positives never conflate with the
tuned-deck negatives under one deck_id (keeps the deck-group split clean).

CAVEAT (documented for the trainer): these positives have larger swap_size
than the tuned-deck negatives (restoring N cards vs swapping ~2). A model
could exploit that confound. This unblocks FP-002 with a balanced label set;
refine swap-size matching later if needed.

Usage:
    python scripts/generate_positives.py --minutes 60 --per-deck-cap 6 \
        --detune-min 4 --detune-max 8 --sim-games 4
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
REPO_DIR = Path(r"C:\dev\commander-builder")
sys.path.insert(0, str(REPO_DIR / "src"))
sys.path.insert(0, str(REPO_DIR / "scripts"))  # for detune_deck

import detune_deck
from commander_builder import knowledge_log as kl
from commander_builder.forge_runner import run_ab_simulation
from commander_builder._proposer_sim import (
    _verdict_from_ab, _pick_filler_decks, _ab_to_iteration_fields,
)

DECK_DIR = REPO_DIR / "vendor" / "forge" / "userdata" / "decks" / "commander"
DB_PATH = REPO_DIR / "knowledge_log.sqlite"
import re as _re
_BRACKET_RE = _re.compile(r"\[B([1-5])\]\.dck$")
_VERSION_RE = _re.compile(r"\sv\d+\s")


def _log(msg, log_file):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with log_file.open("a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _kept_count() -> int:
    import sqlite3
    c = sqlite3.connect(str(DB_PATH))
    try:
        return c.execute("SELECT COUNT(*) FROM iterations WHERE verdict='kept'").fetchone()[0]
    finally:
        c.close()


def _discover():
    out = []
    for p in sorted(DECK_DIR.glob("[[]USER[]]*.dck")):
        if _VERSION_RE.search(p.name) or "DETUNE" in p.name:
            continue
        m = _BRACKET_RE.search(p.name)
        out.append((p, int(m.group(1)) if m else 3))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--target-kept", type=int, default=None,
                    help="stop once knowledge_log has this many 'kept' rows")
    ap.add_argument("--per-deck-cap", type=int, default=6,
                    help="max positives to attempt per deck this run")
    ap.add_argument("--detune-min", type=int, default=4)
    ap.add_argument("--detune-max", type=int, default=8)
    ap.add_argument("--sim-games", type=int, default=4)
    ap.add_argument("--per-run-timeout", type=int, default=600)
    args = ap.parse_args()

    import random
    log_dir = PROJECT_ROOT / "data" / "curate_runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"positives_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    decks = _discover()
    end_time = time.time() + args.minutes * 60
    start_kept = _kept_count()

    _log("=" * 68, log_file)
    _log(f"generate_positives: decks={len(decks)} start_kept={start_kept} "
         f"detune={args.detune_min}-{args.detune_max} games={args.sim_games}", log_file)
    _log("=" * 68, log_file)

    n_ok = n_kept = n_other = n_fail = 0
    per_deck = {p.name: 0 for p, _ in decks}
    di = 0
    iteration = 0

    try:
        while time.time() < end_time:
            if args.target_kept is not None and _kept_count() >= args.target_kept:
                _log(f"target-kept reached -- stopping", log_file)
                break

            deck_path, bracket = decks[di % len(decks)]
            di += 1
            if per_deck[deck_path.name] >= args.per_deck_cap:
                if all(per_deck[p.name] >= args.per_deck_cap for p, _ in decks):
                    _log("all decks hit per-deck cap -- stopping", log_file)
                    break
                continue
            per_deck[deck_path.name] += 1
            iteration += 1

            seed = random.randint(1, 10_000_000)
            n = random.randint(args.detune_min, args.detune_max)
            stem = deck_path.stem  # "[USER] X [B3]"
            detuned_path = DECK_DIR / f"{stem} DETUNE.dck"

            try:
                orig_text = deck_path.read_text(encoding="utf-8")
                detuned_text, removed, n_added = detune_deck.detune(orig_text, n=n, seed=seed)
                detuned_path.write_text(detuned_text, encoding="utf-8")
            except Exception as exc:
                n_fail += 1
                _log(f"  detune FAILED {deck_path.name}: {exc}", log_file)
                continue

            _log(f"--- iter {iteration}: {deck_path.name} detune n={len(removed)} seed={seed} ---", log_file)

            t0 = time.time()
            try:
                fillers = _pick_filler_decks(
                    DECK_DIR, [deck_path, detuned_path], target_bracket=bracket)
                ab = run_ab_simulation(detuned_path, deck_path, games=args.sim_games,
                                       fillers=fillers)
            except Exception as exc:
                n_fail += 1
                _log(f"  sim FAILED: {exc}", log_file)
                detuned_path.unlink(missing_ok=True)
                continue
            dur = time.time() - t0

            if ab.status != "done":
                n_fail += 1
                _log(f"  sim status={ab.status} error={ab.error}", log_file)
                detuned_path.unlink(missing_ok=True)
                continue

            # A=detuned(old), B=original(new). Restore proposal = removed cards.
            # Use the canonical helpers so win-rate/margin are computed EXACTLY
            # as auto-curate does for the negative rows (wins/total incl. draws)
            # -- otherwise positives and negatives differ on win_rate denominator.
            verdict = _verdict_from_ab(ab, margin=1)
            ab_fields = _ab_to_iteration_fields(ab)

            manifest = {
                "added": removed,
                "removed": [f"basic land x{n_added}"],
                "rationale": f"Restore {len(removed)} strong cards removed by detuner "
                             f"(positive-example generator).",
                "source": "detune-positive",
                "requested_adds": removed, "requested_cuts": [],
                "src_deck": deck_path.name,
                "detune_seed": seed,
            }
            it = kl.Iteration(
                deck_id=f"{deck_path.stem}-detune",
                deck_name=f"{deck_path.stem} (detune-restore)",
                bracket=bracket,
                audit_version="detune-positive",
                audit_manifest=manifest,
                verdict="pending",
                # Snapshot = the DETUNED deck (the deck the restore-swap is
                # applied to), so deck-composition features describe the
                # actual swap context, not the already-healthy original.
                deck_snapshot=detuned_text,
            )
            rid = kl.record_iteration(it, db_path=DB_PATH)
            kl.update_iteration_sim(
                rid, verdict=verdict,
                sim_report=ab_fields.get("sim_report"),
                win_rate_old=ab_fields.get("win_rate_old"),
                win_rate_new=ab_fields.get("win_rate_new"),
                margin=ab_fields.get("margin"),
                notes=f"detune-vs-original positive generator (n={len(removed)})",
                db_path=DB_PATH,
            )
            detuned_path.unlink(missing_ok=True)

            n_ok += 1
            if verdict == "kept":
                n_kept += 1
            else:
                n_other += 1
            _log(f"  logged #{rid} verdict={verdict} (old {ab.wins_a}/new {ab.wins_b}, "
                 f"margin={ab_fields.get('margin')}) dur={dur:.0f}s", log_file)

    except KeyboardInterrupt:
        _log("interrupted", log_file)

    _log("=" * 68, log_file)
    _log(f"done: ok={n_ok} kept={n_kept} other={n_other} fail={n_fail}", log_file)
    _log(f"  kept rows: {start_kept} -> {_kept_count()}", log_file)
    _log("=" * 68, log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
