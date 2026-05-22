"""claude_cli pure helpers — the billing-safety invariant + result parsing.

These never shell out to `claude`; they pin the logic the orchestrator's
quota gating and subscription-billing guarantee depend on.
"""
from __future__ import annotations

import json

import pytest

from orchestrator import claude_cli as cc
from orchestrator.claude_cli import ClaudeResult


# --- billing-safety invariant ----------------------------------------------

def test_build_subscription_env_strips_api_keys():
    base = {
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "USERPROFILE": r"C:\Users\pilot",
        "HOME": "/home/pilot",
        "PATH": "/usr/bin",
    }
    env = cc.build_subscription_env(base)
    # The two billing-routing vars are gone...
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    # ...but the auth-locating vars survive (CLI reads on-disk subscription creds).
    assert env["USERPROFILE"] == r"C:\Users\pilot"
    assert env["HOME"] == "/home/pilot"
    assert env["PATH"] == "/usr/bin"


def test_build_subscription_env_does_not_mutate_input():
    base = {"ANTHROPIC_API_KEY": "x", "PATH": "/bin"}
    cc.build_subscription_env(base)
    assert base["ANTHROPIC_API_KEY"] == "x"  # original untouched


def test_assert_no_api_key_in_env():
    cc.assert_no_api_key_in_env({"PATH": "/bin"})  # no raise
    with pytest.raises(AssertionError):
        cc.assert_no_api_key_in_env({"ANTHROPIC_API_KEY": "x"})


# --- error classification (feeds quota gating) ------------------------------

@pytest.mark.parametrize("text", [
    "Error: rate limit exceeded",
    "429 Too Many Requests",
    "You've hit your usage limit",
    "5-hour limit reached",
    "quota exhausted",
])
def test_classify_rate_limit(text):
    assert cc._classify_error(text) == "rate_limit"


@pytest.mark.parametrize("text,expected", [
    ("401 Unauthorized", "auth"),
    ("Not logged in — run /login", "auth"),
    ("no credentials found", "auth"),
    ("request timed out", "timeout"),
    ("something weird happened", "unknown"),
])
def test_classify_other_errors(text, expected):
    assert cc._classify_error(text) == expected


# --- retry-after extraction -------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("try again in 30 seconds", 30),
    ("retry in 2h 30m", 2 * 3600 + 30 * 60),
    ("come back in 15 minutes", 15 * 60),
    ("reset in 3 hours", 3 * 3600),
    ("no hint here", None),
])
def test_extract_retry_after(text, expected):
    assert cc._extract_retry_after(text) == expected


# --- JSON envelope parsing --------------------------------------------------

def test_parse_envelope_success_with_usage_and_cost():
    r = ClaudeResult(success=False)
    cc._parse_json_envelope(json.dumps({
        "result": "the answer",
        "is_error": False,
        "usage": {"input_tokens": 120, "output_tokens": 45,
                  "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
        "total_cost_usd": 0.033,
    }), r)
    assert r.success is True
    assert r.text == "the answer"
    assert r.input_tokens == 120 and r.output_tokens == 45
    assert r.cache_read_tokens == 10 and r.cache_creation_tokens == 5
    assert r.total_cost_usd == 0.033


def test_parse_envelope_is_error_sets_rate_limit_and_retry():
    r = ClaudeResult(success=False)
    cc._parse_json_envelope(json.dumps({
        "is_error": True,
        "result": "rate limit exceeded, try again in 45 seconds",
    }), r)
    assert r.success is False
    assert r.error_type == "rate_limit"
    assert r.retry_after_seconds == 45


def test_parse_envelope_content_block_list():
    r = ClaudeResult(success=False)
    cc._parse_json_envelope(json.dumps({
        "content": [{"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"}],
        "is_error": False,
    }), r)
    assert r.success is True
    assert r.text == "hello world"


def test_parse_envelope_non_json_is_noop():
    r = ClaudeResult(success=False)
    cc._parse_json_envelope("not json at all", r)
    # Untouched: caller's fallback path handles plain stdout.
    assert r.success is False and r.text == "" and r.raw_json is None
