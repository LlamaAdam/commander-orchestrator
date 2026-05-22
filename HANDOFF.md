# HANDOFF — commander-orchestrator (the AUTOMATED FIX-LOOP app)

> **You are in PROGRAM 1 of 2.** This is the **orchestrator** — a local
> dev-automation tool that routes coding/fix tasks between a local model
> (qwen via Ollama) and the Claude CLI, and autonomously fixes failing
> pytest tests in a *target repo*.
>
> **The OTHER program is `commander-builder`** (the MTG deck app at
> `C:\dev\commander-builder`) — see its own `docs/HANDOFF.md`. This
> orchestrator's job is to *test and fix* commander-builder; they are
> separate codebases. If you're thinking about decks, Forge, FP-### plans,
> or the web app → that's commander-builder, not here.

---

## What this is

A 3-tier autonomous fix loop. Given a target repo, it runs pytest, and for
each failure decides whether a cheap local model or Claude should fix it,
applies the fix on a throwaway branch, re-runs pytest to verify, and logs
everything. Purpose: route trivial/mechanical fixes to the **free, fast**
local model (qwen2.5-coder on an RTX 3090) and reserve **Claude** (Max
subscription, rate-limited not per-token) for the hard cases.

- **Location (canonical):** `C:\dev\commander-builder`'s sibling → `C:\dev\commander-orchestrator`
  (a git repo, GitHub-ready). An older copy lives under
  `…/local-agent-mode-sessions/…/outputs/commander-orchestrator` (where this
  project was originally built) — treat `C:\dev\commander-orchestrator` as the
  working home now.
- **Venv:** `.venv` (Python 3.12). To run against commander-builder, also
  `pip install -e C:\dev\commander-builder` into this venv.
- **git-tracked** (initial commit on `main`). `data/` runtime is gitignored.
- **Target repo:** `C:\dev\commander-builder`; the fix loop reads `--repo-dir`
  (junction `data/repos/commander-builder` or pass the path directly).

## How to run

```powershell
# from this dir, using the venv python
.venv\Scripts\python -m orchestrator.cli status      # Ollama + quota + event summary
.venv\Scripts\python -m orchestrator.cli report      # roll-up: fix rate, tier split, spend
.venv\Scripts\python -m orchestrator.cli fix --dry-run   # collect failures + plan, no apply
.venv\Scripts\python -m orchestrator.cli fix         # autonomous fix loop (one pass)
.venv\Scripts\python -m orchestrator.cli pending     # show data/needs_human.md
.venv\Scripts\python -m orchestrator.cli health      # Claude self-review of routing
# long unattended run (fixes then stops when nothing's left):
.venv\Scripts\python scripts\run_continuous.py --hours 12 --burn-ceiling 15 --stop-when-idle 2
```

`orch fix` reports nothing to do when the target suite is green — it's
entirely failure-driven. To exercise it you need failing tests.

## Architecture (3 tiers)

- **Tier 1 (local):** `triage.py` routes to qwen via Ollama; emits a JSON
  action (`install_package` | `apply_diff` | `escalate`). Local `apply_diff`
  is gated to TEST files only (`LOCAL_ONLY_DIFF_PATTERNS`); source diffs escalate.
- **Tier 2 (Claude fallback):** if local's action fails/escalates,
  `Router.handle_claude_only` retries via Claude (bypasses triage). Gated by
  `quota.is_blocked()` and `--no-claude-retry`.
- **Tier 3 (caps):** `data/auto_fix_seen.json` tracks attempt_count/regressions
  per failure hash. Past `MAX_FAILED_ATTEMPTS=3` / `MAX_REGRESSIONS=2` →
  `skipped_capped` + one escalation to `data/needs_human.md`.
- **verify-then-graduate** (`--verify-mode`): Claude pre-reviews each local
  proposal; after `VERIFY_GRADUATION_THRESHOLD=10` verified successes an
  action-type graduates to local-only. State: `data/graduation_state.json`.

**Key files:** `src/orchestrator/` → `auto_fix.py` (the loop, tiers 2/3,
git ops, graduation), `router.py`, `triage.py`, `quota.py`, `claude_cli.py`,
`local_model.py`, `status.py`, `report.py`, `health.py`, `cli.py`,
`harness/` (`runner.py` pytest+JUnit, `failure.py` failure→prompt bundling,
`git_ops.py` clone/update). Drivers in `scripts/` (`run_continuous.py`,
the FP-002 data-gen scripts, `smoke_*.py` manual harnesses).
**Audit trail:** `data/events.jsonl` (triage / local_call / claude_call /
auto_fix_attempt / idle_streak).

## CRITICAL invariants (do not break)

