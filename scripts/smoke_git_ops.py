"""Smoke test for orchestrator.harness.git_ops.

By default uses a small public repo (the orchestrator's own commander-builder
target) and clones to a tempdir, then runs a second idempotent pass.

Run from the project root with the venv active:

    python scripts/smoke_git_ops.py

To skip network and only test argument plumbing / dataclass shape:

    python scripts/smoke_git_ops.py --offline

To target a different repo:

    python scripts/smoke_git_ops.py --repo https://github.com/octocat/Hello-World.git --branch master
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

# Make the src/ tree importable when run from project root.
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from orchestrator.harness.git_ops import GitResult, clone_or_update, short_sha  # noqa: E402


DEFAULT_REPO = "https://github.com/LlamaAdam/commander-builder.git"
DEFAULT_BRANCH = "feature/2026-04-28-session"


def _print_result(label: str, result: GitResult) -> None:
    print(f"-- {label} --")
    print(f"  success:   {result.success}")
    print(f"  operation: {result.operation}")
    print(f"  branch:    {result.branch}")
    print(f"  head_sha:  {short_sha(result.head_sha)}")
    if result.error:
        print(f"  error:     {result.error}")
    # Trim very long output so the smoke log stays readable.
    if result.stderr:
        tail = result.stderr.strip().splitlines()[-3:]
        for line in tail:
            print(f"    stderr> {line}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--offline", action="store_true",
                        help="Skip the real clone; only smoke the dataclass shape.")
    parser.add_argument("--keep", action="store_true",
                        help="Don't delete the tempdir on success (for manual inspection).")
    args = parser.parse_args()

    print("=" * 70)
    print("git_ops smoke test")
    print(f"  repo:    {args.repo}")
    print(f"  branch:  {args.branch}")
    print(f"  offline: {args.offline}")
    print("=" * 70)

    if args.offline:
        # Construct a synthetic GitResult to verify imports + dataclass shape.
        synth = GitResult(
            success=True,
            operation="clone",
            repo_dir=Path("/tmp/fake"),
            branch=args.branch,
            head_sha="deadbeefcafebabe",
            stdout="",
            stderr="",
            error=None,
        )
        _print_result("synthetic GitResult", synth)
        print("[OK]  offline smoke (dataclass shape) passed.")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="orch_smoke_git_"))
    target = tmp / "commander-builder"
    print(f"  tmpdir:  {tmp}")
    print()

    try:
        # Pass 1: fresh clone.
        r1 = clone_or_update(args.repo, target, branch=args.branch)
        _print_result("pass 1 (fresh clone)", r1)
        if not r1.success:
            print("[FAIL]  fresh clone did not succeed.")
            return 1
        if r1.operation != "clone":
            print(f"[FAIL]  expected operation 'clone', got {r1.operation!r}.")
            return 1
        if not r1.head_sha or len(r1.head_sha) < 7:
            print(f"[FAIL]  head_sha looks invalid: {r1.head_sha!r}.")
            return 1
        print()

        # Pass 2: idempotent re-run on the same dir.
        r2 = clone_or_update(args.repo, target, branch=args.branch)
        _print_result("pass 2 (idempotent re-run)", r2)
        if not r2.success:
            print("[FAIL]  idempotent re-run did not succeed.")
            return 1
        if r2.operation not in ("pull", "fetch"):
            print(f"[FAIL]  expected operation 'pull' or 'fetch' on re-run, got {r2.operation!r}.")
            return 1
        if r2.head_sha != r1.head_sha:
            # Acceptable if the remote moved between the two calls — log but don't fail.
            print(f"  note: HEAD moved between pass 1 ({short_sha(r1.head_sha)}) "
                  f"and pass 2 ({short_sha(r2.head_sha)}). Remote may have updated. OK.")

        print()
        print("[OK]  git_ops smoke test passed.")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"  cleaned up {tmp}")
        else:
            print(f"  --keep specified, leaving {tmp} in place")


if __name__ == "__main__":
    sys.exit(main())
