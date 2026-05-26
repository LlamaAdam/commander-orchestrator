"""Smoke test for orchestrator.harness.runner.

Two modes:

  Default (fast, ~1s): parse a synthetic JUnit XML to exercise the parser
  and dataclass shapes. No real pytest invocation, no network.

  --real <repo_dir>: actually invoke `python -m pytest` (fast lane) in the
  given repo. Expects an already-cloned + pip-installed commander-builder.

Examples:

  python scripts/smoke_runner.py
  python scripts/smoke_runner.py --real C:/path/to/commander-builder
  python scripts/smoke_runner.py --real ./data/repos/commander-builder --lane fast
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from orchestrator.harness.runner import (  # noqa: E402
    parse_junit_xml,
    run_pytest,
)


SYNTHETIC_JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="2" skipped="3" tests="8" time="1.234">
    <testcase classname="tests.test_alpha" name="test_pass_one" file="tests/test_alpha.py" line="10" time="0.01"/>
    <testcase classname="tests.test_alpha" name="test_pass_two" file="tests/test_alpha.py" line="20" time="0.01"/>
    <testcase classname="tests.test_alpha" name="test_pass_three" file="tests/test_alpha.py" line="30" time="0.01"/>
    <testcase classname="tests.test_beta" name="test_fail_one" file="tests/test_beta.py" line="40" time="0.05">
      <failure message="AssertionError: 1 != 2">
Traceback (most recent call last):
  File "tests/test_beta.py", line 41, in test_fail_one
    assert 1 == 2
AssertionError: 1 != 2
      </failure>
    </testcase>
    <testcase classname="tests.test_beta" name="test_fail_two" file="tests/test_beta.py" line="50" time="0.05">
      <failure message="ValueError: bad input">
Traceback (most recent call last):
  File "tests/test_beta.py", line 51, in test_fail_two
    raise ValueError("bad input")
ValueError: bad input
      </failure>
    </testcase>
    <testcase classname="tests.test_gamma" name="test_error_one" file="tests/test_gamma.py" line="60" time="0.05">
      <error message="ImportError: missing module 'foo'">
Traceback (most recent call last):
  File "tests/test_gamma.py", line 61, in test_error_one
    import foo
ImportError: missing module 'foo'
      </error>
    </testcase>
    <testcase classname="tests.test_delta" name="test_skip_one" file="tests/test_delta.py" line="70">
      <skipped message="needs --run-slow"/>
    </testcase>
    <testcase classname="tests.test_delta" name="test_skip_two" file="tests/test_delta.py" line="80">
      <skipped message="needs --run-slow"/>
    </testcase>
    <testcase classname="tests.test_delta" name="test_skip_three" file="tests/test_delta.py" line="90">
      <skipped message="needs --run-slow"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def _smoke_parser():
    print("-- synthetic JUnit XML parse --")
    with tempfile.TemporaryDirectory(prefix="orch_smoke_run_") as td:
        xml = Path(td) / "synth.xml"
        xml.write_text(SYNTHETIC_JUNIT_XML, encoding="utf-8")

        counts, failures = parse_junit_xml(xml)
        print(f"  counts:   {counts}")
        print(f"  failures: {len(failures)} (expected 3 = 2 failures + 1 error)")

        if counts != {"total": 8, "failed": 2, "errors": 1, "skipped": 3, "passed": 2}:
            print(f"[FAIL]  count mismatch: {counts!r}")
            return 1

        if len(failures) != 3:
            print(f"[FAIL]  expected 3 entries, got {len(failures)}")
            return 1

        f_types = sorted(f.failure_type for f in failures)
        if f_types != ["error", "failure", "failure"]:
            print(f"[FAIL]  failure_type mismatch: {f_types}")
            return 1

        f0 = next(f for f in failures if f.name == "test_fail_one")
        if f0.file != "tests/test_beta.py":
            print(f"[FAIL]  file attr wrong: {f0.file!r}")
            return 1
        if f0.line != 40:
            print(f"[FAIL]  line attr wrong: {f0.line!r}")
            return 1
        if "AssertionError" not in f0.message:
            print(f"[FAIL]  message missing AssertionError: {f0.message!r}")
            return 1
        if "assert 1 == 2" not in f0.traceback:
            print(f"[FAIL]  traceback missing assert line: {f0.traceback!r}")
            return 1

        e0 = next(f for f in failures if f.failure_type == "error")
        if "ImportError" not in e0.message:
            print(f"[FAIL]  error message missing ImportError: {e0.message!r}")
            return 1

    print("[OK]  synthetic JUnit XML parsed correctly.")
    return 0


def _print_result(label, r):
    print(f"-- {label} --")
    print(f"  success:       {r.success}")
    print(f"  lane:          {r.lane}")
    print(f"  exit_code:     {r.exit_code}")
    print(f"  duration:      {r.duration_seconds}s")
    print(f"  passed/failed/errors/skipped/total: "
          f"{r.n_passed}/{r.n_failed}/{r.n_errors}/{r.n_skipped}/{r.n_total}")
    if r.error:
        print(f"  harness error: {r.error}")
    if r.failures:
        print(f"  first failure: {r.failures[0].nodeid}")
        print(f"    message:     {r.failures[0].message[:120]}")
    if r.junit_xml_path:
        print(f"  junit_xml:     {r.junit_xml_path}")
    if (not r.success) or r.n_total == 0:
        if r.stderr_tail.strip():
            print("  --- stderr tail (last 20 lines) ---")
            for line in r.stderr_tail.splitlines()[-20:]:
                print(f"  | {line}")
        if r.stdout_tail.strip():
            print("  --- stdout tail (last 20 lines) ---")
            for line in r.stdout_tail.splitlines()[-20:]:
                print(f"  | {line}")


def _smoke_real(repo_dir, lane):
    print(f"-- real pytest invocation (lane={lane}) --")
    print(f"  repo_dir: {repo_dir}")
    if not repo_dir.exists():
        print(f"[FAIL]  repo_dir does not exist: {repo_dir}")
        return 1
    if not (repo_dir / ".git").exists():
        print(f"  warning: {repo_dir} is not a git repo (may still be runnable)")

    result = run_pytest(repo_dir, lane=lane)
    _print_result(f"pytest --lane={lane}", result)

    if result.n_total == 0 and result.error is None:
        print("[FAIL]  no tests collected and no harness error -- something is off.")
        return 1
    if result.error and result.n_total == 0:
        print(f"[FAIL]  harness error and no parsed counts: {result.error}")
        return 1

    print("[OK]  real pytest invocation completed.")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", type=Path, default=None,
                        help="Path to a checked-out repo. Runs real pytest there.")
    parser.add_argument("--lane", choices=["fast", "slow"], default="fast")
    args = parser.parse_args()

    print("=" * 70)
    print("runner smoke test")
    print(f"  real:   {args.real}")
    print(f"  lane:   {args.lane}")
    print("=" * 70)

    rc = _smoke_parser()
    if rc != 0:
        return rc
    print()

    if args.real:
        rc = _smoke_real(args.real, args.lane)
        if rc != 0:
            return rc

    print()
    print("[OK]  runner smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
