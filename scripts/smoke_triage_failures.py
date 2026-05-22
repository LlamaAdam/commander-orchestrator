"""Smoke test for orchestrator.triage_failures.

Default (synthetic, ~1s): builds a fake Router that just echoes back, generates
a TestRunResult with two synthetic failures (via a stubbed run_pytest), runs
the pipeline with skip_clone=True, asserts both markdown files were written.

--real <repo_dir>: against a real cloned + pip-installed commander-builder,
calls the actual Router. Uses --max-failures to keep cost predictable.

Usage:
    python scripts/smoke_triage_failures.py
    python scripts/smoke_triage_failures.py --real .\data\repos\commander-builder --max-failures 1
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _synthetic_smoke() -> int:
    """Run a synthetic end-to-end with stubbed router + harness."""
    print("-- synthetic triage pipeline --")

    # Stub Router BEFORE importing triage_failures so the imports resolve.
    import types
    from dataclasses import dataclass as _dc, field as _f

    @_dc
    class StubResult:
        success: bool = True
        handler: str = "local"
        text: str = "_(stub model response: install flask)_"
        error: str = ""
        error_type: str = ""
        triage_decision: dict = _f(default_factory=lambda: {"via": "rule", "rule": "import-error"})
        duration_seconds: float = 0.42
        blocked: bool = False
        seconds_until_unblock: float = 0.0

    class StubRouter:
        def __init__(self, **kw):
            self.calls = 0

        def handle(self, task):
            self.calls += 1
            return StubResult(text=f"stub response #{self.calls}")

    # Patch into sys.modules.
    router_mod = types.ModuleType("orchestrator.router")
    router_mod.Router = StubRouter
    router_mod.TaskResult = StubResult
    sys.modules["orchestrator.router"] = router_mod

    # Stub harness.run_pytest to return synthetic failures (so we don't need
    # a real repo or pytest install for this smoke).
    from orchestrator.harness.runner import TestFailure, TestRunResult
    fake_failures = [
        TestFailure(
            nodeid="tests/test_a.py::test_one",
            classname="tests.test_a",
            name="test_one",
            file="tests/test_a.py",
            line=10,
            failure_type="error",
            message="ModuleNotFoundError: flask",
            traceback="Traceback (most recent call last):\n  File \"tests/test_a.py\", line 10\n  ModuleNotFoundError: flask",
        ),
        TestFailure(
            nodeid="tests/test_b.py::test_two",
            classname="tests.test_b",
            name="test_two",
            file="tests/test_b.py",
            line=20,
            failure_type="failure",
            message="AssertionError: 1 != 2",
            traceback="Traceback (most recent call last):\n  File \"tests/test_b.py\", line 20\n  AssertionError: 1 != 2",
        ),
    ]
    fake_run = TestRunResult(
        success=False, lane="fast", exit_code=1, duration_seconds=0.5,
        n_passed=8, n_failed=1, n_errors=1, n_skipped=0, n_total=10,
        failures=fake_failures,
    )

    # triage_failures did `from .harness import run_pytest`, so the *name* lives
    # in orchestrator.triage_failures module namespace -- patch THERE, not on
    # the harness submodule.
    from orchestrator import triage_failures as tf_mod
    original_run_pytest = tf_mod.run_pytest
    tf_mod.run_pytest = lambda repo_dir, **kw: fake_run

    from orchestrator.harness.git_ops import GitResult
    original_clone = tf_mod.clone_or_update
    tf_mod.clone_or_update = lambda *a, **kw: GitResult(
        success=True, operation="pull", repo_dir=Path("."), branch=None,
        head_sha="deadbeef", stdout="", stderr="", error=None,
    )

    try:
        from orchestrator.triage_failures import triage_failures, _safe_nodeid

        with tempfile.TemporaryDirectory(prefix="orch_smoke_triage_") as td:
            tdroot = Path(td)
            # Pretend the repo lives at tdroot/repo (we don't need files since
            # bundle_failure handles missing test sources gracefully).
            fake_repo = tdroot / "repo"
            fake_repo.mkdir()
            (fake_repo / ".git").mkdir()
            output_dir = tdroot / "out_triage"

            result = triage_failures(
                repo_dir=fake_repo,
                lane="fast",
                skip_clone=True,
                output_dir=output_dir,
                router=StubRouter(),
            )

            if not result.success:
                print(f"[FAIL]  result.success=False: {result.error}")
                return 1
            if result.total_failures != 2:
                print(f"[FAIL]  total_failures expected 2, got {result.total_failures}")
                return 1
            if result.routed_to_local != 2:
                print(f"[FAIL]  routed_to_local expected 2, got {result.routed_to_local}")
                return 1
            if result.routed_to_claude != 0:
                print(f"[FAIL]  routed_to_claude expected 0, got {result.routed_to_claude}")
                return 1
            # Verify markdown files exist on disk.
            for tf in result.triaged:
                if not tf.markdown_path:
                    print(f"[FAIL]  no markdown_path for {tf.failure.nodeid}")
                    return 1
                md = Path(tf.markdown_path)
                if not md.exists():
                    print(f"[FAIL]  markdown not on disk: {md}")
                    return 1
                content = md.read_text(encoding="utf-8")
                required = [
                    "# Triage:",
                    "**handler**: `local`",
                    "**routed via**: `rule`",
                    "## Model response",
                ]
                for s in required:
                    if s not in content:
                        print(f"[FAIL]  {md.name} missing substring {s!r}")
                        return 1

            print(f"  total_failures:    {result.total_failures}")
            print(f"  routed_to_local:   {result.routed_to_local}")
            print(f"  routed_to_claude:  {result.routed_to_claude}")
            print(f"  duration:          {result.duration_seconds:.2f}s")
            print(f"  reports:")
            for tf in result.triaged:
                print(f"    {Path(tf.markdown_path).name}")

    finally:
        tf_mod.run_pytest = original_run_pytest
        tf_mod.clone_or_update = original_clone

    print("[OK]  synthetic smoke passed.")
    return 0


def _real_smoke(repo_dir: Path, lane: str, max_failures: int, claude_model) -> int:
    print(f"-- real triage pipeline --")
    print(f"  repo_dir:      {repo_dir}")
    print(f"  lane:          {lane}")
    print(f"  max_failures:  {max_failures}")

    # Use the actual Router + actual run_pytest.
    from orchestrator.router import Router
    from orchestrator.triage_failures import triage_failures

    router_kwargs = {}
    if claude_model:
        router_kwargs["claude_model"] = claude_model
    router = Router(**router_kwargs)

    result = triage_failures(
        repo_dir=repo_dir,
        lane=lane,
        skip_clone=True,
        max_failures=max_failures,
        router=router,
    )

    if not result.success:
        print(f"[FAIL]  {result.error}")
        return 1

    tr = result.test_run
    print(f"  test_run: {tr.n_passed}/{tr.n_failed}/{tr.n_errors}/{tr.n_skipped} "
          f"({tr.duration_seconds:.1f}s)")
    print(f"  triaged:  {result.total_failures} "
          f"(local={result.routed_to_local}, claude={result.routed_to_claude}, "
          f"blocked={result.blocked_by_quota})")
    print(f"  duration: {result.duration_seconds:.1f}s")
    for tf in result.triaged:
        r = tf.task_result
        tail = " (BLOCKED)" if r.blocked else ""
        print(f"    {tf.failure.nodeid[:60]:60s} -> {r.handler}{tail}")
        print(f"      md: {tf.markdown_path}")
    print("[OK]  real smoke passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", type=Path, default=None,
                        help="Run against a real cloned commander-builder")
    parser.add_argument("--lane", choices=["fast", "slow"], default="fast")
    parser.add_argument("--max-failures", type=int, default=1,
                        help="Cap failures (only used with --real; default 1)")
    parser.add_argument("--claude-model", default=None)
    args = parser.parse_args()

    print("=" * 70)
    print("triage_failures smoke test")
    print(f"  real:  {args.real}")
    print("=" * 70)

    rc = _synthetic_smoke()
    if rc != 0:
        return rc
    print()

    if args.real:
        rc = _real_smoke(args.real, args.lane, args.max_failures, args.claude_model)
        if rc != 0:
            return rc

    print()
    print("[OK]  triage_failures smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
