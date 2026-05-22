"""Triage — rule pre-filter + Llama classifier with safe-default fallback."""
from __future__ import annotations

import orchestrator.triage as triage
from conftest import fake_local_result


# --- rule pre-filter (no Llama) --------------------------------------------

def test_rule_routes_trivial_to_local():
    for task in ("rename foo to bar", "fix a typo in the README",
                 "run ruff on the package", "bump the version to 2.0"):
        d = triage.triage(task)
        assert d.handler == "local", task
        assert d.via == "rule"


def test_rule_routes_short_question_to_local():
    d = triage.triage("what is sorted() in Python?")
    assert d.handler == "local"
    assert d.rule_name == "short-question"


def test_rule_routes_complex_to_claude():
    for task in ("design a caching layer for the API",
                 "investigate the bug in the parser failure",
                 "this is a multi-file change"):
        d = triage.triage(task)
        assert d.handler == "claude", task
        assert d.via == "rule"


# --- Llama classifier path --------------------------------------------------

_NEUTRAL = "Compute the nth Fibonacci number with memoization"


def test_classifier_local_decision(monkeypatch):
    monkeypatch.setattr(triage.local_model, "generate", lambda *a, **k: fake_local_result(
        text='{"handler": "local", "reason": "small fn", "complexity": "small", "estimated_files": 1}'))
    d = triage.triage(_NEUTRAL)
    assert d.handler == "local"
    assert d.via == "llama"
    assert d.complexity == "small"
    assert d.estimated_files == 1


def test_classifier_claude_decision(monkeypatch):
    monkeypatch.setattr(triage.local_model, "generate", lambda *a, **k: fake_local_result(
        text='{"handler": "claude", "reason": "needs context"}'))
    d = triage.triage(_NEUTRAL)
    assert d.handler == "claude"
    assert d.via == "llama"


def test_classifier_failure_falls_back_to_claude(monkeypatch):
    monkeypatch.setattr(triage.local_model, "generate",
                        lambda *a, **k: fake_local_result(success=False, error="ollama down"))
    d = triage.triage(_NEUTRAL)
    assert d.handler == "claude"
    assert d.via == "fallback"


def test_classifier_non_json_falls_back_to_claude(monkeypatch):
    monkeypatch.setattr(triage.local_model, "generate",
                        lambda *a, **k: fake_local_result(text="sure! handler=local"))
    d = triage.triage(_NEUTRAL)
    assert d.handler == "claude"
    assert d.via == "fallback"


def test_classifier_unknown_handler_falls_back_to_claude(monkeypatch):
    monkeypatch.setattr(triage.local_model, "generate", lambda *a, **k: fake_local_result(
        text='{"handler": "gpt5", "reason": "made up"}'))
    d = triage.triage(_NEUTRAL)
    assert d.handler == "claude"
    assert d.via == "fallback"
