"""Autonomous fix loop.

For each TestFailure:
  1. bundle_failure(failure, repo_dir) -- existing harness bundler.
  2. Build a fix-action prompt asking for JSON.
  3. Route via existing Router (uses your local/claude rules + quota).
  4. Parse the action JSON.
  5. Danger-list check on touched files. Match -> escalate.
  6. Apply: pip install OR full-file overwrite (replace_file) OR git apply
     (apply_diff) -- the latter two on an auto-fix/<ts> branch.
  7. Verify: re-run fast lane; if better -> keep, if regressed -> revert.
  8. Anything we can't handle goes to data/needs_human.md.

This module is additive: existing router.py, triage.py, claude_cli.py untouched.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .harness import TestFailure, bundle_failure, run_pytest
from .harness.failure import FailureBundle
from .router import Router, TaskResult


# --- defaults ---------------------------------------------------------------

DEFAULT_DANGER_PATTERNS = [
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements*.txt",
    "Pipfile*",
    "poetry.lock",
    ".env*",
    "**/.env*",
    "**/secrets/**",
    "**/credentials*",
    "**/migrations/**",
    "**/auth/**",
    "**/security/**",
    ".github/workflows/**",
    ".gitlab-ci.yml",
]

# Local-only diff scope. The local model (qwen) can apply_diff to TEST files
# directly. Diffs touching anything else escalate to tier 2 (Claude) -- they
# need more code understanding than the small local model can safely provide.
# This is policy on top of the danger-list; danger-listed paths escalate even
# for Claude.
LOCAL_ONLY_DIFF_PATTERNS = [
    "tests/**",
    "**/tests/**",
    "test_*.py",
    "**/test_*.py",
    "*_test.py",
    "**/*_test.py",
    "conftest.py",
    "**/conftest.py",
]


def _is_test_file(path: str) -> bool:
    """True if `path` matches one of the LOCAL_ONLY_DIFF_PATTERNS."""
    p = path.replace("\\", "/")
    for pat in LOCAL_ONLY_DIFF_PATTERNS:
        if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat):
            return True
    return False

CONFIDENCE_THRESHOLD = 0.7
DEDUP_WINDOW_SECONDS = 6 * 3600

# The replace_file action requires the model to emit the COMPLETE corrected
# file, so the fix bundle must show whole source files -- not the small
# truncation used for triage classification. A file truncated in the prompt
# cannot be regenerated, which forces a low-confidence escalate. These budgets
# are generous enough to show typical target files in full.
FIX_TEST_SOURCE_CHARS = 12000
FIX_RELATED_SOURCE_CHARS = 40000

# Tier 3 caps. Once a failure has been attempted MAX_FAILED_ATTEMPTS times
# without success, OR has caused MAX_REGRESSIONS test regressions, it is
# permanently dedup'd with LONG_DEDUP_SECONDS so the loop stops banging on it.
MAX_FAILED_ATTEMPTS = 3
MAX_REGRESSIONS = 2
LONG_DEDUP_SECONDS = 7 * 24 * 3600

# Verify-then-graduate. When verify_mode is on, each LOCAL-proposed action is
# reviewed by Claude before it's applied. Once an action-type has accumulated
# VERIFY_GRADUATION_THRESHOLD verified-and-successful applies, it "graduates"
# to local-only auto-apply (verification is skipped thereafter to save Claude
# calls). State persists in data/graduation_state.json.
VERIFY_GRADUATION_THRESHOLD = 10
GRADUATABLE_ACTIONS = ("install_package", "apply_diff", "replace_file")


# --- data classes -----------------------------------------------------------

@dataclass
class FixAction:
    action: str  # "install_package" | "apply_diff" | "replace_file" | "escalate" | "no_action"
    confidence: float = 0.0
    reasoning: str = ""
    package: str = ""
    diff: str = ""
    path: str = ""  # only for replace_file: the single file to overwrite
    new_content: str = ""  # only for replace_file: the COMPLETE corrected file
    files_touched: List[str] = field(default_factory=list)
    escalate_reason: str = ""
    raw_response: str = ""


@dataclass
class FixAttempt:
    status: str  # "fixed" | "already_fixed" | "regressed" | "apply_failed" | "escalated" | "skipped_dedup" | "skipped_capped" | "would_apply" | "error"
    failure_nodeid: str
    action: Optional[FixAction] = None
    handler: str = ""
    branch: str = ""
    baseline_fail_count: int = 0
    after_fail_count: int = 0
    reason: str = ""
    duration_seconds: float = 0.0
    claude_retry_used: bool = False  # tier 2: did we fall back to Claude?
    attempt_count: int = 0  # tier 3: how many times has this failure been tried?
    regressions: int = 0  # tier 3: how many times has it regressed?


# --- helpers ----------------------------------------------------------------

def _run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )


def _git(args: List[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return _run(["git"] + args, cwd=cwd, timeout=timeout)


def load_danger_list(project_root: Path) -> List[str]:
    """Read data/danger_list.txt if present; else return defaults."""
    p = project_root / "data" / "danger_list.txt"
    if not p.exists():
        return list(DEFAULT_DANGER_PATTERNS)
    out: List[str] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out or list(DEFAULT_DANGER_PATTERNS)


def is_danger_path(path: str, patterns: List[str]) -> bool:
    p = path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat):
            return True
    return False


def _dedup_hash(failure: TestFailure) -> str:
    payload = (failure.nodeid + "::" + (failure.traceback or "")[:1000]).encode("utf-8", "replace")
    return hashlib.sha256(payload).hexdigest()[:16]


def load_seen(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_seen(path: Path, seen: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen, indent=2), encoding="utf-8")


# --- prompt + parse ---------------------------------------------------------

def build_fix_action_prompt(bundle: FailureBundle, *, prior_attempt: str = "") -> str:
    base = bundle.prompt  # reuse the existing diagnose-style bundle
    suffix = (
        "\n\n---\n\n"
        "## YOUR TASK -- respond with JSON ONLY\n\n"
        "Choose ONE action and return strict JSON matching this schema:\n\n"
        "```json\n"
        "{\n"
        '  "action": "install_package" | "replace_file" | "apply_diff" | "escalate",\n'
        '  "confidence": 0.0 to 1.0,\n'
        '  "reasoning": "one short sentence",\n'
        '  "package": "<pip spec>"  // only for install_package, e.g. "flask" or "commander-builder[web]"\n'
        '  "path": "path/to/file.py",  // only for replace_file -- the single file to overwrite\n'
        '  "new_content": "<COMPLETE corrected file>",  // only for replace_file -- the ENTIRE file, not a snippet\n'
        '  "diff": "<unified diff text>",  // only for apply_diff -- minimal patch against affected file(s)\n'
        '  "files_touched": ["path/to/file.py"],  // only for apply_diff\n'
        '  "escalate_reason": "<why human or Claude is needed>"  // only for escalate\n'
        "}\n"
        "```\n\n"
        "Rules:\n"
        "- Output ONLY the JSON. No prose before or after.\n"
        "- If the failure is a missing module and the error message tells you "
        "what to install, use `install_package`.\n"
        "- To CHANGE CODE in a SMALL file, PREFER `replace_file`: pick the ONE "
        "file to fix via `path` and return its COMPLETE corrected contents in "
        "`new_content` (the whole file from first line to last, with your fix "
        "applied -- not a diff, snippet, or ellipsis). This is the most reliable "
        "way to land a fix in a small file.\n"
        "- For any file the context above marks `LARGE` (or that is otherwise "
        "big), you MUST use `apply_diff` with a MINIMAL unified diff, NOT "
        "`replace_file`. Regenerating a large file risks silently corrupting "
        "code OUTSIDE your fix (mangled quotes, dropped lines, reflowed "
        "comments). A diff only touches its hunks, so it has no blast radius. "
        "Include every touched file in `files_touched`.\n"
        "- If you are not confident (less than 0.7) OR you would need to "
        "modify config files / secrets / migrations / CI / auth code, use "
        "`escalate`.\n"
        "- Be conservative. When in doubt, escalate.\n"
    )
    if prior_attempt:
        suffix += (
            "\n## PRIOR ATTEMPT (this failure has already been tried)\n"
            f"{prior_attempt}\n"
            "\nYou are a MORE CAPABLE model operating at a HIGHER TRUST TIER "
            "than whatever made the prior attempt. You ARE allowed to edit "
            "SOURCE files, not just tests -- anything except config / secrets / "
            "migrations / CI / auth code (those still escalate).\n"
            "- If the prior attempt was escalated ONLY because the previous "
            "model is restricted to test files, that restriction does NOT apply "
            "to you: implement the real fix in the source file. Do NOT escalate "
            "merely because the fix touches a non-test source file -- prefer "
            "`replace_file` with the COMPLETE corrected file.\n"
            "- If the prior attempt's edit FAILED to apply or REGRESSED tests, "
            "propose a corrected fix (e.g. switch `apply_diff` -> "
            "`replace_file`).\n"
            "- Escalate only when a human is genuinely required (the fix needs a "
            "forbidden path, or you cannot determine a safe fix).\n"
        )
    return base + suffix


def _format_prior_attempt(action: FixAction, outcome: str, error: str = "") -> str:
    """One-paragraph summary of what was tried and how it failed, for the Claude retry prompt."""
    lines = [
        f"- action: `{action.action}`",
        f"- confidence: {action.confidence:.2f}",
        f"- reasoning: {action.reasoning[:200]}",
    ]
    if action.action == "install_package" and action.package:
        lines.append(f"- package proposed: `{action.package}`")
    if action.action == "apply_diff":
        if action.files_touched:
            lines.append(f"- files touched: {', '.join(action.files_touched)}")
        if action.diff:
            lines.append(f"- diff (first 400 chars):\n```\n{action.diff[:400]}\n```")
    if action.action == "replace_file":
        if action.path:
            lines.append(f"- file replaced: `{action.path}`")
        if action.new_content:
            lines.append(f"- new content (first 400 chars):\n```\n{action.new_content[:400]}\n```")
    lines.append(f"- outcome: **{outcome}**")
    if error:
        lines.append(f"- error: {error[:300]}")
    return "\n".join(lines)


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_LOOSE_JSON_RE = re.compile(r"\{[^{}]*?\"action\"[^{}]*\}", re.DOTALL)


def parse_fix_action(response_text: str) -> FixAction:
    """Defensive JSON extraction from the model's response."""
    raw = response_text.strip()
    text = raw
    payload = None

    # Try fenced ```json blocks first.
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            payload = None

    # Try whole-string parse.
    if payload is None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None

    # Try loose match.
    if payload is None:
        m = _LOOSE_JSON_RE.search(text)
        if m:
            try:
                payload = json.loads(m.group(0))
            except json.JSONDecodeError:
                payload = None

    if not isinstance(payload, dict):
        return FixAction(action="escalate", confidence=0.0,
                         escalate_reason="model response was not parseable as JSON",
                         raw_response=raw[:2000])

    action = str(payload.get("action", "")).strip().lower()
    if action not in ("install_package", "apply_diff", "replace_file", "escalate", "no_action"):
        return FixAction(action="escalate", confidence=0.0,
                         escalate_reason=f"unknown action: {action!r}",
                         raw_response=raw[:2000])

    path = str(payload.get("path", "") or "").strip()
    files_touched = [str(x) for x in (payload.get("files_touched") or [])]
    # replace_file targets a single `path`; mirror it into files_touched so the
    # danger/local-scope gates, branch creation, commit, and revert machinery
    # (all keyed off files_touched) work without special-casing.
    if action == "replace_file" and path and not files_touched:
        files_touched = [path]

    return FixAction(
        action=action,
        confidence=float(payload.get("confidence", 0.0) or 0.0),
        reasoning=str(payload.get("reasoning", "") or ""),
        package=str(payload.get("package", "") or "").strip(),
        diff=str(payload.get("diff", "") or ""),
        path=path,
        new_content=str(payload.get("new_content", "") or ""),
        files_touched=files_touched,
        escalate_reason=str(payload.get("escalate_reason", "") or ""),
        raw_response=raw[:2000],
    )


