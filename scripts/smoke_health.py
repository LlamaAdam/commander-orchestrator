"""Smoke test for orchestrator.health.

Two modes:

  Default (dry-run, no Claude call):
    Builds a synthetic project with quota+events, generates the report in
    dry-run mode, verifies the prompt is structured correctly and the markdown
    file is written.

  --real:
    Issues a real Claude call (Haiku by default, very cheap -- typical cost
    well under $0.01). Writes a real Markdown report and prints it.

Usage:
    python scripts/smoke_health.py
    python scripts/smoke_health.py --real
    python scripts/smoke_health.py --real --model sonnet
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from orchestrator.health import generate_health_report, _build_prompt, _read_recent_events  # noqa: E402


def _make_synthetic_project(root: Path) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    now = time.time()
    quota_state = {
        "total_calls": 12,
        "total_successes": 11,
        "total_input_tokens": 84,
        "total_output_tokens": 9540,
    }
    (root / "data" / "quota_state.json").write_text(
        json.dumps(quota_state, indent=2), encoding="utf-8"
    )
    events = [
        {"event": "triage", "task_preview": "Rename old_func to new_func in script.py", "timestamp": now - 3600},
        {"event": "local_call", "success": True, "prompt_tokens": 38, "output_tokens": 257,
         "duration_seconds": 7.3, "timestamp": now - 3593},
        {"event": "triage", "task_preview": "Design the architecture for an event-sourcing pipeline",
         "timestamp": now - 1800},
        {"event": "claude_call", "success": True, "error_type": "", "input_tokens": 6,
         "output_tokens": 1387, "duration_seconds": 23.9, "cost_usd_reported": 0.094,
         "timestamp": now - 1776},
        {"event": "triage", "task_preview": "What is sorted() in Python?", "timestamp": now - 600},
        {"event": "local_call", "success": True, "prompt_tokens": 12, "output_tokens": 95,
         "duration_seconds": 2.1, "timestamp": now - 598},
    ]
    with (root / "data" / "events.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _smoke_dry_run(root: Path) -> int:
    print("-- dry-run (no Claude call) --")

    # First exercise the helpers directly.
    events = _read_recent_events(root / "data" / "events.jsonl", max_events=50)
    if len(events) != 6:
        print(f"[FAIL]  expected 6 events read, got {len(events)}")
        return 1
    prompt = _build_prompt(events, {"total_calls": 12})
    required_substrings = [
        "routing decisions",
        "Summary",
        "Concerns",
        "Suggested tuning",
        "Rename old_func",
        "event-sourcing",
        "sorted()",
        "## Quota snapshot",
        "## Recent events",
    ]
    missing = [s for s in required_substrings if s not in prompt]
    if missing:
        print(f"[FAIL]  prompt missing substrings: {missing}")
        return 1
    print(f"  prompt chars: {len(prompt)}")
    print(f"  events:       {len(events)}")

    # Then exercise generate_health_report in dry-run mode end-to-end.
    report = generate_health_report(project_root=root, dry_run=True)
    if not report.success:
        print(f"[FAIL]  dry-run report.success=False: {report.error}")
        return 1
    if report.n_events_reviewed != 6:
        print(f"[FAIL]  dry-run n_events_reviewed expected 6, got {report.n_events_reviewed}")
        return 1
    if "DRY RUN" not in report.markdown:
        print("[FAIL]  dry-run markdown should contain 'DRY RUN'")
        return 1
    print(f"  dry-run report markdown chars: {len(report.markdown)}")

    print("[OK]  dry-run smoke passed.")
    return 0


def _smoke_real(root: Path, model: str) -> int:
    print(f"-- real Claude call (model={model}) --")
    report = generate_health_report(project_root=root, model=model)
    if not report.success:
        print(f"[FAIL]  generate_health_report failed: {report.error}")
        return 1
    if not report.written_to:
        print("[FAIL]  report should have been written but written_to is None")
        return 1
    if not Path(report.written_to).exists():
        print(f"[FAIL]  report file not on disk: {report.written_to}")
        return 1
    print(f"  written:       {report.written_to}")
    print(f"  events:        {report.n_events_reviewed}")
    print(f"  in/out toks:   {report.input_tokens}/{report.output_tokens}")
    print(f"  cost:          ${report.cost_usd:.4f}")
    print(f"  duration:      {report.duration_seconds:.1f}s")
    print()
    print("--- report preview (first 25 lines) ---")
    for line in report.markdown.splitlines()[:25]:
        print(f"  {line}")
    print(f"  ... ({len(report.markdown.splitlines())} total lines)")
    print()
    print("[OK]  real smoke passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true",
                        help="Issue a real Claude call (costs money, default: dry-run)")
    parser.add_argument("--model", default="haiku",
                        help="Claude model when --real is set (default: haiku)")
    args = parser.parse_args()

    print("=" * 70)
    print("health smoke test")
    print(f"  real:  {args.real}")
    print(f"  model: {args.model}")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="orch_smoke_health_") as td:
        root = Path(td)
        _make_synthetic_project(root)
        rc = _smoke_dry_run(root)
        if rc != 0:
            return rc
        print()
        if args.real:
            rc = _smoke_real(root, args.model)
            if rc != 0:
                return rc

    print()
    print("[OK]  health smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
