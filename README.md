# commander-orchestrator

A local dev-automation tool that **autonomously fixes failing pytest tests** in
a target repo, routing each fix between a cheap local model (qwen2.5-coder via
Ollama) and the **Claude CLI** under a Max subscription. Trivial/mechanical
fixes go to the free, fast local model; hard cases escalate to Claude.

> **Full orientation, architecture, invariants, and the current backlog are in
> [`HANDOFF.md`](HANDOFF.md). Read that first.**

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .[dev]      # Windows; use bin/ on *nix
.venv/Scripts/python -m pytest                     # offline unit suite (130 tests)
```

To actually run the fix loop you also need a target repo importable in the venv
(`pip install -e <target>`), Ollama serving a local model, and the `claude` CLI
authenticated under your subscription.

```bash
.venv/Scripts/python -m orchestrator.cli status    # Ollama + quota + events
.venv/Scripts/python -m orchestrator.cli fix       # one autonomous fix pass
.venv/Scripts/python -m orchestrator.cli report    # roll-up: fix rate, tier split, spend
.venv/Scripts/python scripts/run_continuous.py --hours 12 --stop-when-idle 2
```

## ⚠️ Critical invariant

This tool runs the `claude` CLI under a **Max subscription**, not the API.
It **never lets `ANTHROPIC_API_KEY` reach the `claude` subprocess** (that would
flip billing to per-token API). `claude_cli.build_subscription_env` scrubs it;
the test suite enforces this. Don't undo it.

## Layout

- `src/orchestrator/` — the package (3-tier fix loop, router, triage, quota, harness).
- `scripts/` — drivers (`run_continuous.py`) + manual smoke harnesses.
- `tests/` — offline unit suite (no Ollama/Claude/network). See `tests/README.md`.
- `data/` — runtime state (gitignored).

MIT-style local project; not yet published. See `HANDOFF.md` for status.
