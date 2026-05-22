"""Pytest runner for the test harness.

Two lanes that mirror the commander-builder HANDOFF doc:
    fast: `pytest`              ~30-40s, ~1110 passing, ~123 skipped
    slow: `pytest --run-slow`   ~3min,   ~1233 passing

Parsing strategy: pytest is invoked with `--junitxml=<path>`. JUnit XML is the
machine-readable surface; stdout/stderr tails are captured for human-readable
context only.

Env policy: this runner INHERITS env. The commander-auto-curate-style tests
in commander-builder expect `ANTHROPIC_API_KEY` to be available if set. The
orchestrator's `claude_cli.py` scrubs that var when invoking the `claude`
CLI — these are two separate concerns and live in two separate modules.

Also exposes `run_commander_doctor` as a pre-flight check (the HANDOFF doc
calls it out as exit-0-or-RED before running the suite).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# A "path/to/file.py:LINE" frame inside a pytest traceback.
_TB_LOC_RE = re.compile(r"([A-Za-z0-9_./\\-]+\.py):(\d+)")


def _location_from_traceback(traceback: str, hint_file: Optional[str]) -> Optional[tuple]:
    """Recover (file, line) from a traceback when JUnit omits the attributes.

    pytest's JUnit XML sometimes leaves `file`/`line` empty (notably with
    dotted classnames), which would blind the failure-bundler. The traceback
    still carries frames like ``tests/test_x.py:42: in test_x``. Prefer the
    frame whose basename matches ``hint_file`` (the test module derived from
    the classname); otherwise use the deepest frame (where it actually blew up).
    """
    matches = _TB_LOC_RE.findall(traceback or "")
    if not matches:
        return None
    if hint_file:
        hint = os.path.basename(hint_file.replace("\\", "/"))
        for path, ln in matches:
            if os.path.basename(path.replace("\\", "/")) == hint:
                return path, int(ln)
    path, ln = matches[-1]
    return path, int(ln)


@dataclass
class TestFailure:
    """A single pytest failure or error from JUnit XML."""

    nodeid: str  # e.g., "tests/test_foo.py::test_bar"
    classname: str
    name: str
    file: str  # source file (may be relative to repo root)
    line: Optional[int]
    failure_type: str  # "failure" | "error"
    message: str  # short pytest message (truncated)
    traceback: str  # full traceback text (truncated)


@dataclass
class TestRunResult:
    """Structured result from one pytest invocation."""

    success: bool  # True iff pytest exited 0
    lane: str  # "fast" | "slow"
    exit_code: int
    duration_seconds: float
    n_passed: int
    n_failed: int
    n_errors: int
    n_skipped: int
    n_total: int
    failures: list[TestFailure] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    junit_xml_path: Optional[Path] = None
    error: Optional[str] = None  # non-None if the harness itself failed (timeout, etc.)


# --- internals ----------------------------------------------------------------


def _tail(s: str, n: int = 4000) -> str:
    return s[-n:] if len(s) > n else s


def parse_junit_xml(xml_path: Path, message_chars: int = 1000,
                    traceback_chars: int = 8000) -> tuple[dict[str, int], list[TestFailure]]:
    """Parse a pytest JUnit XML file into counts + structured failures.

    Returns (counts_dict, failures_list). counts_dict has keys:
        total, failed, errors, skipped, passed.
    """
    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    suites = list(root.iter("testsuite"))

    total = failed = errors = skipped = 0
    failures: list[TestFailure] = []

    for suite in suites:
        total += int(suite.get("tests", 0) or 0)
        failed += int(suite.get("failures", 0) or 0)
        errors += int(suite.get("errors", 0) or 0)
        skipped += int(suite.get("skipped", 0) or 0)

        for case in suite.iter("testcase"):
            classname = case.get("classname", "") or ""
            name = case.get("name", "") or ""
            file_attr = case.get("file", "") or ""
            line_attr = case.get("line")
            try:
                line = int(line_attr) if line_attr is not None else None
            except (TypeError, ValueError):
                line = None

            if file_attr:
                nodeid = f"{file_attr}::{name}"
            else:
                nodeid = f"{classname}::{name}" if classname else name

            for child in case:
                tag = (child.tag or "").lower()
                if tag in ("failure", "error"):
                    tb = (child.text or "")[:traceback_chars]
                    # Recover file/line when JUnit omits them, so the failure
                    # bundler can locate + extract the failing test function.
                    eff_file, eff_line = file_attr, line
                    if not eff_file and classname:
                        eff_file = classname.replace(".", "/") + ".py"
                    if not file_attr or eff_line is None:
                        loc = _location_from_traceback(tb, eff_file)
                        if loc:
                            if not file_attr:
                                eff_file = loc[0]
                            if eff_line is None:
                                eff_line = loc[1]
                    failures.append(
                        TestFailure(
                            nodeid=nodeid,
                            classname=classname,
                            name=name,
                            file=eff_file,
                            line=eff_line,
                            failure_type=tag,
                            message=(child.get("message") or "")[:message_chars],
                            traceback=tb,
                        )
                    )

    passed = max(0, total - failed - errors - skipped)
    return (
        {"total": total, "failed": failed, "errors": errors,
         "skipped": skipped, "passed": passed},
        failures,
    )


# --- public API ---------------------------------------------------------------


def run_pytest(
    repo_dir: Path,
    *,
    lane: str = "fast",
    junit_dir: Optional[Path] = None,
    timeout_seconds: int = 600,
    extra_args: Optional[list[str]] = None,
    python_exe: Optional[str] = None,
) -> TestRunResult:
    """Invoke pytest inside `repo_dir`. Returns a TestRunResult.

    lane='fast'  → plain `pytest`
    lane='slow'  → `pytest --run-slow`

    `python_exe` lets you pin a specific interpreter (e.g. the orchestrator's
    venv python). When None, uses `sys.executable -m pytest` to ensure we
    invoke the pytest that lives in the currently active environment.

    pytest exit codes:
        0  all tests passed
        1  one or more tests failed
        2  test execution was interrupted
        3  internal error
        4  pytest CLI usage error
        5  no tests collected
    """
    repo_dir = Path(repo_dir)
    if not repo_dir.exists():
        raise FileNotFoundError(f"repo_dir does not exist: {repo_dir}")

    if lane not in ("fast", "slow"):
        raise ValueError(f"unknown lane: {lane!r} (expected 'fast' or 'slow')")

    # Resolve to absolute paths — we change pytest's cwd to repo_dir below,
    # and any relative --junitxml path would be interpreted relative to THAT,
    # not relative to the harness's cwd. Absolute paths sidestep the mismatch.
    junit_dir = (Path(junit_dir) if junit_dir is not None else (repo_dir / ".pytest_harness")).resolve()
    junit_dir.mkdir(parents=True, exist_ok=True)
    junit_xml = (junit_dir / f"results_{lane}.xml").resolve()
    if junit_xml.exists():
        try:
            junit_xml.unlink()
        except OSError:
            pass  # pytest will overwrite

    py = python_exe or sys.executable
    args = [py, "-m", "pytest",
            f"--junitxml={junit_xml}",
            "--tb=short", "-q", "--color=no"]
    if lane == "slow":
        args.append("--run-slow")
    if extra_args:
        args.extend(extra_args)

    env = os.environ.copy()  # INHERIT env (incl. ANTHROPIC_API_KEY if set)

    t0 = time.monotonic()
    stdout = ""
    stderr = ""
    error: Optional[str] = None
    exit_code = -1
    try:
        proc = subprocess.run(
            args,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout_seconds,
        )
        exit_code = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\n[harness] pytest exceeded {timeout_seconds}s timeout."
        error = f"pytest timed out after {timeout_seconds}s"
    except FileNotFoundError as exc:
        stderr = str(exc)
        error = f"could not invoke pytest: {exc}"
    duration = time.monotonic() - t0

    counts = {"total": 0, "failed": 0, "errors": 0, "skipped": 0, "passed": 0}
    failures: list[TestFailure] = []
    if junit_xml.exists():
        try:
            counts, failures = parse_junit_xml(junit_xml)
        except ET.ParseError as exc:
            stderr += f"\n[harness] failed to parse JUnit XML at {junit_xml}: {exc}"
            if not error:
                error = "junit xml parse error"

    success = (exit_code == 0) and (error is None)

    return TestRunResult(
        success=success,
        lane=lane,
        exit_code=exit_code,
        duration_seconds=round(duration, 3),
        n_passed=counts["passed"],
        n_failed=counts["failed"],
        n_errors=counts["errors"],
        n_skipped=counts["skipped"],
        n_total=counts["total"],
        failures=failures,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
        junit_xml_path=junit_xml if junit_xml.exists() else None,
        error=error,
    )


def run_commander_doctor(repo_dir: Path, *, timeout_seconds: int = 60) -> dict:
    """Run `commander-doctor` in repo_dir. Returns {success, exit_code, stdout, stderr}.

    The HANDOFF doc describes commander-doctor as exiting non-zero on RED issues.
    Use this as a pre-flight before running the test suite.

    Requires that `pip install -e .[claude]` has been run inside the active venv
    so that `commander-doctor` is on PATH.
    """
    repo_dir = Path(repo_dir)
    env = os.environ.copy()
    try:
        proc = subprocess.run(
            ["commander-doctor"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout_seconds,
        )
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": _tail(proc.stdout or ""),
            "stderr": _tail(proc.stderr or ""),
        }
    except FileNotFoundError:
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": "commander-doctor not on PATH — run `pip install -e .[claude]` in the repo first.",
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"commander-doctor exceeded {timeout_seconds}s timeout",
        }


def pip_install_editable(
    repo_dir: Path,
    *,
    extras: Optional[list[str]] = None,
    python_exe: Optional[str] = None,
    timeout_seconds: int = 600,
) -> dict:
    """Run `pip install -e .[extras]` inside repo_dir.

    Use to prepare a freshly-cloned commander-builder for testing. Idempotent
    (pip will just re-resolve and no-op if everything is already installed).
    Inherits env.
    """
    repo_dir = Path(repo_dir)
    py = python_exe or sys.executable
    spec = "."
    if extras:
        spec = f".[{','.join(extras)}]"
    args = [py, "-m", "pip", "install", "-e", spec]
    try:
        proc = subprocess.run(
            args,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": _tail(proc.stdout or ""),
            "stderr": _tail(proc.stderr or ""),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"pip install -e exceeded {timeout_seconds}s timeout",
        }