# --- apply ------------------------------------------------------------------

@dataclass
class ApplyResult:
    success: bool
    error: str = ""


_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9_,\-]+\])?(==[A-Za-z0-9._-]+|>=[A-Za-z0-9._-]+)?$")


def _safe_package_spec(spec: str) -> bool:
    return bool(_PKG_RE.match(spec.strip()))


def apply_install_package(spec: str, *, python_exe: Optional[str] = None,
                          timeout: int = 600) -> ApplyResult:
    spec = spec.strip()
    if not _safe_package_spec(spec):
        return ApplyResult(success=False, error=f"unsafe package spec: {spec!r}")
    py = python_exe or sys.executable
    proc = _run([py, "-m", "pip", "install", spec], timeout=timeout)
    if proc.returncode != 0:
        return ApplyResult(success=False,
                           error=f"pip install rc={proc.returncode}: {proc.stderr[-1000:]}")
    return ApplyResult(success=True)


_DIFF_FENCE_RE = re.compile(r"```(?:diff|patch|python|py)?\s*\n(.*?)```", re.DOTALL)
_PATCH_START_PREFIXES = ("diff --git ", "--- ", "Index: ", "@@ ")


# A well-formed hunk header: "@@ -a,b +c,d @@ [section]". LLMs frequently emit
# "@@ <function signature> @@" with NO line ranges, which git rejects as
# "patch with only garbage". We rewrite those to a placeholder range and let
# `git apply --recount` recompute the real counts from the hunk body + context.
_GOOD_HUNK_RE = re.compile(r"^@@ -\d")
_ANY_HUNK_RE = re.compile(r"^@@")


