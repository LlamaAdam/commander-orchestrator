"""`orch report` — event roll-up metrics."""
from __future__ import annotations

import json

from orchestrator import report as rpt
from orchestrator.auto_fix import MAX_FAILED_ATTEMPTS


def _events():
    return [
        {"event": "triage", "decision": {"handler": "local", "via": "rule"}},
        {"event": "triage", "decision": {"handler": "claude", "via": "fallback"}},
        {"event": "local_call", "success": True, "output_tokens": 30},
        {"event": "claude_call", "success": True, "error_type": "",
         "input_tokens": 100, "output_tokens": 40, "cost_usd_reported": 0.02},
        {"event": "claude_call", "success": False, "error_type": "rate_limit",
         "input_tokens": 0, "output_tokens": 0, "cost_usd_reported": 0.0},
        # tier-1 local fix (no retry)
        {"event": "auto_fix_attempt", "status": "fixed", "claude_retry_used": False},
        # tier-2 Claude fix (retry used)
        {"event": "auto_fix_attempt", "status": "fixed", "claude_retry_used": True},
        {"event": "auto_fix_attempt", "status": "escalated", "claude_retry_used": True},
        {"event": "auto_fix_attempt", "status": "already_fixed", "claude_retry_used": False},
        {"event": "idle_streak", "streak": 3},
        {"event": "idle_streak", "streak": 1},
    ]


def test_build_report_core_counts():
    r = rpt.build_report(".", events=_events(), seen={}, graduation={})
    assert r["events_total"] == 11
    assert r["by_type"]["auto_fix_attempt"] == 4
    fx = r["fixes"]
    assert fx["attempts_total"] == 4
    # real attempts exclude already_fixed -> fixed, fixed, escalated = 3
    assert fx["real_attempts"] == 3
    assert fx["success_rate"] == round(2 / 3, 3)
    assert fx["by_status"]["fixed"] == 2


def test_build_report_tier_split():
    r = rpt.build_report(".", events=_events(), seen={}, graduation={})
    t = r["tiers"]
    assert t["tier1_local_fixed"] == 1
    assert t["tier2_claude_fixed"] == 1
    assert t["claude_retry_attempts"] == 2


def test_build_report_claude_and_local_telemetry():
    r = rpt.build_report(".", events=_events(), seen={}, graduation={})
    c = r["claude"]
    assert c["calls"] == 2 and c["successes"] == 1 and c["rate_limited"] == 1
    assert c["input_tokens"] == 100 and c["output_tokens"] == 40
    assert c["cost_usd_reported"] == 0.02
    assert r["local"]["calls"] == 1 and r["local"]["output_tokens"] == 30


def test_build_report_triage_and_idle():
    r = rpt.build_report(".", events=_events(), seen={}, graduation={})
    assert r["triage"]["handler"] == {"local": 1, "claude": 1}
    assert r["triage"]["via"] == {"rule": 1, "fallback": 1}
    assert r["idle"]["max_streak"] == 3


def test_build_report_dedup_caps_and_graduation():
    seen = {
        "h1": {"attempt_count": MAX_FAILED_ATTEMPTS, "regressions": 0},  # capped
        "h2": {"attempt_count": 1, "regressions": 0},                    # not capped
    }
    grad = {"install_package": {"successes": 10, "graduated": True},
            "apply_diff": {"successes": 3, "graduated": False}}
    r = rpt.build_report(".", events=[], seen=seen, graduation=grad)
    assert r["dedup"]["tracked_failures"] == 2
    assert r["dedup"]["capped"] == 1
    assert r["graduation"]["install_package"]["graduated"] is True
    assert r["graduation"]["apply_diff"]["successes"] == 3


def test_build_report_empty_is_safe():
    r = rpt.build_report(".", events=[], seen={}, graduation={})
    assert r["events_total"] == 0
    assert r["fixes"]["success_rate"] is None  # no real attempts -> n/a


def test_format_report_renders_without_error():
    r = rpt.build_report(".", events=_events(), seen={"h": {"attempt_count": 1}},
                         graduation={"install_package": {"successes": 2, "graduated": False}})
    text = rpt.format_report(r)
    assert "Orchestrator activity report" in text
    assert "tier-1 local fixed: 1" in text
    assert "tier-2 Claude fixed: 1" in text


def test_load_events_skips_corrupt_lines(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text('{"event": "a"}\nnot json\n\n{"event": "b"}\n', encoding="utf-8")
    evs = rpt.load_events(p)
    assert [e["event"] for e in evs] == ["a", "b"]


def test_load_events_missing_file(tmp_path):
    assert rpt.load_events(tmp_path / "nope.jsonl") == []


def test_build_report_reads_files(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "events.jsonl").write_text(
        json.dumps({"event": "auto_fix_attempt", "status": "fixed",
                    "claude_retry_used": False}) + "\n", encoding="utf-8")
    (data / "auto_fix_seen.json").write_text(json.dumps({"h": {"attempt_count": 1}}),
                                             encoding="utf-8")
    (data / "graduation_state.json").write_text(
        json.dumps({"apply_diff": {"successes": 1, "graduated": False}}), encoding="utf-8")
    r = rpt.build_report(tmp_path)
    assert r["fixes"]["by_status"]["fixed"] == 1
    assert r["dedup"]["tracked_failures"] == 1
    assert "apply_diff" in r["graduation"]
