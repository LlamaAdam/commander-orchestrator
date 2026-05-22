"""Persistent quota / rate-limit tracker for the Claude CLI.

Design
------
We cannot read Claude's remaining-quota directly through the CLI, so this
tracker is REACTIVE, not predictive:

    - On every call, we record the outcome and the tokens consumed.
    - When a call comes back with error_type == "rate_limit", we set
      `blocked_until = now + retry_after_seconds` (with a conservative
      fallback if the retry hint couldn't be parsed) and persist.
    - `is_blocked()` is the only gating signal the orchestrator uses to
      decide whether to dispatch a new Claude task.

Token counts and estimated cost are kept for telemetry (visible in
`orch status` and the health check) — they DO NOT participate in gating.
The `cost_usd` field reported by the CLI's JSON envelope is treated as
an indicative number, not a precise figure (the CLI's reported value has
been observed to overshoot real subscription cost dramatically).

Storage
-------
A single JSON file (default: `<cwd>/data/quota_state.json`). Writes are
atomic via tempfile + os.replace so a crash mid-write cannot corrupt the
file. State is bounded — the rolling call log is capped at 500 entries
and entries older than 7 days are pruned on save.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Fallback block duration when the CLI says rate-limited but didn't give
# a parseable retry hint. Conservative to avoid spamming.
_DEFAULT_RATE_LIMIT_BLOCK_SECONDS = 60 * 60  # 1 hour

# Bound the rolling call log.
_MAX_CALL_HISTORY = 500
_CALL_HISTORY_MAX_AGE_SECONDS = 7 * 24 * 3600


@dataclass
class CallRecord:
    """One historical Claude CLI invocation."""

    timestamp: float
    success: bool
    error_type: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


@dataclass
class QuotaState:
    """Persistent state for the rate-limit tracker."""

    # Gating
    blocked_until: float | None = None  # epoch seconds, or None
    last_block_reason: str = ""
    last_block_at: float | None = None
    last_block_retry_after: int | None = None

    # Telemetry
    total_calls: int = 0
    total_successes: int = 0
    total_rate_limits: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    history: list[CallRecord] = field(default_factory=list)

    # ---- (de)serialization ------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict already converts dataclasses recursively
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "QuotaState":
        history_raw = d.get("history", [])
        history = [CallRecord(**rec) for rec in history_raw]
        return cls(
            blocked_until=d.get("blocked_until"),
            last_block_reason=d.get("last_block_reason", ""),
            last_block_at=d.get("last_block_at"),
            last_block_retry_after=d.get("last_block_retry_after"),
            total_calls=d.get("total_calls", 0),
            total_successes=d.get("total_successes", 0),
            total_rate_limits=d.get("total_rate_limits", 0),
            total_input_tokens=d.get("total_input_tokens", 0),
            total_output_tokens=d.get("total_output_tokens", 0),
            total_cost_usd=d.get("total_cost_usd", 0.0),
            history=history,
        )


class QuotaTracker:
    """Persistent reactive rate-limit tracker.

    Use this around every `claude` CLI invocation:

        tracker = QuotaTracker(path="data/quota_state.json")
        blocked, seconds_left = tracker.is_blocked()
        if blocked:
            # queue the task; reset_at = time.time() + seconds_left
            ...
        else:
            result = run_claude(prompt)
            tracker.record(result)
    """

    def __init__(self, path: str | Path = "data/quota_state.json"):
        self.path = Path(path)
        self.state = self._load()

    # ---- load / save -----------------------------------------------------

    def _load(self) -> QuotaState:
        if not self.path.exists():
            return QuotaState()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return QuotaState.from_dict(data)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            # Corrupted state file. Don't crash the orchestrator — start fresh,
            # but back up the bad file for forensics.
            backup = self.path.with_suffix(self.path.suffix + f".corrupt-{int(time.time())}")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            print(f"[quota] WARN: failed to load {self.path}: {e}. Started fresh.")
            return QuotaState()

    def save(self, now: float | None = None) -> None:
        """Atomically persist state. Prunes history first.

        Args:
            now: Reference timestamp for the history-age cutoff. If None, falls
                back to max(wall_clock, latest_record_timestamp). This matters
                when record() is called with a synthetic `now` (tests, backfills)
                that is much older than wall clock — without threading the same
                timestamp through, the prune step would wipe the history.
        """
        self._prune_history(now=now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state.to_dict(), f, indent=2)
            os.replace(tmp_path, self.path)
        except Exception:
            # Clean up the tempfile if anything goes wrong.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _prune_history(self, now: float | None = None) -> None:
        if now is None:
            # Use the latest record timestamp OR wall clock, whichever is later.
            # This keeps tests / backfills with synthetic timestamps from being
            # silently wiped by a wall-clock-based cutoff.
            if self.state.history:
                now = max(time.time(), max(r.timestamp for r in self.state.history))
            else:
                now = time.time()
        cutoff = now - _CALL_HISTORY_MAX_AGE_SECONDS
        self.state.history = [r for r in self.state.history if r.timestamp >= cutoff]
        if len(self.state.history) > _MAX_CALL_HISTORY:
            self.state.history = self.state.history[-_MAX_CALL_HISTORY:]

    # ---- gating ----------------------------------------------------------

    def is_blocked(self, now: float | None = None) -> tuple[bool, float]:
        """Return (blocked, seconds_until_unblock).

        If not blocked, seconds_until_unblock is 0.0.
        If blocked, it's the positive duration until the block lifts.
        """
        now = now if now is not None else time.time()
        if self.state.blocked_until is None:
            return False, 0.0
        if self.state.blocked_until <= now:
            # Block expired — clear it and persist on next save.
            self.state.blocked_until = None
            return False, 0.0
        return True, self.state.blocked_until - now

    # ---- recording -------------------------------------------------------

    def record(self, result, now: float | None = None) -> None:
        """Update state from a ClaudeResult (or a duck-typed equivalent).

        Records the outcome to history + lifetime counters. If the result was
        a rate-limit, sets `blocked_until` based on `retry_after_seconds` (with
        a conservative fallback). Always persists after recording.
        """
        now = now if now is not None else time.time()

        rec = CallRecord(
            timestamp=now,
            success=bool(getattr(result, "success", False)),
            error_type=getattr(result, "error_type", "") or "",
            input_tokens=getattr(result, "input_tokens", None) or 0,
            output_tokens=getattr(result, "output_tokens", None) or 0,
            cost_usd=getattr(result, "total_cost_usd", None) or 0.0,
            duration_seconds=getattr(result, "duration_seconds", 0.0) or 0.0,
        )

        self.state.history.append(rec)
        self.state.total_calls += 1
        if rec.success:
            self.state.total_successes += 1
        self.state.total_input_tokens += rec.input_tokens
        self.state.total_output_tokens += rec.output_tokens
        self.state.total_cost_usd += rec.cost_usd

        if rec.error_type == "rate_limit":
            self.state.total_rate_limits += 1
            retry_after = getattr(result, "retry_after_seconds", None)
            if retry_after is None or retry_after <= 0:
                retry_after = _DEFAULT_RATE_LIMIT_BLOCK_SECONDS
            self.state.blocked_until = now + retry_after
            self.state.last_block_reason = (getattr(result, "error", "") or "")[:500]
            self.state.last_block_at = now
            self.state.last_block_retry_after = int(retry_after)

        self.save(now=now)

    # ---- inspection ------------------------------------------------------

    def summary(self, now: float | None = None) -> dict:
        """Return a dict summarizing current state — for `orch status` and health checks."""
        now = now if now is not None else time.time()
        blocked, seconds_left = self.is_blocked(now)
        recent = [r for r in self.state.history if r.timestamp >= now - 3600]
        recent_success = sum(1 for r in recent if r.success)
        recent_rate_limit = sum(1 for r in recent if r.error_type == "rate_limit")
        return {
            "blocked": blocked,
            "seconds_until_unblock": int(seconds_left),
            "blocked_until_iso": (
                _epoch_to_iso(self.state.blocked_until) if self.state.blocked_until else None
            ),
            "last_block_reason": self.state.last_block_reason,
            "last_block_at_iso": (
                _epoch_to_iso(self.state.last_block_at) if self.state.last_block_at else None
            ),
            "calls_last_hour": len(recent),
            "successes_last_hour": recent_success,
            "rate_limits_last_hour": recent_rate_limit,
            "lifetime": {
                "total_calls": self.state.total_calls,
                "total_successes": self.state.total_successes,
                "total_rate_limits": self.state.total_rate_limits,
                "total_input_tokens": self.state.total_input_tokens,
                "total_output_tokens": self.state.total_output_tokens,
                "total_cost_usd_reported": round(self.state.total_cost_usd, 4),
            },
        }


def _epoch_to_iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
