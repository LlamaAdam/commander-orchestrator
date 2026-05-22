"""QuotaTracker — reactive rate-limit gating + persistence."""
from __future__ import annotations

from orchestrator.quota import (
    QuotaTracker,
    _DEFAULT_RATE_LIMIT_BLOCK_SECONDS,
)
from conftest import fake_claude_result


def _tracker(tmp_path):
    return QuotaTracker(path=tmp_path / "quota_state.json")


def test_fresh_tracker_not_blocked(tmp_path):
    blocked, secs = _tracker(tmp_path).is_blocked(now=1000.0)
    assert blocked is False and secs == 0.0


def test_rate_limit_sets_block_with_retry_after(tmp_path):
    t = _tracker(tmp_path)
    t.record(
        fake_claude_result(success=False, error_type="rate_limit",
                           error="429", retry_after_seconds=120),
        now=1000.0,
    )
    blocked, secs = t.is_blocked(now=1000.0)
    assert blocked is True
    assert secs == 120.0
    # Block lifts exactly at the deadline.
    assert t.is_blocked(now=1120.0)[0] is False


def test_rate_limit_without_hint_uses_conservative_default(tmp_path):
    t = _tracker(tmp_path)
    t.record(fake_claude_result(success=False, error_type="rate_limit",
                                retry_after_seconds=None), now=0.0)
    blocked, secs = t.is_blocked(now=0.0)
    assert blocked is True
    assert secs == float(_DEFAULT_RATE_LIMIT_BLOCK_SECONDS)


def test_success_does_not_block_and_counts_telemetry(tmp_path):
    t = _tracker(tmp_path)
    t.record(fake_claude_result(success=True, input_tokens=100,
                                output_tokens=50, total_cost_usd=0.25), now=5.0)
    assert t.is_blocked(now=6.0)[0] is False
    assert t.state.total_calls == 1
    assert t.state.total_successes == 1
    assert t.state.total_input_tokens == 100
    assert t.state.total_output_tokens == 50


def test_block_expiry_clears_blocked_until(tmp_path):
    t = _tracker(tmp_path)
    t.record(fake_claude_result(success=False, error_type="rate_limit",
                                retry_after_seconds=60), now=0.0)
    # Past the deadline, is_blocked both reports False AND clears state.
    assert t.is_blocked(now=61.0)[0] is False
    assert t.state.blocked_until is None


def test_state_persists_across_instances(tmp_path):
    t1 = _tracker(tmp_path)
    t1.record(fake_claude_result(success=False, error_type="rate_limit",
                                 retry_after_seconds=300), now=1000.0)
    # New tracker reads the same file -> still blocked.
    t2 = QuotaTracker(path=tmp_path / "quota_state.json")
    blocked, secs = t2.is_blocked(now=1000.0)
    assert blocked is True and secs == 300.0
    assert t2.state.total_rate_limits == 1


def test_corrupt_state_file_starts_fresh_and_backs_up(tmp_path):
    p = tmp_path / "quota_state.json"
    p.write_text("{ this is not json", encoding="utf-8")
    t = QuotaTracker(path=p)  # must not raise
    assert t.state.total_calls == 0
    # Corrupt file was moved aside for forensics.
    assert list(tmp_path.glob("quota_state.json.corrupt-*"))


def test_summary_reports_block_and_recent_window(tmp_path):
    t = _tracker(tmp_path)
    t.record(fake_claude_result(success=True), now=10_000.0)
    t.record(fake_claude_result(success=False, error_type="rate_limit",
                                retry_after_seconds=90), now=10_000.0)
    s = t.summary(now=10_000.0)
    assert s["blocked"] is True
    assert s["calls_last_hour"] == 2
    assert s["successes_last_hour"] == 1
    assert s["rate_limits_last_hour"] == 1
    assert s["lifetime"]["total_calls"] == 2
