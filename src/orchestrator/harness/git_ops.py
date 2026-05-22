"""Git operations for the test harness.

Idempotent clone / checkout / pull. All git invocations go through `_run_git`,
which captures stdout/stderr as UTF-8 with replacement (matches the Windows
encoding fix already in claude_cli.py).

Subprocess env: INHERITED. We do not scrub anything here — git may need
GIT_SSH_COMMAND, SSH_AUTH_SOCK, http.proxy, credential helpers, etc.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class GitResult:
    """Structured result from a clone/fetch/checkout/pull sequence."""

    success: bool
    operation: str  # "clone" | "fetch" | "checkout" | "pull"
    repo_dir: Path
    branch: Optional[str]
    head_sha: Optional[str]
    stdout: str
    stderr: str
    error: Optional[str]  # human-readable error, None if success


def _run_git(
    args: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 300,
) -> Tuple[int, str, str]:
    """Run a git subprocess. Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _head_sha(repo_dir: Path) -> Optional[str]:
    try:
        rc, out, _ = _run_git(["rev-parse", "HEAD"], cwd=repo_dir, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out.strip() if rc == 0 and out.strip() else None


def short_sha(sha: Optional[str], n: int = 8) -> str:
    """Return the first n chars of a sha, or empty string if sha is None/empty."""
    return (sha or "")[:n]


def _is_existing_clone(target_dir: Path) -> bool:
    return target_dir.exists() and (target_dir / ".git").exists()


def clone_or_update(
    repo_url: str,
    target_dir: Path,
    branch: Optional[str] = None,
    *,
    fetch_if_exists: bool = True,
    clone_depth: Optional[int] = None,
) -> GitResult:
    """Ensure `target_dir` is a clone of `repo_url` on `branch`.

    - If `target_dir` doesn't exist or isn't a git repo, clones fresh.
    - Otherwise: fetch (if `fetch_if_exists`), checkout `branch`, fast-forward pull.
    - `clone_depth`: pass an int for a shallow clone. Default None = full history.

    Idempotent. Safe to call repeatedly.
    """
    target_dir = Path(target_dir)

    if _is_existing_clone(target_dir):
        # Existing clone path.
        if fetch_if_exists:
            try:
                rc, out, err = _run_git(["fetch", "--all", "--prune"], cwd=target_dir)
            except subprocess.TimeoutExpired as exc:
                return GitResult(
                    success=False,
                    operation="fetch",
                    repo_dir=target_dir,
                    branch=branch,
                    head_sha=_head_sha(target_dir),
                    stdout="",
                    stderr=str(exc),
                    error="git fetch timed out",
                )
            if rc != 0:
                return GitResult(
                    success=False,
                    operation="fetch",
                    repo_dir=target_dir,
                    branch=branch,
                    head_sha=_head_sha(target_dir),
                    stdout=out,
                    stderr=err,
                    error=f"git fetch failed (rc={rc})",
                )

        if branch:
            rc, out, err = _run_git(["checkout", branch], cwd=target_dir)
            if rc != 0:
                return GitResult(
                    success=False,
                    operation="checkout",
                    repo_dir=target_dir,
                    branch=branch,
                    head_sha=_head_sha(target_dir),
                    stdout=out,
                    stderr=err,
                    error=f"git checkout {branch!r} failed (rc={rc})",
                )

            rc, out, err = _run_git(["pull", "--ff-only"], cwd=target_dir)
            if rc != 0:
                return GitResult(
                    success=False,
                    operation="pull",
                    repo_dir=target_dir,
                    branch=branch,
                    head_sha=_head_sha(target_dir),
                    stdout=out,
                    stderr=err,
                    error=f"git pull --ff-only failed (rc={rc})",
                )

        return GitResult(
            success=True,
            operation="pull" if branch else "fetch",
            repo_dir=target_dir,
            branch=branch,
            head_sha=_head_sha(target_dir),
            stdout="",
            stderr="",
            error=None,
        )

    # Fresh clone path.
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone"]
    if branch:
        args.extend(["--branch", branch])
    if clone_depth is not None:
        args.extend(["--depth", str(clone_depth)])
    args.extend([repo_url, str(target_dir)])

    try:
        rc, out, err = _run_git(args, timeout=900)
    except subprocess.TimeoutExpired as exc:
        return GitResult(
            success=False,
            operation="clone",
            repo_dir=target_dir,
            branch=branch,
            head_sha=None,
            stdout="",
            stderr=str(exc),
            error="git clone timed out",
        )

    if rc != 0:
        return GitResult(
            success=False,
            operation="clone",
            repo_dir=target_dir,
            branch=branch,
            head_sha=None,
            stdout=out,
            stderr=err,
            error=f"git clone failed (rc={rc})",
        )

    return GitResult(
        success=True,
        operation="clone",
        repo_dir=target_dir,
        branch=branch,
        head_sha=_head_sha(target_dir),
        stdout=out,
        stderr=err,
        error=None,
    )
