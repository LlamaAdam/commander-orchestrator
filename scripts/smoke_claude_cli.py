"""End-to-end smoke test for orchestrator.claude_cli.run_claude.

Exits 0 on success, non-zero on failure. Designed to be run as:
    python scripts/smoke_claude_cli.py

Checks performed:
    1. claude binary is on PATH.
    2. ANTHROPIC_API_KEY is not in the env we build for the subprocess.
    3. A trivial prompt round-trips successfully via subscription auth.
    4. JSON envelope parsed and (if present) token usage extracted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure src/ is importable when running this script directly without `pip install -e .`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from orchestrator.claude_cli import (  # noqa: E402
    assert_no_api_key_in_env,
    build_subscription_env,
    find_claude_binary,
    run_claude,
)


def main() -> int:
    print("=" * 70)
    print("claude_cli smoke test")
    print("=" * 70)

    # 1. Locate binary
    try:
        claude_bin = find_claude_binary()
        print(f"[OK]  claude binary: {claude_bin}")
    except FileNotFoundError as e:
        print(f"[XX]  {e}")
        return 1

    # 2. Audit the subscription env
    env = build_subscription_env()
    try:
        assert_no_api_key_in_env(env)
        print("[OK]  Subscription env contains no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN.")
    except AssertionError as e:
        print(f"[XX]  {e}")
        return 1

    # 3. Also confirm critical inheritance: USERPROFILE / HOME must be preserved
    #    so the CLI finds ~/.claude/.credentials.json
    home_var = "USERPROFILE" if os.name == "nt" else "HOME"
    if home_var not in env or not env[home_var]:
        print(f"[XX]  {home_var} missing from subprocess env. CLI won't find credentials.")
        return 1
    print(f"[OK]  {home_var}={env[home_var]} (credentials path preserved)")

    # 4. Round-trip prompt
    prompt = "Reply with exactly the word: ALIVE"
    print(f"\nSending prompt: {prompt!r}")
    print("(this calls claude -p --output-format json)\n")

    result = run_claude(prompt, timeout_seconds=60)

    print(f"  duration:      {result.duration_seconds:.2f}s")
    print(f"  return_code:   {result.return_code}")
    print(f"  success:       {result.success}")
    if result.input_tokens is not None or result.output_tokens is not None:
        print(f"  input_tokens:  {result.input_tokens}")
        print(f"  output_tokens: {result.output_tokens}")
    if result.total_cost_usd is not None:
        print(f"  cost_usd:      {result.total_cost_usd}")
    if result.error:
        print(f"  error_type:    {result.error_type}")
        print(f"  error:         {result.error}")
        print(f"  raw_stderr:    {result.raw_stderr[:500]}")
    if result.text:
        print(f"  text:          {result.text!r}")

    if not result.success:
        print("\n[XX]  Smoke test FAILED.")
        return 1

    if "ALIVE" not in (result.text or "").upper():
        print("\n[!!]  Claude responded successfully but did not say 'ALIVE'.")
        print("      Treating as soft-pass — auth works, the model is just chatty.")

    print("\n[OK]  Smoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
