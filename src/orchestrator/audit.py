"""Preflight audit — verify every subsystem before a run, and surface the
current bug + backlog state.

`run_audit()` probes each dependency the fix loop needs (local model, Claude
CLI, billing-safety, quota, target repo, harness) and rolls up the standing
backlog (needs-human escalations + tier-3-capped failures). With `deep=True`
it also runs the target suite once to report the live bug count.

Used by:
  - `orch audit [--deep]` (manual preflight before a run), and
  - `run_continuous` at start-up (auto-preflight; aborts on a hard FAIL).

Pure-ish + testable: every external probe goes through a module function that
tests monkeypatch (local_model.ping, claude_cli.find_claude_binary, ...).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import claude_cli, local_model, quota

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str  # OK | WARN | FAIL
    detail: str = ""


@dataclass
class AuditReport:
    checks: list = field(default_factory=list)
    backlog: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True unless a hard blocker (FAIL) is present. WARNs are tolerated."""
        return all(c.status != FAIL for c in self.checks)

    @property
    def n_warn(self) -> int:
        return sum(1 for c in self.checks if c.status == WARN)

    @property
    def n_fail(self) -> int:
        return sum(1 for c in self.checks if c.status == FAIL)


def _count_needs_human(path: Path) -> int:
    """Count escalations in needs_human.md (one '## ' section each)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return 0
    return sum(1 for ln in text.splitlines() if ln.startswith("## "))


def _count_capped(seen_path: Path) -> int:
    import json
    from .auto_fix import MAX_FAILED_ATTEMPTS, MAX_REGRESSIONS
    try:
        seen = json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, FileNotFoundError, json.JSONDecodeError):
        return 0
    if not isinstance(seen, dict):
        return 0
    return sum(
        1 for v in seen.values()
        if isinstance(v, dict) and (
            int(v.get("attempt_count", 0) or 0) >= MAX_FAILED_ATTEMPTS
            or int(v.get("regressions", 0) or 0) >= MAX_REGRESSIONS
        )
    )


def run_audit(
    project_root,
    repo_dir,
    *,
    ollama_url: str = local_model.DEFAULT_OLLAMA_URL,
    deep: bool = False,
) -> AuditReport:
    """Probe subsystems + roll up backlog. `deep` also runs the target suite."""
    project_root = Path(project_root)
    repo_dir = Path(repo_dir)
    checks: list = []

    # --- runtime ---
    checks.append(Check("python", OK, sys.version.split()[0]))

    # --- local model (Ollama) -- tier 1. WARN (not FAIL): Claude still works. ---
    try:
        if local_model.ping(ollama_url):
            models = local_model.list_models(ollama_url)
            has_coder = any("qwen" in m.lower() or "coder" in m.lower() for m in models)
            checks.append(Check(
                "ollama", OK if has_coder else WARN,
                f"reachable; models={models}" if has_coder
                else f"reachable but no coder model found: {models}"))
        else:
            checks.append(Check("ollama", WARN,
                                "unreachable -- tier-1 local fixes disabled (Claude fallback still works)"))
    except Exception as e:  # noqa: BLE001
        checks.append(Check("ollama", WARN, f"probe error: {type(e).__name__}: {e}"))

    # --- Claude CLI -- tier 2 + triage fallback. FAIL if absent. ---
    try:
        path = claude_cli.find_claude_binary()
        checks.append(Check("claude_cli", OK, path))
    except FileNotFoundError:
        checks.append(Check("claude_cli", FAIL,
                            "not on PATH -- tier-2 + triage fallback unavailable"))

    # --- billing safety: ANTHROPIC_API_KEY must not be a live key ---
    if os.environ.get("ANTHROPIC_API_KEY"):
        checks.append(Check("billing_safety", WARN,
                            "ANTHROPIC_API_KEY is set non-empty -- it's scrubbed at call time, "
                            "but unset it to be safe (subscription billing)"))
    else:
        checks.append(Check("billing_safety", OK, "no live ANTHROPIC_API_KEY (subscription auth)"))

    # --- quota gate ---
    try:
        q = quota.QuotaTracker(path=project_root / "data" / "quota_state.json")
        blocked, secs = q.is_blocked()
        checks.append(Check("quota", WARN if blocked else OK,
                            f"BLOCKED ~{int(secs)}s remaining" if blocked else "clear"))
    except Exception as e:  # noqa: BLE001
        checks.append(Check("quota", WARN, f"unreadable: {e}"))

    # --- data dir writable ---
    data_dir = project_root / "data"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".audit_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("data_writable", OK, str(data_dir)))
    except OSError as e:
        checks.append(Check("data_writable", FAIL, f"cannot write {data_dir}: {e}"))

    # --- target repo -- FAIL if missing (can't run at all) ---
    if not repo_dir.exists():
        checks.append(Check("target_repo", FAIL, f"missing: {repo_dir}"))
    elif not (repo_dir / ".git").exists():
        checks.append(Check("target_repo", WARN,
                            f"{repo_dir} is not a git repo -- apply_diff/replace_file need git"))
    else:
        checks.append(Check("target_repo", OK, str(repo_dir)))

    # --- pytest available (the harness shells out to it) ---
    import importlib.util
    if importlib.util.find_spec("pytest") is not None:
        checks.append(Check("pytest", OK, "importable"))
    else:
        checks.append(Check("pytest", FAIL, "pytest not installed in this venv"))

    # --- deep: run the target suite once for a live bug count ---
    if deep and repo_dir.exists():
        try:
            from .harness import run_pytest
            res = run_pytest(repo_dir, lane="fast")
            bugs = res.n_failed + res.n_errors
            checks.append(Check(
                "target_suite", OK if res.n_total else WARN,
                f"{res.n_passed} passed, {res.n_failed} failed, {res.n_errors} errors "
                f"(n_total={res.n_total})"))
            # bugs here are whatever's failing now (may include the user's WIP)
            # -- reported, not auto-fixed by the audit.
        except Exception as e:  # noqa: BLE001
            checks.append(Check("target_suite", WARN, f"could not run suite: {type(e).__name__}: {e}"))
            bugs = None
    else:
        bugs = None

    backlog = {
        "needs_human": _count_needs_human(project_root / "data" / "needs_human.md"),
        "capped_failures": _count_capped(project_root / "data" / "auto_fix_seen.json"),
    }
    if bugs is not None:
        backlog["failing_tests"] = bugs

    return AuditReport(checks=checks, backlog=backlog)


_GLYPH = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}


def format_audit(r: AuditReport) -> str:
    L = ["=== preflight audit ==="]
    for c in r.checks:
        L.append(f"  [{_GLYPH.get(c.status, '????')}] {c.name:14} {c.detail}")
    b = r.backlog
    L.append("")
    L.append("--- backlog ---")
    L.append(f"  needs-human escalations : {b.get('needs_human', 0)}")
    L.append(f"  tier-3-capped failures  : {b.get('capped_failures', 0)}")
    if "failing_tests" in b:
        L.append(f"  failing tests now       : {b['failing_tests']}")
    L.append("")
    if r.ok:
        verdict = "READY" + (f" (with {r.n_warn} warning(s))" if r.n_warn else "")
    else:
        verdict = f"NOT READY -- {r.n_fail} blocker(s); fix before running"
    L.append(f"verdict: {verdict}")
    return "\n".join(L)
