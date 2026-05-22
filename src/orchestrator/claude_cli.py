"""Subprocess wrapper around the `claude` CLI.

CRITICAL DESIGN INVARIANT
-------------------------
This module is the SINGLE chokepoint for every invocation of the `claude` CLI
made by the orchestrator. It exists for one reason: to guarantee that
`ANTHROPIC_API_KEY` (and related API-credential env vars) are NEVER inherited
by the `claude` subprocess. If those vars leak through, the CLI silently
switches from subscription billing to API-credit billing.

Subscription credentials are stored on disk at `~/.claude/.credentials.json`
on Windows (verified). The CLI reads them based on `USERPROFILE`/`HOME`, so
we must preserve those env vars; we only strip the API-key ones.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

# Env vars that route the Claude CLI to API billing.
# These MUST be removed from any subprocess invocation.
_API_KEY_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)


@dataclass
class ClaudeResult:
    """Structured result of a `claude -p` invocation."""

    success: bool
    text: str = ""
    error: str = ""
    error_type: str = ""  # "rate_limit" | "auth" | "timeout" | "unknown" | ""
    retry_after_seconds: int | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""
    return_code: int = 0
    duration_seconds: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    total_cost_usd: float | None = None
    raw_json: dict[str, Any] | None = None
    cmd: list[str] = field(default_factory=list)


def build_subscription_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env dict suitable for invoking `claude` under subscription auth.

    Inherits the parent process env, then removes every var that would cause
    the CLI to switch to API-key billing.
    """
    env = dict(base_env if base_env is not None else os.environ)
    for k in _API_KEY_ENV_VARS:
        env.pop(k, None)
    return env


def find_claude_binary() -> str:
    """Locate the `claude` CLI binary; return absolute path.

    Raises FileNotFoundError if missing.
    """
    path = shutil.which("claude")
    if not path:
        raise FileNotFoundError(
            "claude CLI not found on PATH. Install it with "
            "`npm install -g @anthropic-ai/claude-code` and reopen your shell."
        )
    return path


def _classify_error(text: str) -> str:
    """Best-effort classification of CLI error text into known categories."""
    lower = text.lower()
    if any(
        s in lower
        for s in (
            "rate limit",
            "rate-limit",
            "too many requests",
            "429",
            "quota",
            "usage limit",
            "5-hour",
            "5 hour",
            "exhausted",
        )
    ):
        return "rate_limit"
    if any(
        s in lower
        for s in (
            "unauthorized",
            "authentication",
            "401",
            "not logged in",
            "no credentials",
            "credential",
            "/login",
        )
    ):
        return "auth"
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    return "unknown"


