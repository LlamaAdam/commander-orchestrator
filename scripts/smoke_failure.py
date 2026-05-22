"""Smoke test for orchestrator.harness.failure.

Builds a synthetic mini-repo in a tempdir with two source files (a test and
the module it imports), constructs a TestFailure pointing at it, runs
`bundle_failure`, and asserts that:

  - test_source contains the test file's text
  - related_sources picked up the referenced source file
  - the assembled prompt contains the failure's message, traceback, and
    both source bodies

No real pytest run needed. Sub-second iteration.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from orchestrator.harness.runner import TestFailure  # noqa: E402
from orchestrator.harness.failure import bundle_failure  # noqa: E402


def _make_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)

    (root / "src" / "calc.py").write_text(
        textwrap.dedent("""
        def add(a, b):
            # BUG: subtracts instead of adds
            return a - b
        """).lstrip(),
        encoding="utf-8",
    )
    (root / "tests" / "test_calc.py").write_text(
        textwrap.dedent("""
        from src.calc import add


        def test_add_two_and_three():
            assert add(2, 3) == 5
        """).lstrip(),
        encoding="utf-8",
    )


def main() -> int:
    print("=" * 70)
    print("failure smoke test (synthetic)")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="orch_smoke_fail_") as td:
        repo = Path(td)
        _make_repo(repo)

        traceback = textwrap.dedent("""
        Traceback (most recent call last):
          File "tests/test_calc.py", line 4, in test_add_two_and_three
            assert add(2, 3) == 5
          File "src/calc.py", line 3, in add
            return a - b
        AssertionError: assert -1 == 5
        """).strip()

        failure = TestFailure(
            nodeid="tests/test_calc.py::test_add_two_and_three",
            classname="tests.test_calc",
            name="test_add_two_and_three",
            file="tests/test_calc.py",
            line=4,
            failure_type="failure",
            message="AssertionError: assert -1 == 5",
            traceback=traceback,
        )

        bundle = bundle_failure(failure, repo)

        print("-- bundle shape --")
        print(f"  test_source chars:    {len(bundle.test_source)}")
        print(f"  related_sources keys: {list(bundle.related_sources.keys())}")
        print(f"  prompt chars:         {len(bundle.prompt)}")
        print()

        # Assertions on bundle contents.
        if not bundle.test_source:
            print("[FAIL]  test_source is empty")
            return 1
        if "def test_add_two_and_three" not in bundle.test_source:
            print("[FAIL]  test_source missing test definition")
            return 1

        # Should have picked up src/calc.py as a related source via the traceback.
        # Both 'src/calc.py' (posix) and 'src\\calc.py' (windows) are acceptable keys.
        related_keys = list(bundle.related_sources.keys())
        if not any(k.endswith("calc.py") for k in related_keys):
            print(f"[FAIL]  related_sources should include calc.py, got {related_keys}")
            return 1

        # The related body should contain the buggy function.
        calc_body = next(v for k, v in bundle.related_sources.items() if k.endswith("calc.py"))
        if "return a - b" not in calc_body:
            print("[FAIL]  related calc.py body missing the buggy line")
            return 1

        # Prompt assertions.
        prompt = bundle.prompt
        required_substrings = [
            "tests/test_calc.py::test_add_two_and_three",
            "AssertionError: assert -1 == 5",
            "return a - b",
            "Traceback (most recent call last):",
            "## Task",
            "unified diff",
        ]
        missing = [s for s in required_substrings if s not in prompt]
        if missing:
            print(f"[FAIL]  prompt missing substrings: {missing}")
            print("  first 600 chars of prompt:")
            print("    " + prompt[:600].replace("\n", "\n    "))
            return 1

        print("-- prompt preview (first 25 lines) --")
        for line in prompt.splitlines()[:25]:
            print(f"  {line}")
        print(f"  ... ({len(prompt.splitlines())} total lines)")

    print()
    print("[OK]  failure smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