1. **NEVER let `ANTHROPIC_API_KEY` reach the `claude` subprocess.** That flips
   billing from the Max subscription to per-token API. `claude_cli.build_subscription_env`
   scrubs it; always invoke runs with `ANTHROPIC_API_KEY=` empty. (Tested in
   `tests/test_claude_cli.py`.)
2. **Auto-fix branches stay LOCAL** — never push. Commits use an INLINE git
   identity (`-c user.email=orchestrator@local`), never mutating git config.
3. **WIP-safe git ops:** never `git add -A`, `git reset --hard`, or `clean -fd`.
   `create_working_branch` refuses only when a *target* file is dirty; commit
   stages only patched files; revert restores only patched files. (Guarded by
   `tests/test_auto_fix_gitops.py`.)
4. **Don't edit orchestrator source while a run is in flight** — fresh
   subprocesses re-import it mid-run.

## Tests

`.venv\Scripts\python -m pytest` → **128 passing**, offline (no Ollama/Claude/
network/Forge; all seams stubbed). See `tests/README.md`. Modules covered:
quota, triage, router, auto_fix (pure/tiers/gitops), report, claude_cli (incl.
the billing invariant), harness runner+bundle+clone, status, local_model, cli.

## Current state & recent work (2026-05-21/22)

- **3-tier loop + verify-then-graduate + `orch report` shipped.** 128-test suite added.
- **Dogfood (ran it against seeded bugs) found 5 pipeline bugs; #1-4 fully fixed:**
  1. JUnit omitted `file`/`line` for dotted classnames → bundler was blind →
     `runner._location_from_traceback` recovers them. ✅
  2. Implementation not bundled for assertion failures → `failure._find_definition_files`
     resolves the test's imported symbols to their defining files (follows re-exports). ✅
  3. Huge test files head-truncated the failing test → `failure._enclosing_block`
     extracts the failing function by line. ✅
  4. `apply_diff` used a cwd-relative patch path → "can't open patch" with a
     relative `--repo-dir` → now absolute. ✅
  5. **LLM unified diffs are a TAR PIT (PARTIAL).** `apply_diff` now
     `sanitize_diff`s (strips ```` ```diff ```` fences/prose), normalizes
     header-less `@@` hunks to `@@ -1 +1 @@`, and retries `[]`→`--recount`→
     `--recount --unidiff-zero`. This fixed 3 successive quirks (fences "No
     valid patches" → headerless hunks "garbage at line 4" → ...), but a 4th
     remains: Claude's diff has a placeholder/wrong start line + real context,
     so git still can't locate the hunk. **Claude DIAGNOSES correctly every
     time (conf 0.88) — only diff *application* is brittle.** See the
     recommendation in Next/open.
- **`run_continuous`:** runs every cycle by default now (HEAD-poll gating is
  opt-in via `--poll-head`); `--stop-when-idle N` exits after N idle cycles.
  VALIDATED: a `--hours 12 --stop-when-idle 2` run fixed tier-1 then stopped
  itself after 3 cycles (~23min), not 12h.
- **128 → 130 tests** (apply_diff fence + headerless-hunk repair).
- **Handoffs split** (this file + commander-builder/docs/HANDOFF.md) to end
  the two-program confusion.

## Known gotchas (Windows)

- `claude.CMD` inherits cmd.exe's ~8KB argv limit → prompts go via **stdin**, not argv.
- Deep venv path → MAX_PATH (260): sklearn's bundled DLLs fail to load
  (`WinError 206`) → the FP-002 trainer is numpy-only.
- `ANTHROPIC_API_KEY` is set to **empty string** in this env as a billing
  safeguard → test truthiness (`not os.environ.get(...)`), not membership.

## Next / open  ← START HERE

- **★ TOP RECOMMENDATION — fix bug #5 via FULL-FILE REPLACEMENT, not more diff
  quirks.** The dogfood proved tier-2 Claude reliably *diagnoses* source bugs
  but applying its unified diffs via `git apply` is a tar pit (4 distinct
  quirks and counting). Add a `replace_file` action alongside `apply_diff`:
  have Claude return the COMPLETE corrected file (or function) and write it
  directly — no git apply, hunk headers, line numbers, or context matching.
  This sidesteps the whole class and is the one thing standing between the
  orchestrator and closing the loop on real source bugs. (Change: auto_fix
  apply path + the fix-action prompt schema in `build_fix_action_prompt` +
  tests.)
- To reproduce bug #5: seed a source bug on a scratch branch, clear
  `data/auto_fix_seen.json`, run `orch fix`; tier-2 ends `apply_failed` with
  a diff preview in `data/needs_human.md`.
- The FP-002 data-gen scripts here (`generate_sameprocess.py`, `train_fp002.py`)
  belong to commander-builder's FP-002 effort — see that repo's handoff;
  conclusion there: kept-vs-reverted is not viable via the curator+Forge sim.
