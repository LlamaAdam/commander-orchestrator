"""End-to-end smoke test for the Router.

WARNING: this script makes one REAL Claude CLI call (subscription auth) and
one REAL Ollama generation. Run after `smoke_triage.py` passes.

Usage:
    python scripts/smoke_router_e2e.py                       # full e2e (default model)
    python scripts/smoke_router_e2e.py --no-claude           # skip the Claude call
    python scripts/smoke_router_e2e.py --model opus          # use Opus (requires Max)
    python scripts/smoke_router_e2e.py --model claude-sonnet-4-6  # full model ID

Writes events to data/events.jsonl and quota state to data/quota_state.json.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Critical: scrub ANTHROPIC_API_KEY before importing anything that calls Claude.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

from orchestrator import local_model  # noqa: E402
from orchestrator.router import Router  # noqa: E402


def _parse_args(argv: list[str]) -> tuple[bool, str | None]:
    """Returns (skip_claude, claude_model_or_None)."""
    skip_claude = "--no-claude" in argv
    model: str | None = None
    if "--model" in argv:
        i = argv.index("--model")
        if i + 1 < len(argv):
            model = argv[i + 1]
        else:
            print("[XX]  --model requires a value (e.g. --model opus)")
            sys.exit(2)
    return skip_claude, model


def main() -> int:
    skip_claude, claude_model = _parse_args(sys.argv[1:])

    print("=" * 70)
    print("router end-to-end smoke test")
    print(f"  claude_model: {claude_model or '(CLI default)'}")
    print(f"  skip_claude:  {skip_claude}")
    print("=" * 70)

    if not local_model.ping():
        print("[XX]  Ollama not reachable. Start Ollama and re-run.")
        return 1

    # Use a temp dir so this test doesn't pollute persistent state.
    with tempfile.TemporaryDirectory() as tmp:
        events_path = Path(tmp) / "events.jsonl"
        quota_path = Path(tmp) / "quota.json"

        router = Router(
            events_path=events_path,
            quota_path=quota_path,
            claude_model=claude_model,
        )

        # ----- Local-routed task ----------------------------------------
        print("\n-- local-routed task --")
        local_task = "Rename old_func to new_func in script.py"
        result = router.handle(local_task)
        print(f"  handler:  {result.handler}")
        print(f"  success:  {result.success}")
        print(f"  via:      {result.triage_decision.get('via')}")
        print(f"  rule:     {result.triage_decision.get('rule_name')}")
        print(f"  duration: {result.duration_seconds:.2f}s")
        print(f"  text[:100]: {(result.text or '')[:100]!r}")
        if result.handler != "local":
            print(f"[XX]  expected local handler, got {result.handler}")
            return 1
        if not result.success:
            print(f"[XX]  local call failed: {result.error}")
            return 1
        print("[OK]  local-routed task completed")

        # ----- Claude-routed task ---------------------------------------
        if not skip_claude:
            print("\n-- claude-routed task (will burn one Claude call) --")
            claude_task = "Design the architecture for a token-bucket rate limiter."
            result = router.handle(claude_task)
            print(f"  handler:  {result.handler}")
            print(f"  success:  {result.success}")
            print(f"  via:      {result.triage_decision.get('via')}")
            print(f"  rule:     {result.triage_decision.get('rule_name')}")
            print(f"  duration: {result.duration_seconds:.2f}s")
            print(f"  blocked:  {result.blocked}")
            print(f"  text[:200]: {(result.text or '')[:200]!r}")
            if result.handler != "claude":
                print(f"[XX]  expected claude handler, got {result.handler}")
                return 1
            if not result.success and not result.blocked:
                print(f"[XX]  claude call failed: {result.error_type}: {result.error}")
                return 1
            print("[OK]  claude-routed task completed (or correctly blocked)")
        else:
            print("\n-- skipping claude-routed task (--no-claude) --")

        # ----- Inspect events.jsonl ------------------------------------
        print("\n-- events.jsonl --")
        with events_path.open() as f:
            events = [json.loads(line) for line in f if line.strip()]
        print(f"  total events: {len(events)}")
        for ev in events:
            preview = {k: v for k, v in ev.items() if k != "decision"}
            print(f"    {ev.get('event','?'):<16} {preview}")

        # ----- Inspect quota state -------------------------------------
        if quota_path.exists():
            with quota_path.open() as f:
                qs = json.load(f)
            print("\n-- quota state --")
            print(f"  total_calls:       {qs.get('total_calls')}")
            print(f"  total_successes:   {qs.get('total_successes')}")
            print(f"  total_input_toks:  {qs.get('total_input_tokens')}")
            print(f"  total_output_toks: {qs.get('total_output_tokens')}")
            print(f"  blocked_until:     {qs.get('blocked_until')}")

    print("\n[OK]  router e2e smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
