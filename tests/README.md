# Orchestrator test suite

Offline unit + light-integration tests for the orchestrator. **No live
Ollama, Claude CLI, network, or Forge** — every external seam is stubbed,
so the suite runs in a few seconds and is safe to run any time (including
while a Forge/curation campaign is using `commander-builder`).

## Run

```
.venv/Scripts/python -m pytest          # whole suite (testpaths=tests)
.venv/Scripts/python -m pytest tests/test_quota.py -q
```

`[tool.pytest.ini_options] testpaths = ["tests"]` in `pyproject.toml` makes
plain `pytest` discover this directory.

## What's covered (105 tests)

| File | Module under test | Focus |
|------|-------------------|-------|
| `test_quota.py` | `quota.py` | reactive rate-limit gating, retry-after, block expiry, cross-instance persistence, corrupt-file recovery, summary |
| `test_triage.py` | `triage.py` | rule pre-filter (trivial→local / complex→claude) + Llama classifier with safe-default-to-claude on every failure mode |
| `test_router.py` | `router.py` | local vs claude dispatch, quota-block short-circuit, `handle_claude_only` bypass + still-respects-block |
| `test_auto_fix_pure.py` | `auto_fix.py` | `parse_fix_action`, `_safe_package_spec` (injection rejection), danger/test-file gates, `_dirty_paths`, dedup hash, graduation threshold, verification parsing |
| `test_auto_fix_gitops.py` | `auto_fix.py` | **WIP-safety**: refuse dirty target, allow unrelated dirt, commit-only-given-files + inline identity, revert preserves untracked WIP |
| `test_auto_fix_tiers.py` | `auto_fix.py` | tier-1 local fix, tier-2 Claude retry after escalate, tier-2 skipped when quota-blocked, `already_fixed` early-exit, dry-run, tier-3 cap skip |
| `test_claude_cli.py` | `claude_cli.py` | **billing-safety invariant** (`build_subscription_env` strips API keys), error classification, retry-after parsing, JSON-envelope parsing |
| `test_harness_git.py` | `harness/git_ops.py` | `clone_or_update` fresh clone + idempotent update path, `short_sha` |
| `test_status.py` | `status.py` | quota read, event summary (24h window), snapshot composition (Ollama stubbed), human formatting |
| `test_local_model.py` | `local_model.py` | `generate`/`ping`/`list_models` with `httpx` stubbed |
| `test_report.py` | `report.py` | `orch report` roll-up: fix outcomes, tier split, telemetry, graduation, dedup caps |

## Conventions (`conftest.py`)

- `make_failure()` / `make_bundle()` — `TestFailure` / `FailureBundle` factories.
- `fake_claude_result()` / `fake_local_result()` — duck-typed model results.
- `git_repo` fixture — a real temp git repo with **inline identity** so tests
  never read or mutate the host's git config.
- Tests stub at module seams via `monkeypatch` (e.g. `triage.local_model.generate`,
  `auto_fix.run_pytest`, `local_model.httpx.Client`).

## Relationship to `scripts/smoke_*.py`

The `smoke_*.py` scripts are separate **manual integration harnesses** (some
need a live Ollama/Claude). The `tests/` suite is the automated, offline,
CI-friendly layer. The stubbed smoke scripts (`smoke_tier23`, `smoke_quota`,
`smoke_verify_graduate`, `smoke_git_ops`, `smoke_triage_failures`) overlap with
these tests and remain as end-to-end sanity checks.