def sanitize_diff(text: str) -> str:
    """Coerce an LLM-emitted diff into something `git apply` will accept.

    Models often (1) wrap the diff in a ```diff fence or precede it with prose
    ("No valid patches in input"), and (2) emit hunk headers without line
    ranges ("patch with only garbage"). We strip the fence/prose, normalize
    malformed hunk headers to `@@ -1 +1 @@` (then --recount fixes the counts),
    and guarantee a trailing newline."""
    t = (text or "").strip()
    m = _DIFF_FENCE_RE.search(t)
    if m and any(tok in m.group(1) for tok in ("diff --git", "--- ", "@@")):
        t = m.group(1).strip()
    lines = t.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith(_PATCH_START_PREFIXES)), 0)
    out = []
    for ln in lines[start:]:
        if _ANY_HUNK_RE.match(ln) and not _GOOD_HUNK_RE.match(ln):
            out.append("@@ -1 +1 @@")  # placeholder; --recount fixes the counts
        else:
            out.append(ln)
    t = "\n".join(out)
    if t and not t.endswith("\n"):
        t += "\n"
    return t


# Increasingly tolerant git-apply flag sets. Plain first (exact), then
# --recount (LLMs routinely miscount @@ hunk line numbers), then also
# --unidiff-zero (zero-context hunks). 3-way is intentionally omitted -- it
# can leave conflict markers, unsafe for an autonomous apply.
_APPLY_STRATEGIES = ([], ["--recount"], ["--recount", "--unidiff-zero"])


def apply_diff(diff_text: str, repo_dir: Path, timeout: int = 60) -> ApplyResult:
    """Apply a unified diff via `git apply`, tolerant of common LLM-diff quirks.

    Sanitizes the diff (fences/prose/newline), then tries increasingly lenient
    apply strategies. On total failure, surfaces a short preview of the
    (sanitized) diff in the error so the escalation note shows what was tried."""
    diff_text = sanitize_diff(diff_text)
    if not diff_text.strip():
        return ApplyResult(success=False, error="empty diff (nothing after sanitizing)")
    # ABSOLUTE patch path: git runs with cwd=repo_dir, so a cwd-relative path
    # (e.g. repo_dir="data/repos/x") would be misresolved -> "can't open patch".
    patch_path = (Path(repo_dir) / ".auto_fix.patch").resolve()
    try:
        patch_path.write_text(diff_text, encoding="utf-8", newline="\n")
        last_err = ""
        for flags in _APPLY_STRATEGIES:
            check = _git(["apply", "--check", *flags, str(patch_path)],
                         cwd=repo_dir, timeout=timeout)
            if check.returncode != 0:
                last_err = f"git apply --check {flags or '[]'} failed: {check.stderr[-300:]}"
                continue
            apply = _git(["apply", *flags, str(patch_path)], cwd=repo_dir, timeout=timeout)
            if apply.returncode == 0:
                return ApplyResult(success=True)
            last_err = f"git apply {flags or '[]'} failed: {apply.stderr[-300:]}"
        preview = "\\n".join(diff_text.splitlines()[:6])
        return ApplyResult(success=False,
                           error=f"{last_err} | diff preview: {preview[:300]}")
    finally:
        try:
            patch_path.unlink()
        except OSError:
            pass


def _resolve_in_repo(path: str, repo_dir: Path) -> Optional[Path]:
    """Resolve `path` (repo-relative or absolute) and confirm it stays inside
    `repo_dir`. Returns the resolved path, or None on traversal/escape.

    This is the safety boundary for replace_file: a model-supplied path must
    never let us write outside the target repo."""
    if not path or not str(path).strip():
        return None
    repo_root = Path(repo_dir).resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else repo_root / candidate).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        return None
    return resolved


def apply_replace_file(path: str, new_content: str, repo_dir: Path) -> ApplyResult:
    """Overwrite a single existing file with the model's COMPLETE corrected
    contents. No git apply, hunk headers, line numbers, or context matching --
    this sidesteps the unified-diff tar pit for source fixes.

    Guards: the path must resolve INSIDE repo_dir (no traversal), the target
    file must already exist (we replace, never scatter new files), and the
    content must be non-empty. Line endings are normalized to LF and a trailing
    newline is guaranteed."""
    target = _resolve_in_repo(path, repo_dir)
    if target is None:
        return ApplyResult(success=False,
                           error=f"path escapes repo or is empty: {path!r}")
    if not target.is_file():
        return ApplyResult(success=False,
                           error=f"target file does not exist: {path!r} (replace_file overwrites only existing files)")
    if not (new_content or "").strip():
        return ApplyResult(success=False, error="empty new_content -- refusing to blank out a file")
    content = new_content.replace("\r\n", "\n").replace("\r", "\n")
    if not content.endswith("\n"):
        content += "\n"
    try:
        target.write_text(content, encoding="utf-8", newline="\n")
    except OSError as e:
        return ApplyResult(success=False, error=f"write failed: {e}")
    return ApplyResult(success=True)


# --- test-integrity guard ---------------------------------------------------

