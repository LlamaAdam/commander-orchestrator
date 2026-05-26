"""Smoke test for orchestrator.quota.QuotaTracker.

Covers:
    1. Empty state load -> defaults.
    2. Recording a successful call updates totals + history.
    3. Recording a rate_limit call sets blocked_until and persists.
    4. is_blocked() honors the timestamp and unblocks when the time passes.
    5. State survives a save/reload cycle.
    6. A corrupted JSON file is moved aside and replaced with a fresh state.

Exits 0 on pass, non-zero on first failure.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from orchestrator.quota import QuotaTracker  # noqa: E402


@dataclass
class FakeResult:
    """Duck-typed stand-in for ClaudeResult so the smoke test has no CLI dep."""

    success: bool = True
    error: str = ""
    error_type: str = ""
    retry_after_seconds: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    duration_seconds: float = 0.0


def fail(msg: str) -> None:
    print(f"[XX]  {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"[OK]  {msg}")


def main() -> int:
    import tempfile

    print("=" * 70)
    print("quota tracker smoke test")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "quota_state.json"

        # 1. Empty load
        tracker = QuotaTracker(path=path)
        blocked, secs = tracker.is_blocked(now=1000.0)
        if blocked or secs != 0.0:
            fail(f"empty tracker should not be blocked, got blocked={blocked} secs={secs}")
        if tracker.state.total_calls != 0:
            fail("empty tracker should have 0 total_calls")
        ok("empty state loads with defaults")

        # 2. Record successful calls
        tracker.record(
            FakeResult(
                success=True,
                input_tokens=10,
                output_tokens=20,
                total_cost_usd=0.001,
                duration_seconds=1.5,
            ),
            now=1000.0,
        )
        tracker.record(
            FakeResult(
                success=True,
                input_tokens=5,
                output_tokens=15,
                total_cost_usd=0.0005,
                duration_seconds=0.8,
            ),
            now=1010.0,
        )
        if tracker.state.total_calls != 2:
            fail(f"expected 2 calls, got {tracker.state.total_calls}")
        if tracker.state.total_successes != 2:
            fail(f"expected 2 successes, got {tracker.state.total_successes}")
        if tracker.state.total_input_tokens != 15:
            fail(f"expected 15 input tokens, got {tracker.state.total_input_tokens}")
        if tracker.state.total_output_tokens != 35:
            fail(f"expected 35 output tokens, got {tracker.state.total_output_tokens}")
        ok("successful calls update totals correctly")

        # File exists and is valid JSON
        if not path.exists():
            fail("state file should exist after record()")
        with path.open() as f:
            on_disk = json.load(f)
        if on_disk["total_calls"] != 2:
            fail("disk state doesn't match in-memory state")
        ok("state persisted to disk and re-readable")

        # 3. Rate-limit call sets block
        tracker.record(
            FakeResult(
                success=False,
                error="rate limit exceeded; try again in 30 minutes",
                error_type="rate_limit",
                retry_after_seconds=1800,
            ),
            now=2000.0,
        )
        blocked, secs = tracker.is_blocked(now=2000.0)
        if not blocked:
            fail("should be blocked immediately after rate_limit record")
        if not (1799 <= secs <= 1801):
            fail(f"expected ~1800s remaining, got {secs}")
        if tracker.state.total_rate_limits != 1:
            fail("total_rate_limits not incremented")
        ok("rate_limit call sets blocked_until correctly")

        # 4. is_blocked unblocks when time passes
        blocked, secs = tracker.is_blocked(now=2000.0 + 1800)
        if blocked or secs != 0.0:
            fail(f"should be unblocked at exact unblock time, got blocked={blocked} secs={secs}")
        blocked, secs = tracker.is_blocked(now=2000.0 + 9999)
        if blocked or secs != 0.0:
            fail("should remain unblocked well after expiry")
        ok("block auto-expires correctly")

        # 5. Reload state and verify it survives
        tracker2 = QuotaTracker(path=path)
        if tracker2.state.total_calls != 3:
            fail(f"reloaded total_calls expected 3, got {tracker2.state.total_calls}")
        if tracker2.state.total_rate_limits != 1:
            fail("reloaded total_rate_limits wrong")
        if len(tracker2.state.history) != 3:
            fail(f"reloaded history expected 3 entries, got {len(tracker2.state.history)}")
        ok("state survives save/reload cycle")

        # 6. Rate-limit with no retry_after falls back to default
        tracker3 = QuotaTracker(path=Path(tmp) / "fallback.json")
        tracker3.record(
            FakeResult(
                success=False,
                error="rate limited (no hint)",
                error_type="rate_limit",
                retry_after_seconds=None,
            ),
            now=5000.0,
        )
        if tracker3.state.last_block_retry_after != 3600:
            fail(
                f"expected fallback 3600s, got {tracker3.state.last_block_retry_after}"
            )
        ok("rate_limit without retry_after uses 1-hour fallback")

        # 7. Corrupted state file is replaced
        corrupt = Path(tmp) / "corrupt.json"
        corrupt.write_text("this is not valid JSON {{{")
        tracker4 = QuotaTracker(path=corrupt)
        if tracker4.state.total_calls != 0:
            fail("corrupt-recovered tracker should be empty")
        # Original bad file should be moved aside
        siblings = list(Path(tmp).glob("corrupt.json.corrupt-*"))
        if not siblings:
            fail("corrupt file should have been moved aside")
        ok("corrupt state file is moved aside and replaced with empty state")

        # 8. Summary shape
        summary = tracker2.summary(now=2000.0 + 100)
        for key in (
            "blocked",
            "seconds_until_unblock",
            "calls_last_hour",
            "lifetime",
        ):
            if key not in summary:
                fail(f"summary missing key: {key}")
        if not summary["blocked"]:
            fail("summary should show blocked=True 100s after the rate-limit")
        ok("summary() returns expected shape and values")

    print("\n[OK]  All quota smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
