"""FP-002 trainer: kept-vs-reverted classifier on knowledge_log iterations.

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
    args = ap.parse_args()

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
