"""FP-002 trainer.

Two modes:
  * default  - kept-vs-reverted classifier on knowledge_log iterations (legacy).
  * --soak   - REFRAME: margin regression on the 40-game soak rows (the
               operator's high-confidence dataset). Reads the merged
               throughput JSONL, keeps status=done & games>=40, backfills
               deck_snapshot from each deck_b file so the pre-sim composition
               features exist (soak rows store none), and fits a numpy ridge
               regression predicting the continuous margin (wins_b - wins_a).
               knowledge_log itself is retired/low-game (2/4/6-game) -- the
               soak is the canonical FP-002 source now.
    Usage: python scripts/train_fp002.py --soak [--min-games 40] [--deck-dir DIR]

--- (legacy) kept-vs-reverted classifier on knowledge_log iterations ---------

Loads all iterations, builds the ml_dataset feature rows (skip neutral),
does a deck-group-aware train/eval split (so the model is evaluated on decks
it never trained on), fits a RandomForest classifier, and reports eval
metrics + feature importances.

Degrades gracefully: if the dataset lacks both classes (e.g. zero 'kept'
examples), it prints a clear "not trainable yet" message and exits 0 rather
than crashing -- so it can be run at any time to check readiness.

CAVEAT surfaced in the importances: the positive ('kept') examples currently
come from the detune-restore generator, which produces larger swaps than the
tuned-deck negatives. If `swap_size` / `cards_added` dominate the importances,
the model is exploiting that confound rather than learning genuine deck-quality
signal. Treat a high swap_size importance as a red flag, not a win.

Usage:
    python scripts/train_fp002.py [--db PATH] [--eval-fraction 0.3] [--save model.joblib]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_DIR = Path(r"C:\dev\commander-builder")
sys.path.insert(0, str(REPO_DIR / "src"))

from commander_builder import knowledge_log as kl
from commander_builder.ml_dataset import (
    FEATURE_NAMES, build_dataset, split_train_eval, dataset_summary,
)


def _load_iterations(db_path: Path, min_id: int | None = None):
    """Load iterations, optionally only those with id >= min_id.

    The min_id filter exists to exclude rows produced BEFORE the A/B
    win-attribution seat fix (commit e8777b6): those verdicts were
    measurement artifacts (deck A and B shared an internal Name=, so wins
    funnelled to one side -> kept/reverted were not real head-to-heads).
    Train on post-fix rows only for honest labels.
    """
    with kl._connect(db_path) as conn:
        if min_id is not None:
            rows = conn.execute(
                "SELECT * FROM iterations WHERE id >= ?", (min_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM iterations").fetchall()
    return [kl.Iteration.from_row(r) for r in rows]


# --- REFRAME: 40-game soak rows -> margin regression ------------------------

_SHARE = r"\\192.168.4.49\soak_inbox"


def _default_soak_globs():
    return [fr"{_SHARE}\Llama_throughput.jsonl",
            fr"{_SHARE}\box2_throughput.jsonl",
            fr"{_SHARE}\box2b_throughput.jsonl"]


def _default_deck_dirs():
    return [fr"{_SHARE}\popular_decks",
            str(REPO_DIR / "vendor" / "forge" / "userdata" / "decks" / "commander"),
            str(REPO_DIR / "vendor" / "forge2" / "userdata" / "decks" / "commander")]


def _index_decks(deck_dirs):
    """filename and stem -> full path, across the given dirs (first wins)."""
    import glob as _glob
    import os
    idx = {}
    for d in deck_dirs:
        if not d or not os.path.isdir(d):
            continue
        for p in _glob.glob(os.path.join(d, "*.dck")):
            name = os.path.basename(p)
            idx.setdefault(name, p)
            idx.setdefault(Path(name).stem, p)
    return idx


def _load_soak_iterations(globs, min_games, deck_dirs):
    """Read merged soak JSONL; keep status=done & games>=min_games; backfill
    deck_snapshot from deck_b's file so the pre-sim composition features
    (which the soak rows never recorded) can be computed. Returns (its, stats).
    """
    import glob as _glob
    import json
    import os
    from commander_builder.web._helpers import _bracket_from_filename
    deck_idx = _index_decks(deck_dirs)
    files = []
    for g in globs:
        files.extend(_glob.glob(g))
    its, misses = [], set()
    stats = {"files": len(files), "lines": 0, "done_hiconf": 0,
             "backfilled": 0, "no_deck": 0}
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    stats["lines"] += 1
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("status") != "done":
                        continue
                    g = r.get("games") or 0
                    if g < min_games:
                        continue
                    stats["done_hiconf"] += 1
                    wa = r.get("wins_a") or 0
                    wb = r.get("wins_b") or 0
                    deck_b = r.get("deck_b") or "?.dck"
                    p = deck_idx.get(os.path.basename(deck_b)) or deck_idx.get(Path(deck_b).stem)
                    snap = None
                    if p:
                        try:
                            snap = Path(p).read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            snap = None
                    if snap:
                        stats["backfilled"] += 1
                    else:
                        stats["no_deck"] += 1
                        misses.add(os.path.basename(deck_b))
                    delta = wb - wa
                    it = kl.Iteration(
                        deck_id=Path(deck_b).stem, deck_name=Path(deck_b).stem,
                        bracket=_bracket_from_filename(deck_b) or 3,
                        audit_version="soak-ab",
                        audit_manifest={"deck_a": r.get("deck_a"), "deck_b": deck_b,
                                        "host": r.get("host"), "games": g},
                        sim_report={"wins_a": wa, "wins_b": wb, "games": g},
                        verdict=("kept" if delta >= 1 else "reverted" if delta <= -1 else "neutral"),
                        win_rate_old=round(wa / g, 4) if g else None,
                        win_rate_new=round(wb / g, 4) if g else None,
                        margin=delta,
                    )
                    it.deck_snapshot = snap  # set after construct (optional field)
                    it.id = len(its) + 1  # synthetic id; extract_features rejects id=None
                    its.append(it)
        except OSError:
            continue
    stats["deck_miss_sample"] = sorted(misses)[:5]
    return its, stats


def _run_soak(args):
    import numpy as np
    from commander_builder.ml_dataset import FEATURE_NAMES, build_dataset, split_train_eval

    globs = args.soak if args.soak else _default_soak_globs()
    deck_dirs = args.deck_dir if args.deck_dir else _default_deck_dirs()
    its, st = _load_soak_iterations(globs, args.min_games, deck_dirs)
    print("=" * 64)
    print("FP-002 REFRAME: margin regression on 40-game soak rows")
    print(f"  soak files matched     : {st['files']}")
    print(f"  JSONL lines read       : {st['lines']}")
    print(f"  done & games >= {args.min_games:<5} : {st['done_hiconf']}")
    print(f"  deck-feature backfill  : {st['backfilled']} ok / {st['no_deck']} missing deck file")
    if st["no_deck"]:
        print(f"    missing deck files e.g.: {st['deck_miss_sample']}")
        print("    -> pass --deck-dir <dir> where those .dck files live to recover them.")
    print("=" * 64)

    # Only rows WITH backfilled deck features can feed the pre-sim regression.
    its = [it for it in its if getattr(it, "deck_snapshot", None)]
    if len(its) < 10:
        print(f"NOT TRAINABLE YET: only {len(its)} rows have pre-sim deck features. "
              f"Need the soak deck files reachable (--deck-dir) and >= ~10 rows.")
        return 0

    rows = build_dataset(its, skip_neutral=False)  # regression keeps margin~0 rows too
    POST_SIM = {
        "total_games", "draws", "decisive_games", "draw_rate",
        "old_wins", "new_wins", "margin", "win_rate_old", "win_rate_new",
        "win_rate_delta", "old_avg_ending_life", "new_avg_ending_life",
        "old_avg_damage_taken", "new_avg_damage_taken",
        "old_avg_turns_when_won", "new_avg_turns_when_won",
        "old_avg_turns_when_lost", "new_avg_turns_when_lost",
        "old_eliminations", "new_eliminations",
    }
    PRE_SIM = [f for f in FEATURE_NAMES if f not in POST_SIM]

    tr, ev = split_train_eval(rows, eval_fraction=args.eval_fraction)
    if not tr or not ev:
        print("WARN: deck-group split lopsided on this small data; using all rows for "
              "train AND eval (train-only indicative fit).")
        tr = ev = rows
    Xtr = np.array([[float(r.features.get(f, 0.0)) for f in PRE_SIM] for r in tr], dtype=float)
    ytr = np.array([float(r.features.get("margin", 0.0)) for r in tr], dtype=float)
    Xev = np.array([[float(r.features.get(f, 0.0)) for f in PRE_SIM] for r in ev], dtype=float)
    yev = np.array([float(r.features.get("margin", 0.0)) for r in ev], dtype=float)
    if Xtr.ndim != 2 or Xtr.shape[0] == 0:
        print("NOT TRAINABLE: no usable feature rows after backfill.")
        return 0

    # Ridge closed-form on standardized features (numpy-only; sklearn won't load).
    mu = Xtr.mean(0); sd = np.atleast_1d(Xtr.std(0)); sd[sd == 0] = 1.0
    Xs = np.hstack([(Xtr - mu) / sd, np.ones((len(Xtr), 1))])
    lam = 1.0
    reg = lam * np.eye(Xs.shape[1]); reg[-1, -1] = 0.0  # don't penalize bias
    w = np.linalg.solve(Xs.T @ Xs + reg, Xs.T @ ytr)

    Xevs = (Xev - mu) / sd; Xevs[~np.isfinite(Xevs)] = 0.0
    Xevs = np.hstack([Xevs, np.ones((len(Xevs), 1))])
    pred = Xevs @ w
    mae = float(np.abs(pred - yev).mean())
    ss_res = float(((yev - pred) ** 2).sum()); ss_tot = float(((yev - yev.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / max(ss_tot, 1e-9)
    base_mae = float(np.abs(yev - ytr.mean()).mean())  # predict-the-mean baseline

    print(f"  target = margin (wins_b - wins_a), range {ytr.min():.0f}..{ytr.max():.0f}")
    print(f"  rows: train={len(tr)} eval={len(ev)} | pre-sim features={len(PRE_SIM)}")
    print(f"  eval MAE={mae:.2f}  R2={r2:.3f}  (baseline predict-mean MAE={base_mae:.2f})")
    if r2 <= 0:
        print("  VERDICT: no predictive signal yet (R2<=0 -> not beating the mean). "
              "Likely needs more rows or better pre-sim features.")
    coefs = sorted(zip(PRE_SIM, w[:-1]), key=lambda t: -abs(t[1]))
    print("  top pre-sim weights (standardized):")
    for n, c in coefs[:8]:
        print(f"    {c:+.3f}  {n}")
    if args.save:
        np.savez(args.save.replace(".joblib", ".npz"),
                 weights=w, mu=mu, sd=sd, features=np.array(PRE_SIM), task=np.array("regress"))
        print(f"\nsaved margin-regression model -> {args.save.replace('.joblib', '.npz')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO_DIR / "knowledge_log.sqlite"))
    ap.add_argument("--eval-fraction", type=float, default=0.3)
    ap.add_argument("--min-per-class", type=int, default=3,
                    help="minimum kept AND reverted needed to attempt training")
    ap.add_argument("--save", default=None, help="optional path to dump the fitted model (joblib)")
    ap.add_argument("--min-id", type=int, default=None,
                    help="only use iterations with id >= N. Use to exclude rows "
                         "from before the A/B seat-attribution fix (e.g. --min-id 314), "
                         "whose kept/reverted labels are measurement artifacts.")
    ap.add_argument("--soak", nargs="*", default=None,
                    help="REFRAME SOURCE: train on merged 40-game soak rows (JSONL) "
                         "instead of knowledge_log. Pass one or more throughput.jsonl "
                         "paths/globs (default: the three share throughput files). "
                         "Implies margin regression unless --task is given.")
    ap.add_argument("--min-games", type=int, default=40,
                    help="for --soak: only use sims with games >= N (the standing "
                         "40-game high-confidence rule). Default 40.")
    ap.add_argument("--deck-dir", action="append", default=None,
                    help="for --soak: directory to find deck .dck files for pre-sim "
                         "feature backfill (repeatable). Soak rows store no deck_snapshot, "
                         "so features are recovered from the deck files referenced.")
    ap.add_argument("--task", choices=["classify", "regress"], default=None,
                    help="classify = kept-vs-reverted (knowledge_log default); "
                         "regress = predict continuous margin (the --soak reframe default).")
    args = ap.parse_args()

    # --- REFRAME PATH: 40-game soak rows -> margin regression ---------------
    if args.soak is not None:
        return _run_soak(args)

    db_path = Path(args.db)
    its = _load_iterations(db_path, min_id=args.min_id)
    rows = build_dataset(its, skip_neutral=True)  # kept + reverted only
    summ = dataset_summary(rows)
    print("=" * 64)
    print("FP-002 dataset")
    if args.min_id is not None:
        print(f"  min-id filter          : id >= {args.min_id} (post-seat-fix rows only)")
    print(f"  total iterations in db : {len(its)}")
    print(f"  usable rows (kept+rev) : {len(rows)}")
    print(f"  label distribution     : {summ.get('label_distribution')}")
    print(f"  unique decks           : {summ.get('unique_decks')}  "
          f"(rows/deck {summ.get('rows_per_deck_min')}-{summ.get('rows_per_deck_max')})")
    print("=" * 64)

    labels = [r.label for r in rows]
    n_kept = labels.count("kept")
    n_rev = labels.count("reverted")
    if n_kept < args.min_per_class or n_rev < args.min_per_class:
        print(f"NOT TRAINABLE YET: need >= {args.min_per_class} each of kept AND reverted "
              f"(have kept={n_kept}, reverted={n_rev}).")
        print("Run scripts/generate_positives.py to add 'kept' examples, then retry.")
        return 0

    # numpy-only (sklearn's bundled DLLs blow past Windows MAX_PATH in this
    # deep venv). A small standardized logistic regression is plenty for a
    # binary classifier on ~170 rows, and keeps the trainer dependency-light.
    import numpy as np

    # Post-sim outcome features. The VERDICT is derived from these (margin>0
    # => kept), so a model trained WITH them is circular -- it will look
    # ~perfect but learns nothing transferable. We train two models to expose
    # this: (1) all features, (2) PRE-sim features only (the honest task:
    # predict a swap's outcome before simming it).
    POST_SIM = {
        "total_games", "draws", "decisive_games", "draw_rate",
        "old_wins", "new_wins", "margin", "win_rate_old", "win_rate_new",
        "win_rate_delta", "old_avg_ending_life", "new_avg_ending_life",
        "old_avg_damage_taken", "new_avg_damage_taken",
        "old_avg_turns_when_won", "new_avg_turns_when_won",
        "old_avg_turns_when_lost", "new_avg_turns_when_lost",
        "old_eliminations", "new_eliminations",
    }
    PRE_SIM = [f for f in FEATURE_NAMES if f not in POST_SIM]

    train_rows, eval_rows = split_train_eval(rows, eval_fraction=args.eval_fraction)
    if not eval_rows:
        print("WARN: deck-group split produced an empty eval set on this small "
              "data; reporting train-only fit (indicative).")
        eval_rows = train_rows

    def _logreg_fit(X, y, steps=3000, lr=0.1, l2=1e-3):
        mu, sd = X.mean(0), X.std(0); sd[sd == 0] = 1.0
        Xs = (X - mu) / sd
        Xs = np.hstack([Xs, np.ones((len(Xs), 1))])  # bias
        w = np.zeros(Xs.shape[1])
        # class-balanced weights
        pos = y.mean(); cw = np.where(y == 1, 0.5 / max(pos, 1e-6), 0.5 / max(1 - pos, 1e-6))
        for _ in range(steps):
            p = 1 / (1 + np.exp(-Xs @ w))
            g = Xs.T @ ((p - y) * cw) / len(y) + l2 * np.r_[w[:-1], 0]
            w -= lr * g
        return w, mu, sd

    def _logreg_pred(w, mu, sd, X):
        Xs = (X - mu) / sd; Xs[~np.isfinite(Xs)] = 0
        Xs = np.hstack([Xs, np.ones((len(Xs), 1))])
        return (1 / (1 + np.exp(-Xs @ w))) >= 0.5

    def _metrics(y_true, y_pred):
        tp = int(((y_pred == 1) & (y_true == 1)).sum())
        tn = int(((y_pred == 0) & (y_true == 0)).sum())
        fp = int(((y_pred == 1) & (y_true == 0)).sum())
        fn = int(((y_pred == 0) & (y_true == 1)).sum())
        acc = (tp + tn) / max(1, len(y_true))
        prec = tp / max(1, tp + fp); rec = tp / max(1, tp + fn)
        f1 = 2 * prec * rec / max(1e-9, prec + rec)
        return acc, prec, rec, f1, (tp, tn, fp, fn)

    def _run(feature_names, title):
        Xtr = np.array([[r.features.get(f, 0.0) for f in feature_names] for r in train_rows])
        ytr = np.array([1.0 if r.label == "kept" else 0.0 for r in train_rows])
        Xev = np.array([[r.features.get(f, 0.0) for f in feature_names] for r in eval_rows])
        yev = np.array([1.0 if r.label == "kept" else 0.0 for r in eval_rows])
        w, mu, sd = _logreg_fit(Xtr, ytr)
        acc, prec, rec, f1, cm = _metrics(yev, _logreg_pred(w, mu, sd, Xev))
        print(f"\n--- {title} ---")
        print(f"  features: {len(feature_names)} | train={len(train_rows)} eval={len(eval_rows)}")
        print(f"  eval  acc={acc:.3f}  precision(kept)={prec:.3f}  recall(kept)={rec:.3f}  f1={f1:.3f}")
        print(f"  confusion (kept=positive): tp={cm[0]} tn={cm[1]} fp={cm[2]} fn={cm[3]}")
        coefs = sorted(zip(feature_names, w[:-1]), key=lambda t: -abs(t[1]))
        print("  top weights (|coef| on standardized features):")
        for name, c in coefs[:8]:
            print(f"    {c:+.2f}  {name}")
        return w, mu, sd

    print(f"\nPRE_SIM features (honest task): {PRE_SIM}")
    _run(FEATURE_NAMES, "MODEL A: all features (CIRCULAR -- verdict derived from margin)")
    w, mu, sd = _run(PRE_SIM, "MODEL B: pre-sim features only (honest predictive task)")

    if args.save:
        np.savez(args.save.replace(".joblib", ".npz"),
                 weights=w, mu=mu, sd=sd, features=np.array(PRE_SIM))
        print(f"\nsaved pre-sim model -> {args.save.replace('.joblib', '.npz')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
