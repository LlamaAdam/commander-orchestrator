"""Step 8 -- drive harness through the existing router, one failure at a time.

Pipeline:
  1. (Optional) Clone/update the target repo via harness.git_ops.clone_or_update.
  2. Run pytest (fast or slow lane) via harness.runner.run_pytest.
  3. For each TestFailure in the JUnit XML, build a FailureBundle.
  4. Call Router.handle(bundle.prompt) -- reuses your existing triage rules,
     quota gating, Ollama/Claude dispatch, and events.jsonl logging.
  5. Write data/triage/<safe-nodeid>.md per failure (handler, response, etc.).
  6. Return a TriageRunResult summary.

This module is intentionally additive: no changes to router.py, quota.py,
triage.py, or claude_cli.py. The harness output flows through the existing
orchestrator unchanged.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from .harness import (
    TestFailure,
    TestRunResult,
    bundle_failure,
    clone_or_update,
    run_pytest,
)
from .harness.failure import FailureBundle
from .router import Router, TaskResult


@dataclass
class TriagedFailure:
    failure: TestFailure
    bundle: FailureBundle
    task_result: TaskResult
    markdown_path: Optional[str] = None


@dataclass
class TriageRunResult:
    success: bool
    repo_dir: str
    lane: str
    test_run: TestRunResult
    triaged: List[TriagedFailure] = field(default_factory=list)
    total_failures: int = 0
    routed_to_local: int = 0
    routed_to_claude: int = 0
    blocked_by_quota: int = 0
    duration_seconds: float = 0.0
    error: str = ""


_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_nodeid(nodeid: str) -> str:
    """pytest nodeids contain ::, /, \\ -- normalize to a filesystem-safe slug."""
    slug = _SLUG_RE.sub("_", nodeid).strip("_")
    return slug[:200] if slug else "unknown"


def _format_markdown(tf: TriagedFailure) -> str:
    f = tf.failure
    r = tf.task_result
    lines: List[str] = [
        f"# Triage: `{f.nodeid}`",
        "",
        f"- **handler**: `{r.handler}`",
        f"- **success**: {r.success}",
        f"- **duration**: {r.duration_seconds:.2f}s",
    ]
    if r.blocked:
        lines.append(f"- **blocked by quota**: yes (unblock in {r.seconds_until_unblock:.0f}s)")
    if r.error_type:
        lines.append(f"- **error_type**: `{r.error_type}`")
    if r.error:
        lines.append(f"- **error**: {r.error[:200]}")
    if r.triage_decision:
        via = r.triage_decision.get("via", "?")
        reason = r.triage_decision.get("rule") or r.triage_decision.get("reason") or "?"
        lines.append(f"- **routed via**: `{via}` ({reason})")
    lines += [
        "",
        "## Failure context",
        f"- file: `{f.file}` (line {f.line if f.line is not None else '?'})",
        f"- type: `{f.failure_type}`",
        "",
        "### Pytest message",
        "```",
        (f.message or "(no message)").rstrip(),
        "```",
        "",
        "### Traceback",
        "```",
        (f.traceback or "(no traceback)").rstrip(),
        "```",
        "",
        "## Model response",
        "",
        (r.text.strip() if r.text else "_(empty response)_"),
    ]
    return "\n".join(lines)


def triage_failures(
    repo_dir: Union[Path, str],
    *,
    repo_url: str = "https://github.com/LlamaAdam/commander-builder.git",
    branch: Optional[str] = "feature/2026-04-28-session",
    lane: str = "fast",
    output_dir: Optional[Path] = None,
    update_clone: bool = False,
    skip_clone: bool = False,
    max_failures: Optional[int] = None,
    router: Optional[Router] = None,
) -> TriageRunResult:
    """Drive the harness -> bundle -> router pipeline end-to-end.

    Args:
        repo_dir:      where the target repo lives on disk (relative or absolute).
        repo_url:      git URL; used only if a clone is needed.
        branch:        branch to check out / update.
        lane:          'fast' (default) or 'slow'.
        output_dir:    where to write per-failure markdown files
                       (default: '<cwd>/data/triage').
        update_clone:  if True AND skip_clone is False, do a fetch+pull on an
                       existing clone. Default False: leave existing clone alone.
        skip_clone:    skip the git step entirely (use when you have a junction
                       or pre-existing checkout you don't want touched).
        max_failures:  cap how many failures we triage (e.g., 1 for a demo run).
        router:        injected Router; default Router() with no kwargs.
    """
    t0 = time.monotonic()
    repo_dir = Path(repo_dir)

    # 1. Clone or update (unless skipped).
    if not skip_clone:
        # Only clone if the dir doesn't exist or has no .git.
        already_clone = repo_dir.exists() and (repo_dir / ".git").exists()
        if not already_clone or update_clone:
            git_result = clone_or_update(
                repo_url, repo_dir, branch=branch, fetch_if_exists=update_clone
            )
            if not git_result.success:
                empty_run = TestRunResult(
                    success=False, lane=lane, exit_code=-1, duration_seconds=0.0,
                    n_passed=0, n_failed=0, n_errors=0, n_skipped=0, n_total=0,
                )
                return TriageRunResult(
                    success=False,
                    repo_dir=str(repo_dir),
                    lane=lane,
                    test_run=empty_run,
                    duration_seconds=round(time.monotonic() - t0, 3),
                    error=f"git step failed: {git_result.error}",
                )

    # 2. Run pytest.
    test_run = run_pytest(repo_dir, lane=lane)

    if test_run.n_total == 0 and test_run.error:
        return TriageRunResult(
            success=False,
            repo_dir=str(repo_dir),
            lane=lane,
            test_run=test_run,
            duration_seconds=round(time.monotonic() - t0, 3),
            error=f"pytest harness error: {test_run.error}",
        )

    # 3. Setup outputs + router.
    if router is None:
        router = Router()
    if output_dir is None:
        output_dir = Path("data/triage")
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 4. Triage each failure.
    failures = list(test_run.failures)
    if max_failures is not None:
        failures = failures[:max_failures]

    triaged: List[TriagedFailure] = []
    routed_local = 0
    routed_claude = 0
    blocked = 0

    for failure in failures:
        bundle = bundle_failure(failure, repo_dir)
        task_result = router.handle(bundle.prompt)

        if task_result.handler == "local":
            routed_local += 1
        elif task_result.handler == "claude":
            routed_claude += 1
            if task_result.blocked:
                blocked += 1

        tf = TriagedFailure(failure=failure, bundle=bundle, task_result=task_result)
        slug = _safe_nodeid(failure.nodeid)
        md_path = output_dir / f"{slug}.md"
        try:
            md_path.write_text(_format_markdown(tf), encoding="utf-8")
            tf.markdown_path = str(md_path)
        except OSError as exc:
            tf.markdown_path = None
            # Note the failure but keep going.
            print(f"[triage] WARN: failed to write {md_path}: {exc}")
        triaged.append(tf)

    return TriageRunResult(
        success=True,
        repo_dir=str(repo_dir),
        lane=lane,
        test_run=test_run,
        triaged=triaged,
        total_failures=len(failures),
        routed_to_local=routed_local,
        routed_to_claude=routed_claude,
        blocked_by_quota=blocked,
        duration_seconds=round(time.monotonic() - t0, 3),
    )
