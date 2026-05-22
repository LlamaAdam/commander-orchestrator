"""GitHub test harness for the orchestrator.

Modules:
    git_ops  — clone / checkout / pull a target repo (idempotent).
    runner   — pytest fast-lane and slow-lane invocations against a checked-out repo.
    failure  — bundle a single pytest failure into a model-ready prompt.

The harness modules are deliberately standalone: they return structured results
but do NOT depend on the router, the quota tracker, or the event log. The
orchestrator (router.py at step 8) is the one that calls these, logs events,
and decides whether to route a failure-fix prompt to the local model or to
Claude.

Subprocess env policy:
    - `git_ops.py`        inherits env (needs whatever git credentials the user has).
    - `runner.py`         inherits env (commander-auto-curate tests want ANTHROPIC_API_KEY
                          if it's set — explicitly NOT scrubbed here).
    - The orchestrator's `claude_cli.py` still scrubs ANTHROPIC_API_KEY when
      invoking the `claude` CLI for orchestrator logic. The two policies coexist.
"""

from .git_ops import GitResult, clone_or_update, short_sha
from .runner import (
    TestFailure,
    TestRunResult,
    run_pytest,
    run_commander_doctor,
)
from .failure import FailureBundle, bundle_failure

__all__ = [
    "GitResult",
    "clone_or_update",
    "short_sha",
    "TestFailure",
    "TestRunResult",
    "run_pytest",
    "run_commander_doctor",
    "FailureBundle",
    "bundle_failure",
]
