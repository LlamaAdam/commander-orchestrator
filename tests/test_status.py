"""status.py — read-only snapshot (quota read, event summary, composition)."""
from __future__ import annotations

import json
import time

from orchestrator import status as st
from orchestrator.status import OllamaStatus


# --- _read_quota ------------------------------------------------------------

def test_read_quota_missing_file(tmp_path):
    qs = st._read_quota(tmp_path / "nope.json")
    assert qs.total_calls == 0 and qs.blocked is False


def test_read_quota_counts_and_block_future(tmp_path):
    p = tmp_path / "q.json"
    p.write_text(json.dumps({
        "total_calls": 9, "total_successes": 7,
        "total_input_tokens": 500, "total_output_tokens": 200,
        "blocked_until": time.time() + 600,
    }), encoding="utf-8")
    qs = st._read_quota(p)
    assert qs.total_calls == 9 and qs.total_successes == 7
    assert qs.total_input_tokens == 500 and qs.total_output_tokens == 200
    assert qs.blocked is True and qs.seconds_until_unblock > 0


def test_read_quota_past_block_not_blocked(tmp_path):
    p = tmp_path / "q.json"
    p.write_text(json.dumps({"blocked_until": time.time() - 100}), encoding="utf-8")
    assert st._read_quota(p).blocked is False


def test_read_quota_corrupt_returns_defaults(tmp_path):
    p = tmp_path / "q.json"
    p.write_text("{bad", encoding="utf-8")
    assert st._read_quota(p).total_calls == 0


# --- _summarize_events ------------------------------------------------------

def test_summarize_events_counts_window_and_types(tmp_path):
    now = time.time()
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in [
        {"event": "triage", "timestamp": now - 1},
        {"event": "triage", "timestamp": now - 2},
        {"event": "claude_call", "timestamp": now - 3},
        {"event": "local_call", "timestamp": now - 48 * 3600},  # outside 24h
    ]) + "\n", encoding="utf-8")
    s = st._summarize_events(p)
    assert s.total_events == 4
    assert s.last_24h_events == 3
    assert s.by_event == {"triage": 2, "claude_call": 1, "local_call": 1}
    assert s.last_event_type == "triage"


def test_summarize_events_skips_corrupt_and_missing(tmp_path):
    assert st._summarize_events(tmp_path / "none.jsonl").total_events == 0
    p = tmp_path / "e.jsonl"
    p.write_text('{"event":"a","timestamp":1}\ngarbage\n', encoding="utf-8")
    assert st._summarize_events(p).total_events == 1


# --- snapshot composition + formatting -------------------------------------

def test_get_status_snapshot_composes(tmp_path, monkeypatch):
    # Avoid the network: stub the Ollama probe.
    monkeypatch.setattr(st, "_check_ollama",
                        lambda host: OllamaStatus(reachable=True, available_models=["qwen2.5-coder:14b"]))
    q = tmp_path / "quota_state.json"
    q.write_text(json.dumps({"total_calls": 3}), encoding="utf-8")
    e = tmp_path / "events.jsonl"
    e.write_text(json.dumps({"event": "triage", "timestamp": time.time()}) + "\n", encoding="utf-8")

    snap = st.get_status_snapshot(tmp_path, quota_state_path=q, events_log_path=e)
    assert snap.ollama.reachable is True
    assert snap.quota.total_calls == 3
    assert snap.events.total_events == 1


def test_format_status_human_renders_blocked(monkeypatch):
    snap = st.StatusSnapshot(
        timestamp=time.time(), project_root="/x",
        ollama=OllamaStatus(reachable=False, error="conn refused"),
        quota=st.QuotaStatus(total_calls=5, blocked=True,
                             blocked_until=time.time() + 120, seconds_until_unblock=120),
        events=st.EventSummary(total_events=10, last_24h_events=4,
                               by_event={"triage": 6, "claude_call": 4},
                               last_event_timestamp=time.time(), last_event_type="triage"),
        quota_state_path="/x/q.json", events_log_path="/x/e.jsonl",
    )
    text = st.format_status_human(snap)
    assert "orchestrator status" in text
    assert "reachable: NO" in text
    assert "BLOCKED until" in text
    assert "triage" in text