def _extract_retry_after(text: str) -> int | None:
    """Try to pull a retry-after duration (seconds) from CLI error text.

    Heuristic — the CLI may print things like 'try again in 4h 23m' or
    'reset at 2026-05-19T05:00:00Z'. We start conservative and extend as we
    encounter real formats.
    """
    import re

    # "in NNN seconds"
    m = re.search(r"in (\d+) seconds?", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # "in NNh NNm"
    m = re.search(r"in (\d+)h\s*(\d+)m", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    # "in NN minutes"
    m = re.search(r"in (\d+) minutes?", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60
    # "in NN hours"
    m = re.search(r"in (\d+) hours?", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 3600
    return None


def _parse_json_envelope(stdout: str, result: ClaudeResult) -> None:
    """Populate result fields from a JSON-mode stdout payload.

    The CLI emits one of several JSON shapes depending on version. We attempt
    to be tolerant. Sets result.success, result.text, token fields, and error
    fields if applicable.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return

    if not isinstance(data, dict):
        return

    result.raw_json = data

    # Token usage — current claude-code JSON format
    usage = data.get("usage") or {}
    if isinstance(usage, dict):
        result.input_tokens = usage.get("input_tokens")
        result.output_tokens = usage.get("output_tokens")
        result.cache_read_tokens = usage.get("cache_read_input_tokens")
        result.cache_creation_tokens = usage.get("cache_creation_input_tokens")

    # Cost (newer CLI versions)
    if "total_cost_usd" in data:
        result.total_cost_usd = data.get("total_cost_usd")

    # Result text / error
    is_error = bool(data.get("is_error"))
    text_val = data.get("result") or data.get("content") or ""
    if isinstance(text_val, list):
        # Some shapes use a list of content blocks
        text_val = "".join(
            block.get("text", "")
            for block in text_val
            if isinstance(block, dict) and block.get("type") == "text"
        )

    if is_error:
        result.error = str(text_val or data.get("error") or "claude CLI reported is_error=true")
        result.error_type = _classify_error(result.error)
        result.retry_after_seconds = _extract_retry_after(result.error)
        result.success = False
        return

    result.text = str(text_val) if text_val else ""
    result.success = True


def run_claude(
    prompt: str,
    *,
    model: str | None = None,
    timeout_seconds: int = 120,
    extra_args: list[str] | None = None,
    cwd: str | None = None,
) -> ClaudeResult:
    """Invoke `claude -p` non-interactively and return a structured result.

    The subprocess env is built via build_subscription_env() — `ANTHROPIC_API_KEY`
    is explicitly absent so the CLI authenticates via the on-disk subscription
    credentials at `~/.claude/.credentials.json`.

    Args:
        prompt: The prompt text to send to Claude.
        model: Optional model name (e.g. "claude-sonnet-4-5"). If None, the CLI
            picks its default.
        timeout_seconds: Hard kill timeout for the subprocess.
        extra_args: Additional CLI flags to append before the prompt.
        cwd: Working directory for the subprocess.

    Returns:
        A ClaudeResult. On success, result.text contains the response. On
        failure, result.error and result.error_type are populated.
    """
    claude_bin = find_claude_binary()

    # The prompt is sent on stdin, NOT as a positional argument. On Windows,
    # `claude.CMD` is a batch wrapper and cmd.exe enforces an ~8KB command-
    # line length limit, so positional prompts longer than that fail with
    # "The command line is too long." (rc=1) before the CLI ever runs.
    # stdin has no such limit and is the supported input path: the CLI's
    # warning "no stdin data received in 3s, proceeding without it" exists
    # precisely so it can be fed via pipe.
    cmd: list[str] = [claude_bin, "-p", "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    if extra_args:
        cmd.extend(extra_args)

    env = build_subscription_env()

    result = ClaudeResult(success=False, cmd=cmd)

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            env=env,
            capture_output=True,
            text=True,
            # Claude CLI emits UTF-8. On Windows, Python defaults to cp1252,
            # which crashes on any byte that isn't a valid cp1252 codepoint
            # (e.g. em-dashes, smart quotes) and silently mojibake's the rest.
            # Pin to UTF-8 with errors="replace" as a safety net.
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        result.duration_seconds = time.perf_counter() - start
        result.error = f"claude CLI exceeded {timeout_seconds}s timeout"
        result.error_type = "timeout"
        return result

    result.duration_seconds = time.perf_counter() - start
    result.return_code = proc.returncode
    result.raw_stdout = proc.stdout
    result.raw_stderr = proc.stderr

    # Try JSON parse first (this also detects is_error inside the envelope).
    _parse_json_envelope(proc.stdout, result)

    # If JSON parse populated success/error, return.
    if result.success or result.error:
        return result

    # Fallback: non-JSON stdout or empty output.
    if proc.returncode == 0:
        result.success = True
        result.text = proc.stdout.strip()
        return result

    result.error = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
    result.error_type = _classify_error(result.error)
    result.retry_after_seconds = _extract_retry_after(result.error)
    return result


def assert_no_api_key_in_env(env: dict[str, str] | None = None) -> None:
    """Hard assertion to use in tests: ensure no API-key var is present.

    Raises AssertionError if any API-key var is set in the given env.
    """
    target = env if env is not None else dict(os.environ)
    leaked = [k for k in _API_KEY_ENV_VARS if k in target]
    if leaked:
        raise AssertionError(
            f"API-key env vars leaked into env: {leaked}. "
            f"This would route the CLI to API billing, not subscription."
        )