# An auto-fix must NEVER make a failing test pass by gutting it. We refuse a
# fix that, in any TEST file it touches, removes assertions or introduces a
# skip/xfail marker relative to the committed version -- the classic ways to
# mask a real bug. (Changing an asserted *value* isn't caught here; removal of
# assertions and skip/xfail injection are the high-signal, low-false-positive
# patterns, and they're the ones that silently turn red green.)
_ASSERT_RE = re.compile(r"\bassert\b|\bpytest\.raises\b|\.assert[A-Za-z]+\s*\(")
_SKIP_XFAIL_RE = re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)|pytest\.(?:skip|xfail)\s*\(|unittest\.skip")


def _count_assertions(text: str) -> int:
    return len(_ASSERT_RE.findall(text or ""))


def _count_skips(text: str) -> int:
    return len(_SKIP_XFAIL_RE.findall(text or ""))


def detect_test_weakening(before: str, after: str) -> str:
    """Return a reason if `after` weakens `before` as a test file, else ''."""
    if _count_assertions(after) < _count_assertions(before):
        return "assertions removed"
    if _count_skips(after) > _count_skips(before):
        return "skip/xfail marker added"
    return ""


def _committed_text(repo_dir: Path, rel_path: str) -> str:
    """Content of `rel_path` at git HEAD, or '' if untracked/new/missing."""
    proc = _git(["show", f"HEAD:{rel_path.replace(chr(92), '/')}"], cwd=repo_dir)
    return proc.stdout if proc.returncode == 0 else ""


def check_no_test_weakening(repo_dir: Path, touched_files: List[str]) -> str:
    """For each touched TEST file, compare its new (working-tree) content to the
    committed version; return a reason if any was weakened, else ''. Newly
    added test files have no committed baseline and are left to verification."""
    for rel in touched_files:
        if not _is_test_file(rel):
            continue
        before = _committed_text(repo_dir, rel)
        if not before:
            continue
        try:
            after = (Path(repo_dir) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        reason = detect_test_weakening(before, after)
        if reason:
            return f"{rel}: {reason}"
    return ""


# --- branch lifecycle -------------------------------------------------------

def _dirty_paths(porcelain_output: str) -> set:
    """Parse `git status --porcelain` into a set of repo-relative paths.

    Handles rename entries ("old -> new") by keeping the new path. Strips
    the two-char status prefix and any surrounding quotes; normalizes
    backslashes so comparison against forward-slash diff paths works.
    """
    paths = set()
    for line in porcelain_output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename
            path = path.split(" -> ")[-1]
        paths.add(path.replace("\\", "/").strip().strip('"'))
    return paths


def create_working_branch(repo_dir: Path, branch_name: str,
                          target_files: Optional[List[str]] = None) -> ApplyResult:
    """Create the auto-fix branch.

    A fully-clean tree is NOT required -- a dev with unrelated work-in-
    progress should still get auto-fixes. We only refuse if one of the
    files the diff will touch is itself already modified, since patching
    a dirty target risks conflicts or committing the user's in-progress
    edits to that file. Unrelated dirt is carried along by `checkout -B`
    and never staged (see commit_files) or destroyed (see revert_files).
    """
    proc = _git(["status", "--porcelain"], cwd=repo_dir)
    if proc.returncode != 0:
        return ApplyResult(success=False, error=f"git status failed: {proc.stderr}")

    if target_files:
        dirty = _dirty_paths(proc.stdout)
        targets = {str(f).replace("\\", "/").strip() for f in target_files}
        conflicting = sorted(targets & dirty)
        if conflicting:
            return ApplyResult(
                success=False,
                error=(f"target file(s) have uncommitted changes: "
                       f"{conflicting[:3]} -- refusing to overwrite WIP"),
            )

    co = _git(["checkout", "-B", branch_name], cwd=repo_dir)
    if co.returncode != 0:
        return ApplyResult(success=False, error=f"checkout -B failed: {co.stderr}")
    return ApplyResult(success=True)


def commit_files(repo_dir: Path, message: str, files: List[str]) -> ApplyResult:
    """Stage and commit ONLY the given files. Never `git add -A`, so a
    user's unrelated dirty/untracked files are never swept into the
    auto-fix commit.

    Commits with an INLINE author identity so it works even when the repo
    (and global git) has no user.name/user.email configured -- without
    mutating any git config. The auto-fix branch is local-only, so the
    synthetic identity never reaches a remote.
    """
    if not files:
        return ApplyResult(success=False, error="no files to commit")
    add = _git(["add", "--"] + list(files), cwd=repo_dir)
    if add.returncode != 0:
        return ApplyResult(success=False, error=f"git add failed: {add.stderr}")
    proc = _git([
        "-c", "user.email=orchestrator@local",
        "-c", "user.name=commander-orchestrator",
        "commit", "-m", message,
    ], cwd=repo_dir)
    if proc.returncode != 0:
        return ApplyResult(success=False, error=f"commit failed: {proc.stderr[-300:]}")
    return ApplyResult(success=True)


def revert_files(repo_dir: Path, files: List[str],
                 original_branch: str) -> ApplyResult:
    """Undo a failed/regressed apply by reverting ONLY the patched files,
    then return to the original branch. Deliberately avoids `git reset
    --hard` and `git clean -fd` so a user's unrelated WIP and untracked
    files are never destroyed."""
    if files:
        # Restore the patched files to their committed (HEAD) state.
        _git(["checkout", "HEAD", "--"] + list(files), cwd=repo_dir)
    co = _git(["checkout", original_branch], cwd=repo_dir)
    if co.returncode != 0:
        return ApplyResult(success=False, error=f"checkout {original_branch} failed: {co.stderr}")
    return ApplyResult(success=True)


def current_branch(repo_dir: Path) -> str:
    proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    return proc.stdout.strip() if proc.returncode == 0 else ""


# --- verify-then-graduate ---------------------------------------------------

@dataclass
class VerificationResult:
    approve: bool
    reason: str = ""
    raw: str = ""


def load_graduation(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_graduation(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_graduated(state: dict, action_type: str) -> bool:
    return bool(state.get(action_type, {}).get("graduated", False))


def record_verified_success(state: dict, action_type: str,
                            threshold: int = VERIFY_GRADUATION_THRESHOLD) -> bool:
    """Bump the verified-success counter for an action-type. Returns True if
    this call is the one that crosses the graduation threshold."""
    entry = state.setdefault(action_type, {"successes": 0, "graduated": False})
    if entry.get("graduated"):
        return False
    entry["successes"] = int(entry.get("successes", 0)) + 1
    if entry["successes"] >= threshold:
        entry["graduated"] = True
        return True
    return False


def build_verify_prompt(bundle: FailureBundle, action: FixAction) -> str:
    """A cheap review prompt: approve/reject the local model's proposed fix."""
    parts = [
        "You are reviewing a fix proposed by a small local model for a failing "
        "pytest test. Approve it ONLY if it is correct, minimal, and safe to "
        "apply automatically. Reject anything wrong, risky, or overly broad.",
        "",
        bundle.prompt,
        "",
        "## PROPOSED FIX (from local model)",
        f"- action: `{action.action}`",
        f"- reasoning: {action.reasoning[:300]}",
    ]
    if action.action == "install_package":
        parts.append(f"- package: `{action.package}`")
    elif action.action == "apply_diff":
        parts.append(f"- files: {', '.join(action.files_touched)}")
        parts.append(f"- diff:\n```\n{action.diff[:1500]}\n```")
    elif action.action == "replace_file":
        parts.append(f"- file: `{action.path}`")
        parts.append(f"- proposed full contents:\n```\n{action.new_content[:1500]}\n```")
    parts += [
        "",
        "Respond with JSON ONLY: {\"approve\": true|false, \"reason\": \"one short sentence\"}",
    ]
    return "\n".join(parts)


_VERIFY_JSON_RE = re.compile(r"\{[^{}]*\"approve\"[^{}]*\}", re.DOTALL)


def parse_verification(text: str) -> VerificationResult:
    raw = (text or "").strip()
    payload = None
    # Try fenced ```json block, then a loose {...approve...} match, then the
    # whole string. First successful parse wins.
    for candidate in (
        (_JSON_BLOCK_RE.search(raw), 1),
        (_VERIFY_JSON_RE.search(raw), 0),
    ):
        m, grp = candidate
        if m is not None:
            try:
                payload = json.loads(m.group(grp))
                break
            except json.JSONDecodeError:
                payload = None
    if payload is None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
    if not isinstance(payload, dict) or "approve" not in payload:
        # Unparseable verification -> conservatively reject (don't auto-apply
        # an unreviewed fix).
        return VerificationResult(approve=False,
                                  reason="verification response unparseable",
                                  raw=raw[:500])
    return VerificationResult(
        approve=bool(payload.get("approve")),
        reason=str(payload.get("reason", "") or "")[:300],
        raw=raw[:500],
    )


def verify_action_with_claude(bundle: FailureBundle, action: FixAction,
                              router: Router) -> VerificationResult:
    """Ask Claude to approve/reject the local model's proposed fix."""
    prompt = build_verify_prompt(bundle, action)
    tr = router.handle_claude_only(prompt, reason="verify local proposal")
    if not tr.success:
        # Claude call failed -> conservatively reject so we don't auto-apply
        # an unverified fix. The caller will route to tier-2 / needs_human.
        return VerificationResult(approve=False,
                                  reason=f"verify call failed: {tr.error_type or tr.error}")
    return parse_verification(tr.text or "")


# --- escalation -------------------------------------------------------------

# The human-escalation queue is backed by a structured index keyed by nodeid
# (data/needs_human.json) and a rendered, human-readable view
# (data/needs_human.md). Keying by nodeid means repeated escalations of the
# same failing test update ONE entry instead of appending a new section every
# attempt, and lets the loop RESOLVE an entry once that test passes again --
# so `orch pending` shows only genuinely-open items, not stale history.

def needs_human_index_path(project_root: Path) -> Path:
    return Path(project_root) / "data" / "needs_human.json"


def needs_human_md_path(project_root: Path) -> Path:
    return Path(project_root) / "data" / "needs_human.md"


def load_needs_human_index(project_root: Path) -> dict:
    p = needs_human_index_path(project_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _action_summary(action: Optional[FixAction]) -> Optional[dict]:
    if action is None:
        return None
    return {
        "action": action.action,
        "confidence": action.confidence,
        "reasoning": action.reasoning[:300],
        "package": action.package,
        "files_touched": list(action.files_touched),
        "escalate_reason": action.escalate_reason[:300],
    }


def _render_needs_human_md(index: dict) -> str:
    """Render the OPEN entries of the index as the human-readable queue.

    Same per-failure layout as before; open items only, newest first. A short
    footer notes how many were auto-resolved so the file isn't silently lossy."""
    open_entries = [e for e in index.values() if e.get("status") == "open"]
    resolved = sum(1 for e in index.values() if e.get("status") == "resolved")
    open_entries.sort(key=lambda e: e.get("last_ts", 0), reverse=True)
    out = ["# Needs human - open escalations",
           "",
           f"_{len(open_entries)} open, {resolved} resolved (auto-cleared once their test passed)._"]
    for e in open_entries:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("last_ts", 0)))
        out.append(f"\n## {ts} -- `{e.get('nodeid','')}`")
        if e.get("escalation_count", 1) > 1:
            out.append(f"- **escalated**: {e['escalation_count']}x (first {time.strftime('%Y-%m-%d %H:%M', time.localtime(e.get('first_ts', 0)))})")
        out.append(f"- **reason**: {e.get('reason','')}")
        out.append(f"- **failure type**: {e.get('failure_type','')}")
        out.append(f"- **pytest message**: {e.get('message') or '(none)'}")
        a = e.get("action")
        if a:
            out.append(f"- **action proposed**: `{a.get('action')}` (confidence {a.get('confidence')})")
            if a.get("reasoning"):
                out.append(f"- **model reasoning**: {a['reasoning']}")
            if a.get("action") == "install_package" and a.get("package"):
                out.append(f"- **suggested package**: `{a['package']}`")
            if a.get("files_touched"):
                out.append(f"- **files touched**: {', '.join(a['files_touched'])}")
            if a.get("escalate_reason"):
                out.append(f"- **model escalate reason**: {a['escalate_reason']}")
        if e.get("extra"):
            out += ["", e["extra"]]
    return "\n".join(out) + "\n"


def _save_needs_human(project_root: Path, index: dict) -> Path:
    """Persist the index and re-render the .md view. On the FIRST structured
    write, archive any pre-existing freeform .md so legacy content isn't lost."""
    data_dir = Path(project_root) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    idx_path = needs_human_index_path(project_root)
    md_path = needs_human_md_path(project_root)
    if not idx_path.exists() and md_path.exists():
        try:
            md_path.replace(data_dir / "needs_human.archive.md")
        except OSError:
            pass
    idx_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    md_path.write_text(_render_needs_human_md(index), encoding="utf-8")
    return md_path


def append_needs_human(project_root: Path, *, failure: TestFailure,
                       action: Optional[FixAction], reason: str,
                       extra: str = "") -> Path:
    """Record (or update) an open escalation for a failing test, keyed by
    nodeid. Repeated escalations of the same test update the single entry and
    bump its count rather than appending a fresh section each attempt."""
    project_root = Path(project_root)
    index = load_needs_human_index(project_root)
    now = time.time()
    msg = (failure.message or "").splitlines()[0][:200] if failure.message else ""
    prev = index.get(failure.nodeid) or {}
    index[failure.nodeid] = {
        "nodeid": failure.nodeid,
        "status": "open",
        "reason": reason,
        "failure_type": failure.failure_type,
        "message": msg,
        "action": _action_summary(action),
        "extra": extra,
        "escalation_count": int(prev.get("escalation_count", 0)) + 1,
        "first_ts": prev.get("first_ts", now),
        "last_ts": now,
        "resolved_ts": None,
    }
    return _save_needs_human(project_root, index)


def resolve_needs_human(project_root: Path, *, nodeid: str) -> bool:
    """Mark any open escalation for `nodeid` resolved (its test passes now).
    Returns True if an open entry was cleared. Re-renders the .md view."""
    project_root = Path(project_root)
    index = load_needs_human_index(project_root)
    entry = index.get(nodeid)
    if not entry or entry.get("status") != "open":
        return False
    entry["status"] = "resolved"
    entry["resolved_ts"] = time.time()
    _save_needs_human(project_root, index)
    return True


# --- core: fix one failure --------------------------------------------------

def _try_action(
    *,
    failure: TestFailure,
    repo_dir: Path,
    bundle: FailureBundle,
    tr: TaskResult,
    baseline_fail: int,
    orig_branch: str,
    danger_patterns: List[str],
    dry_run: bool,
    python_exe: Optional[str],
    claude_retry_used: bool,
    t0: float,
    router: Optional[Router] = None,
    verify_mode: bool = False,
    graduation: Optional[dict] = None,
    graduation_path: Optional[Path] = None,
) -> FixAttempt:
    """Single attempt: parse tr -> action, gate, [verify], apply, check, return.

    Does NOT write to needs_human.md. The outer auto_fix_one decides when to
    escalate so the tier-2 retry can override a first-attempt escalation.

    verify-then-graduate: when verify_mode is on and this is a LOCAL proposal
    for a not-yet-graduated action-type, Claude reviews the proposal before
    apply. A rejection returns status="escalated" (so tier-2 then takes over).
    A verified-and-successful apply bumps the graduation counter.
    """
    base_kwargs = dict(failure_nodeid=failure.nodeid, handler=tr.handler,
                       claude_retry_used=claude_retry_used)

    def _done(**kw):
        return FixAttempt(
            duration_seconds=round(time.monotonic() - t0, 3),
            **base_kwargs, **kw,
        )

    if not tr.success:
        return _done(status="escalated",
                     reason=f"router call failed: {tr.error_type or tr.error}")

    action = parse_fix_action(tr.text or "")

    if action.action == "escalate" or action.confidence < CONFIDENCE_THRESHOLD:
        return _done(status="escalated", action=action,
                     reason=f"low confidence or escalate ({action.confidence:.2f})")

    # File-touching actions (apply_diff, replace_file) are gated identically:
    # danger-list + local-only test-file scope. install_package is validated via
    # _safe_package_spec; pip install is bounded and reversible.
    if action.action in ("apply_diff", "replace_file"):
        touched = list(action.files_touched)
        if any(is_danger_path(p, danger_patterns) for p in touched):
            return _done(status="escalated", action=action,
                         reason=f"{action.action} touches danger-listed path")

        # Local-only scope: the small local model can only edit test files.
        # Anything else escalates so tier 2 (Claude) can take the shot.
        if tr.handler == "local" and touched:
            non_test = [p for p in touched if not _is_test_file(p)]
            if non_test:
                return _done(status="escalated", action=action,
                             reason=f"local {action.action} touches non-test file(s) {non_test[:2]} -- escalating to Claude")
        # If files_touched is empty, the edit is unreviewable -- escalate.
        if tr.handler == "local" and not touched:
            return _done(status="escalated", action=action,
                         reason=f"local {action.action} with empty files_touched -- escalating")

    if dry_run:
        return _done(status="would_apply", action=action)

    # --- verify-then-graduate gate ------------------------------------------
    # Only local proposals get verified, only for graduatable action-types,
    # only while that type hasn't graduated yet, and only if Claude isn't
    # quota-blocked. A rejection escalates (tier-2 will then propose its own).
    did_verify = False
    if (verify_mode and router is not None and tr.handler == "local"
            and action.action in GRADUATABLE_ACTIONS
            and not is_graduated(graduation or {}, action.action)):
        blocked, _ = router.quota.is_blocked()
        if not blocked:
            vr = verify_action_with_claude(bundle, action, router)
            if not vr.approve:
                return _done(status="escalated", action=action,
                             reason=f"verification rejected: {vr.reason}")
            did_verify = True
        # If Claude is blocked we apply best-effort without verification
        # (verification is a safety net, not a hard gate).

    branch_name = f"auto-fix/{int(time.time())}-{_dedup_hash(failure)}"
    file_action = action.action in ("apply_diff", "replace_file")
    touched_files = list(action.files_touched) if file_action else []

    if file_action:
        br = create_working_branch(repo_dir, branch_name, target_files=touched_files)
        if not br.success:
            return _done(status="error", action=action, branch=branch_name,
                         reason=br.error)

    if action.action == "install_package":
        ar = apply_install_package(action.package, python_exe=python_exe)
    elif action.action == "apply_diff":
        ar = apply_diff(action.diff, repo_dir)
    elif action.action == "replace_file":
        ar = apply_replace_file(action.path, action.new_content, repo_dir)
    else:
        ar = ApplyResult(success=False, error=f"unhandled action {action.action!r}")

    if not ar.success:
        if file_action:
            revert_files(repo_dir, touched_files, orig_branch)
        return _done(status="apply_failed", action=action, branch=branch_name,
                     reason=ar.error)

    # Test-integrity guard: never accept a fix that weakens a touched test file
    # (removes assertions / adds skip|xfail) -- that would make the suite green
    # by masking the bug. Checked BEFORE pytest so we reject without a run.
    if touched_files:
        weak = check_no_test_weakening(repo_dir, touched_files)
        if weak:
            revert_files(repo_dir, touched_files, orig_branch)
            return _done(status="escalated", action=action, branch=branch_name,
                         reason=f"refused: edit weakens test ({weak})")

    after = run_pytest(repo_dir, lane="fast")
    after_fail = after.n_failed + after.n_errors

    if after_fail < baseline_fail:
        commit_note = ""
        if file_action:
            cr = commit_files(repo_dir, f"auto-fix: {failure.nodeid}", touched_files)
            if not cr.success:
                # The fix works (tests pass) but didn't land as a commit. Don't
                # fail the attempt over it -- surface a warning instead.
                commit_note = f"commit warning: {cr.error[:150]}"
        # Graduation: a verified-and-successful local apply is evidence this
        # action-type is trustworthy. Bump the counter; promote at threshold.
        if did_verify and graduation is not None:
            record_verified_success(graduation, action.action)
            if graduation_path is not None:
                save_graduation(graduation_path, graduation)
        return _done(status="fixed", action=action, branch=branch_name,
                     baseline_fail_count=baseline_fail, after_fail_count=after_fail,
                     reason=commit_note)

    if file_action:
        revert_files(repo_dir, touched_files, orig_branch)
    return _done(status="regressed", action=action, branch=branch_name,
                 baseline_fail_count=baseline_fail, after_fail_count=after_fail,
                 reason=f"no improvement (baseline={baseline_fail}, after={after_fail})")


# Outcomes for which tier-2 Claude retry can plausibly help.
_RETRYABLE_STATUSES = ("apply_failed", "regressed", "escalated")


def auto_fix_one(
    failure: TestFailure,
    repo_dir: Path,
    *,
    project_root: Path,
    router: Optional[Router] = None,
    danger_patterns: Optional[List[str]] = None,
    dry_run: bool = False,
    python_exe: Optional[str] = None,
    enable_claude_retry: bool = True,
    verify_mode: bool = False,
    graduation: Optional[dict] = None,
    graduation_path: Optional[Path] = None,
) -> FixAttempt:
    t0 = time.monotonic()
    repo_dir = Path(repo_dir)
    project_root = Path(project_root)
    if router is None:
        router = Router()
    if danger_patterns is None:
        danger_patterns = load_danger_list(project_root)
    if verify_mode and graduation is None:
        # Standalone call with verify on but no shared state -- load/own it.
        graduation_path = graduation_path or (project_root / "data" / "graduation_state.json")
        graduation = load_graduation(graduation_path)

    bundle = bundle_failure(
        failure, repo_dir,
        test_source_chars=FIX_TEST_SOURCE_CHARS,
        related_source_chars=FIX_RELATED_SOURCE_CHARS,
    )

    # Baseline pytest count (skipped for dry_run since we won't apply anything).
    if dry_run:
        baseline_fail = 0
        orig_branch = ""
    else:
        baseline = run_pytest(repo_dir, lane="fast")
        baseline_fail = baseline.n_failed + baseline.n_errors
        orig_branch = current_branch(repo_dir)

        # Early-exit: a sibling fix in the same batch may have resolved this
        # failure before we got to it (e.g. installing flask in attempt 1
        # fixes every test that uses the flask fixture). Detect by a clean
        # pytest baseline -- nothing to verify against, so any "fix" we tried
        # would be misclassified as "regressed" (after_fail==baseline_fail==0).
        if baseline_fail == 0:
            return FixAttempt(
                status="already_fixed",
                failure_nodeid=failure.nodeid,
                handler="",
                baseline_fail_count=0,
                after_fail_count=0,
                reason="pytest baseline is clean -- failure was resolved by an earlier attempt or external change",
                duration_seconds=round(time.monotonic() - t0, 3),
            )

    # --- First attempt: triage-routed (usually local) -----------------------
    prompt = build_fix_action_prompt(bundle)
    tr1 = router.handle(prompt)
    attempt = _try_action(
        failure=failure, repo_dir=repo_dir, bundle=bundle, tr=tr1,
        baseline_fail=baseline_fail, orig_branch=orig_branch,
        danger_patterns=danger_patterns, dry_run=dry_run,
        python_exe=python_exe, claude_retry_used=False, t0=t0,
        router=router, verify_mode=verify_mode,
        graduation=graduation, graduation_path=graduation_path,
    )

    # --- Tier 2: Claude fallback --------------------------------------------
    # Retry only if:
    #   - first attempt was handled by "local" (Claude already tried otherwise)
    #   - status is something Claude might fix (apply_failed/regressed/escalated)
    #   - quota is not blocked
    #   - we're not in dry-run mode
    retry_eligible = (
        enable_claude_retry
        and not dry_run
        and attempt.handler == "local"
        and attempt.status in _RETRYABLE_STATUSES
    )
    if retry_eligible:
        blocked, _ = router.quota.is_blocked()
        if not blocked:
            prior_outcome = attempt.status
            prior = _format_prior_attempt(
                attempt.action or FixAction(action="(none)"),
                prior_outcome, attempt.reason,
            )
            retry_prompt = build_fix_action_prompt(bundle, prior_attempt=prior)
            tr2 = router.handle_claude_only(
                retry_prompt,
                reason=f"tier-2 retry after local {prior_outcome}",
            )
            retry_attempt = _try_action(
                failure=failure, repo_dir=repo_dir, bundle=bundle, tr=tr2,
                baseline_fail=baseline_fail, orig_branch=orig_branch,
                danger_patterns=danger_patterns, dry_run=dry_run,
                python_exe=python_exe, claude_retry_used=True, t0=t0,
            )
            # Prefer the retry's outcome whether it succeeded or not -- it
            # reflects the freshest decision and is the one we want to record.
            attempt = retry_attempt

    # --- Final escalation write ---------------------------------------------
    # Only write to needs_human.md if the final state still needs a human.
    if attempt.status in ("escalated", "apply_failed", "regressed", "error"):
        if attempt.status == "regressed":
            reason = f"fix did not reduce failures (baseline={attempt.baseline_fail_count}, after={attempt.after_fail_count})"
        elif attempt.status == "apply_failed":
            reason = f"apply failed: {attempt.reason}"
        elif attempt.status == "error":
            reason = f"internal error: {attempt.reason}"
        else:
            reason = attempt.reason or "escalated"
        if attempt.claude_retry_used:
            reason = f"tier-2 Claude retry also failed: {reason}"
        append_needs_human(
            project_root, failure=failure,
            action=attempt.action, reason=reason,
        )

    return attempt


# --- multi-failure orchestration ---------------------------------------------

@dataclass
class AutoFixRun:
    attempts: List[FixAttempt] = field(default_factory=list)
    n_fixed: int = 0
    n_already_fixed: int = 0  # baseline was clean -- sibling fix resolved it
    n_escalated: int = 0
    n_regressed: int = 0
    n_skipped_dedup: int = 0
    n_skipped_capped: int = 0  # tier 3: hit attempt/regression cap
    n_claude_retried: int = 0  # tier 2: failures where Claude retry kicked in
    n_graduated: int = 0  # action-types that crossed the graduation threshold this run
    duration_seconds: float = 0.0


def _log_attempt_event(events_path: Path, attempt: FixAttempt) -> None:
    """Append one auto_fix_attempt event to events.jsonl."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event": "auto_fix_attempt",
        "timestamp": time.time(),
        "nodeid": attempt.failure_nodeid,
        "status": attempt.status,
        "handler": attempt.handler,
        "claude_retry_used": attempt.claude_retry_used,
        "attempt_count": attempt.attempt_count,
        "regressions": attempt.regressions,
        "baseline_fail": attempt.baseline_fail_count,
        "after_fail": attempt.after_fail_count,
        "duration_seconds": attempt.duration_seconds,
        "reason": attempt.reason[:300],
    }
    if attempt.action:
        payload["action"] = attempt.action.action
        payload["confidence"] = attempt.action.confidence
        if attempt.action.action == "install_package":
            payload["package"] = attempt.action.package
        if attempt.action.action == "apply_diff":
            payload["files_touched"] = attempt.action.files_touched
        if attempt.action.action == "replace_file":
            payload["files_touched"] = attempt.action.files_touched
            payload["path"] = attempt.action.path
    with events_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def auto_fix_failures(
    failures: List[TestFailure],
    repo_dir: Path,
    *,
    project_root: Path,
    router: Optional[Router] = None,
    danger_patterns: Optional[List[str]] = None,
    dry_run: bool = False,
    max_failures: Optional[int] = None,
    seen_path: Optional[Path] = None,
    dedup_window_seconds: int = DEDUP_WINDOW_SECONDS,
    python_exe: Optional[str] = None,
    enable_claude_retry: bool = True,
    verify_mode: bool = False,
    graduation_path: Optional[Path] = None,
) -> AutoFixRun:
    t0 = time.monotonic()
    project_root = Path(project_root)
    if seen_path is None:
        seen_path = project_root / "data" / "auto_fix_seen.json"
    seen = load_seen(seen_path)
    if router is None:
        router = Router()
    if danger_patterns is None:
        danger_patterns = load_danger_list(project_root)
    events_path = project_root / "data" / "events.jsonl"

    # Graduation state shared across all failures in this run, so a type that
    # graduates partway through stops getting verified for the rest.
    if graduation_path is None:
        graduation_path = project_root / "data" / "graduation_state.json"
    graduation = load_graduation(graduation_path) if verify_mode else {}
    grad_before = {k: bool(v.get("graduated")) for k, v in graduation.items()}

    run = AutoFixRun()
    pool = failures if max_failures is None else failures[:max_failures]
    now = time.time()

    for failure in pool:
        h = _dedup_hash(failure)
        prev = seen.get(h, {}) or {}
        prev_count = int(prev.get("attempt_count", 0) or 0)
        prev_regressions = int(prev.get("regressions", 0) or 0)
        last = float(prev.get("last_attempt_at", 0) or 0)

        # Tier 3 cap: hard-skip with long dedup once we've tried too many times
        # or seen too many regressions.
        if prev_count >= MAX_FAILED_ATTEMPTS or prev_regressions >= MAX_REGRESSIONS:
            capped_attempt = FixAttempt(
                status="skipped_capped",
                failure_nodeid=failure.nodeid,
                attempt_count=prev_count,
                regressions=prev_regressions,
                reason=(f"attempt_count={prev_count} regressions={prev_regressions} "
                        f"(caps: attempts={MAX_FAILED_ATTEMPTS}, regressions={MAX_REGRESSIONS})"),
            )
            run.attempts.append(capped_attempt)
            run.n_skipped_capped += 1
            # Only escalate once -- check whether we already wrote the cap note.
            if not prev.get("cap_escalated"):
                append_needs_human(
                    project_root, failure=failure, action=None,
                    reason=("hit retry cap: " + capped_attempt.reason),
                )
                prev["cap_escalated"] = True
            # Refresh long dedup so we don't spam the log.
            prev["last_attempt_at"] = time.time()
            prev["last_status"] = "skipped_capped"
            seen[h] = prev
            save_seen(seen_path, seen)
            _log_attempt_event(events_path, capped_attempt)
            continue

        # Standard time-window dedup (only if NOT capped -- caps take priority).
        if last and (now - last) < dedup_window_seconds:
            dedup_attempt = FixAttempt(
                status="skipped_dedup",
                failure_nodeid=failure.nodeid,
                attempt_count=prev_count,
                regressions=prev_regressions,
                reason="seen recently",
            )
            run.attempts.append(dedup_attempt)
            run.n_skipped_dedup += 1
            _log_attempt_event(events_path, dedup_attempt)
            continue

        attempt = auto_fix_one(
            failure, repo_dir,
            project_root=project_root,
            router=router,
            danger_patterns=danger_patterns,
            dry_run=dry_run,
            python_exe=python_exe,
            enable_claude_retry=enable_claude_retry,
            verify_mode=verify_mode,
            graduation=graduation,
            graduation_path=graduation_path,
        )

        # Bump tier-3 counters based on outcome. `already_fixed` is NOT a real
        # attempt -- baseline was clean before we did anything -- so it does
        # not consume retry budget.
        if attempt.status == "already_fixed":
            new_count = prev_count
            new_regressions = prev_regressions
        else:
            new_count = prev_count + 1
            new_regressions = prev_regressions
            if attempt.status == "regressed":
                # Only count a "true regression" (got worse) toward the regression cap.
                if attempt.after_fail_count > attempt.baseline_fail_count:
                    new_regressions += 1
        attempt.attempt_count = new_count
        attempt.regressions = new_regressions

        run.attempts.append(attempt)
        if attempt.status == "fixed":
            run.n_fixed += 1
            # Reset counters on success so a future regression in the same nodeid
            # gets a fresh budget. The hash will change if the traceback shifts,
            # but the nodeid alone may recur.
            new_count = 0
            new_regressions = 0
            prev.pop("cap_escalated", None)
            # Clear any prior open escalation for this test -- it passes now.
            resolve_needs_human(project_root, nodeid=failure.nodeid)
        elif attempt.status == "already_fixed":
            run.n_already_fixed += 1
            # Treat like a success for counter purposes -- nothing to retry.
            prev.pop("cap_escalated", None)
            # A sibling fix made this test pass -- clear its open escalation too.
            resolve_needs_human(project_root, nodeid=failure.nodeid)
        elif attempt.status == "escalated":
            run.n_escalated += 1
        elif attempt.status == "regressed":
            run.n_regressed += 1
        if attempt.claude_retry_used:
            run.n_claude_retried += 1

        prev.update({
            "nodeid": failure.nodeid,
            "last_attempt_at": time.time(),
            "last_status": attempt.status,
            "attempt_count": new_count,
            "regressions": new_regressions,
            "last_handler": attempt.handler,
            "last_claude_retry": attempt.claude_retry_used,
        })
        seen[h] = prev
        save_seen(seen_path, seen)
        _log_attempt_event(events_path, attempt)

    # Count action-types that newly graduated during this run.
    if verify_mode:
        for atype, entry in graduation.items():
            if entry.get("graduated") and not grad_before.get(atype):
                run.n_graduated += 1

    run.duration_seconds = round(time.monotonic() - t0, 3)
    return run
