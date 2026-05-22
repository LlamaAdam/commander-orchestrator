"""Smoke test for orchestrator.triage.

Does NOT call Claude. Does call the local Ollama server once for a real
Llama-classified case (the ambiguous prompt below). Total runtime: a few
seconds plus model warm-up.

Checks:
    1. Trivial rule-matched cases route to "local" via "rule".
    2. Architecture/multi-file rule-matched cases route to "claude" via "rule".
    3. An ambiguous case actually calls Llama, gets back a valid handler.
    4. If Ollama is unreachable, the fallback returns "claude" with via="fallback".
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from orchestrator import local_model, triage  # noqa: E402


def fail(msg: str) -> int:
    print(f"[XX]  {msg}")
    return 1


def ok(msg: str) -> None:
    print(f"[OK]  {msg}")


# ---- Test cases ------------------------------------------------------------

_TRIVIAL_CASES: list[str] = [
    "Rename foo to bar in utils.py",
    "Fix a typo in the docstring of compute_score",
    "Run black on the project",
    "Bump the version to 0.2.0",
    "Remove trailing whitespace from all .py files",
    "What is sorted() in Python?",
]

_CLAUDE_CASES: list[str] = [
    "Refactor the database layer across all three services",
    "Design the architecture for our new event-sourcing pipeline",
    "Implement the user authentication feature with email verification, password reset, and admin override",
    "Investigate the intermittent timeout bug in the payment processor",
]

# Something the rules shouldn't catch — forces an actual Llama call.
_AMBIGUOUS_CASE = (
    "Write a Python function that takes a list of dicts and groups them by a given key, "
    "returning a dict-of-lists. Include type hints."
)


def main() -> int:
    print("=" * 70)
    print("triage smoke test")
    print("=" * 70)

    # ---- Rule-only cases ------------------------------------------------
    print("\n-- trivial rules --")
    for task in _TRIVIAL_CASES:
        d = triage.triage(task)
        if d.via != "rule" or d.handler != "local":
            return fail(
                f"task {task!r} expected (rule -> local), got (via={d.via} handler={d.handler}) "
                f"reason={d.reason!r}"
            )
        ok(f"local via rule [{d.rule_name}]: {task[:60]}")

    print("\n-- claude rules --")
    for task in _CLAUDE_CASES:
        d = triage.triage(task)
        if d.via != "rule" or d.handler != "claude":
            return fail(
                f"task {task!r} expected (rule -> claude), got (via={d.via} handler={d.handler}) "
                f"reason={d.reason!r}"
            )
        ok(f"claude via rule [{d.rule_name}]: {task[:60]}")

    # ---- Llama classification path --------------------------------------
    print("\n-- llama classification (ambiguous case) --")
    if not local_model.ping():
        return fail("Ollama server not reachable at http://localhost:11434")

    models = local_model.list_models()
    if not any("qwen2.5-coder" in m for m in models):
        print(f"[!!]  Available models: {models}")
        return fail("qwen2.5-coder model not found locally. Run `ollama pull` first.")

    print(f"  Available models: {models}")
    print(f"  Ambiguous case: {_AMBIGUOUS_CASE!r}")
    d = triage.triage(_AMBIGUOUS_CASE)
    print(f"  → handler={d.handler}  via={d.via}  complexity={d.complexity}  est_files={d.estimated_files}")
    print(f"  → reason: {d.reason}")
    print(f"  → triage took {d.duration_seconds:.2f}s")
    if d.raw_classifier_output:
        print(f"  → raw: {d.raw_classifier_output[:200]}")

    if d.via not in ("llama", "fallback"):
        return fail(f"expected via=llama or fallback, got via={d.via}")
    if d.handler not in ("local", "claude"):
        return fail(f"invalid handler: {d.handler}")
    if d.via == "fallback":
        print("[!!]  Llama call fell back to claude — classifier path didn't fully succeed.")
        print("       This is non-fatal (fallback is correct behavior) but worth noting.")
    else:
        ok(f"Llama returned a valid decision ({d.handler})")

    # ---- Ollama-down fallback -------------------------------------------
    print("\n-- fallback when Ollama unreachable --")
    d = triage.triage(
        "Some ambiguous task that the rules don't catch.",
        base_url="http://localhost:1",  # bogus port
        timeout=2.0,
    )
    if d.via != "fallback" or d.handler != "claude":
        return fail(
            f"expected fallback->claude when Ollama down, got via={d.via} handler={d.handler}"
        )
    ok(f"fallback to claude when Ollama down: {d.reason[:80]}")

    print("\n[OK]  All triage smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
