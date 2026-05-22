"""Smoke test for orchestrator.status.

Builds a synthetic project (tempdir with data/quota_state.json + data/events.jsonl),
runs get_status_snapshot() against it, asserts all fields populated, prints the
human-readable output for visual confirmation.

No Claude call. Ollama probe will likely fail in the smoke environment;
that's expected and the smoke verifies the graceful-degradation path.

Usage:
    python scripts/smoke_status.py
    python scripts/smoke_status.py --ollama-host http://127.0.0.1:11434  # try real Ollama
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

from orchestrator.status import (  # noqa: E402
    get_status_snapshot,
    format_status_human,
)


def _make_synthetic_project(root: Path, blocked: bool = False) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    now = time.time()
    quota_state = {
        "total_calls": 7,
        "total_successes": 6,
        "total_input_tokens": 42,
        "total_output_tokens": 2891,
        "history": [],
    }
    if blocked:
        quota_state["blocked_until"] = now + 1800
    (root / "data" / "quota_state.json").write_text(
        json.dumps(quota_state, indent=2), encoding="utf-8"
    )

    events = [
        {"event": "triage", "task_preview": "Rename foo to bar", "timestamp": now - 7200},
        {"event": "local_call", "success": True, "prompt_tokens": 30, "output_tokens": 200, "timestamp": now - 7195},
        {"event": "triage", "task_preview": "Design auth flow", "timestamp": now - 3600},
        {"event": "claude_call", "success": True, "input_tokens": 6, "output_tokens": 1500, "cost_usd_reported": 0.045, "timestamp": now - 3580},
        {"event": "triage", "task_preview": "Fix typo", "timestamp": now - 60},
        {"event": "local_call", "success": True, "prompt_tokens": 20, "output_tokens": 80, "timestamp": now - 55},
    ]
    with (root / "data" / "events.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocked", action="store_true",
                        help="Test the 'currently rate-limited' path")
    parser.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    print("=" * 70)
    print("status smoke test")
    print(f"  synthetic blocked: {args.blocked}")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="orch_smoke_status_") as td:
        root = Path(td)
        _make_synthetic_project(root, blocked=args.blocked)
        snap = get_status_snapshot(project_root=root, ollama_host=args.ollama_host)

        # Shape assertions.
        if snap.quota.total_calls != 7:
            print(f"[FAIL]  quota.total_calls expected 7, got {snap.quota.total_calls}")
            return 1
        if snap.quota.total_output_tokens != 2891:
            print(f"[FAIL]  quota.total_output_tokens mismatch: {snap.quota.total_output_tokens}")
            return 1
        if args.blocked and not snap.quota.blocked:
            print("[FAIL]  expected snap.quota.blocked to be True")
            return 1
        if not args.blocked and snap.quota.blocked:
            print("[FAIL]  expected snap.quota.blocked to be False")
            return 1
        if snap.events.total_events != 6:
            print(f"[FAIL]  events.total expected 6, got {snap.events.total_events}")
            return 1
        if snap.events.by_event.get("triage", 0) != 3:
            print(f"[FAIL]  events.by_event['triage'] expected 3, got {snap.events.by_event.get('triage')}")
            return 1
        if snap.events.last_event_type != "local_call":
            print(f"[FAIL]  last event should be local_call, got {snap.events.last_event_type!r}")
            return 1

        # Print human form for visual check.
        print()
        print(format_status_human(snap))

    print("[OK]  status smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
