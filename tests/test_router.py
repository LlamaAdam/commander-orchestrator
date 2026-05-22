"""Router — triage -> dispatch (local/claude), quota gating, event log."""
from __future__ import annotations

import json
import time

import orchestrator.router as router_mod
from orchestrator.router import Router
from orchestrator.triage import TriageDecision
from conftest import fake_claude_result, fake_local_result


def _router(tmp_path, **kw):
    return Router(
        events_path=tmp_path / "events.jsonl",
        quota_path=tmp_path / "quota_state.json",
        **kw,
    )


def _events(tmp_path):
    p = tmp_path / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_local_route_dispatches_local(tmp_path, monkeypatch):
    monkeypatch.setattr(router_mod.triage, "triage",
                        lambda *a, **k: TriageDecision(handler="local", reason="trivial", via="rule"))
    monkeypatch.setattr(router_mod.local_model, "generate",
                        lambda *a, **k: fake_local_result(text="done locally"))
    # Claude must NOT be touched on the local path.
    monkeypatch.setattr(router_mod.claude_cli, "run_claude",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("claude called")))

    r = _router(tmp_path)
    res = r.handle("rename foo to bar")
    assert res.success and res.handler == "local" and res.text == "done locally"
    kinds = [e["event"] for e in _events(tmp_path)]
    assert "triage" in kinds and "local_call" in kinds


def test_claude_route_dispatches_claude_and_records_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(router_mod.triage, "triage",
                        lambda *a, **k: TriageDecision(handler="claude", reason="hard", via="rule"))
    monkeypatch.setattr(router_mod.claude_cli, "run_claude",
                        lambda *a, **k: fake_claude_result(text="done remotely"))
    r = _router(tmp_path)
    res = r.handle("design a system")
    assert res.success and res.handler == "claude" and res.text == "done remotely"
    assert r.quota.state.total_calls == 1
    kinds = [e["event"] for e in _events(tmp_path)]
    assert "claude_call" in kinds


def test_claude_route_blocked_does_not_call_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(router_mod.triage, "triage",
                        lambda *a, **k: TriageDecision(handler="claude", reason="hard", via="rule"))
    monkeypatch.setattr(router_mod.claude_cli, "run_claude",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("claude called while blocked")))
    r = _router(tmp_path)
    r.quota.state.blocked_until = time.time() + 10_000  # block hard

    res = r.handle("design a system")
    assert res.success is False
    assert res.blocked is True
    assert res.seconds_until_unblock > 0
    kinds = [e["event"] for e in _events(tmp_path)]
    assert "claude_blocked" in kinds


def test_handle_claude_only_bypasses_triage(tmp_path, monkeypatch):
    # triage.triage must NOT be called on the forced path.
    monkeypatch.setattr(router_mod.triage, "triage",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("triage called")))
    monkeypatch.setattr(router_mod.claude_cli, "run_claude",
                        lambda *a, **k: fake_claude_result(text="reviewed"))
    r = _router(tmp_path)
    res = r.handle_claude_only("verify this fix", reason="verify local proposal")
    assert res.success and res.handler == "claude"
    ev = _events(tmp_path)
    triage_ev = [e for e in ev if e["event"] == "triage"]
    assert triage_ev and triage_ev[0]["decision"]["via"] == "forced-fallback"


def test_handle_claude_only_still_respects_block(tmp_path, monkeypatch):
    monkeypatch.setattr(router_mod.claude_cli, "run_claude",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("claude called while blocked")))
    r = _router(tmp_path)
    r.quota.state.blocked_until = time.time() + 10_000
    res = r.handle_claude_only("verify this fix")
    assert res.blocked is True and res.success is False
