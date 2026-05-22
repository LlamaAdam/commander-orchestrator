"""Reproduce the failed tier-2 Claude call in cycle 1.

Builds a representative tier-2 retry prompt and invokes claude_cli.run_claude
directly. Prints the full ClaudeResult so we can see stdout/stderr/rc.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestrator.claude_cli import run_claude


BUNDLE = """A pytest error occurred. Diagnose the root cause and propose a minimal fix.

## Test: `tests.test_image_cache::test_card_image_route_uses_cache_on_second_call`
- file: `` (line ?)
- type: `error`

## Pytest message
```
failed on setup with "RuntimeError: flask is required for the web scaffold. Install with: pip install commander-builder[web]"
```

## Traceback (truncated)
```
Traceback (most recent call last):
  File "C:\\dev\\commander-builder\\tests\\test_image_cache.py", line 12, in client
    from flask import Flask
ModuleNotFoundError: No module named 'flask'
```
"""

PRIOR = """
## PRIOR ATTEMPT (this failure has already been tried)
- action: `install_package`
- confidence: 1.00
- reasoning: missing flask
- package proposed: `flask`
- outcome: **regressed**
- error: no improvement (baseline=0, after=0)
Propose something DIFFERENT from the prior attempt. If you cannot improve on it, respond with `escalate`.
"""

SUFFIX = """

---

## YOUR TASK -- respond with JSON ONLY

Choose ONE action and return strict JSON matching this schema:

```json
{
  "action": "install_package" | "apply_diff" | "escalate",
  "confidence": 0.0 to 1.0,
  "reasoning": "one short sentence",
  "package": "<pip spec>",
  "diff": "<unified diff text>",
  "files_touched": ["path/to/file.py"],
  "escalate_reason": "<why human or Claude is needed>"
}
```
"""

prompt = BUNDLE + SUFFIX + PRIOR
print(f"[repro] prompt_len = {len(prompt)} chars")
print(f"[repro] calling run_claude...")
r = run_claude(prompt)
print(f"[repro] success     = {r.success}")
print(f"[repro] error_type  = {r.error_type}")
print(f"[repro] error       = {r.error[:500]!r}")
print(f"[repro] rc          = {r.return_code}")
print(f"[repro] duration_s  = {r.duration_seconds:.3f}")
print(f"[repro] stdout(300) = {r.raw_stdout[:300]!r}")
print(f"[repro] stderr(500) = {r.raw_stderr[:500]!r}")
print(f"[repro] cmd[:4]     = {r.cmd[:4]}")
print(f"[repro] cmd[-1] len = {len(r.cmd[-1]) if r.cmd else 0}")
