# commander-orchestrator

[![tests](https://github.com/LlamaAdam/commander-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/LlamaAdam/commander-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

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

## Easy run (launcher)

For a no-flags-to-remember entry point, double-click **`Orchestrator.cmd`**
(or run `.venv\Scripts\orch-launcher.exe`). It opens an interactive menu to
pick the **target repo to work on** (persisted to
`data/launcher_config.json`) and run audit / work list / self-test / status /
a single fix pass / the continuous loop.

`pip install -e .` generates the real `orch-launcher.exe` in `.venv\Scripts\`;
it re-uses this machine's venv Python (lightweight — no bundling). It's also
scriptable:

```bash
.venv/Scripts/orch-launcher.exe set-repo C:\dev\commander-builder
.venv/Scripts/orch-launcher.exe show
.venv/Scripts/orch-launcher.exe audit
.venv/Scripts/orch-launcher.exe run --hours 1
```

**Project Manager** (menu option 8 / `orch-launcher pm`) surveys the target
repo and prints one prioritized plan: what the fix loop can auto-fix *now*
(failing tests), what needs a human/Claude task (open backlog + FP roadmap),
and deferred tests. It can also register an unattended run via Windows Task
Scheduler:

```bash
.venv/Scripts/orch-launcher.exe pm --scan-tests          # plan + live failing-test count
.venv/Scripts/orch-launcher.exe pm schedule --schedule DAILY --time 02:00
.venv/Scripts/orch-launcher.exe pm list                  # show scheduled Orchestrator-* tasks
.venv/Scripts/orch-launcher.exe pm unschedule NightlyFix
```

> **Planned next step:** a fully self-contained PyInstaller `.exe` (Python +
> deps embedded) so it runs on a machine with no venv. The launcher module is
> already structured for it (`ORCH_PROJECT_ROOT` override for relocated layouts).

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

## License

[MIT](LICENSE) © 2026 LlamaAdam.

See `HANDOFF.md` for current status, architecture, and the backlog.
