"""local_model.py — Ollama client (httpx stubbed; no real server)."""
from __future__ import annotations

import httpx

from orchestrator import local_model as lm


class _FakeResp:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._json


class _FakeClient:
    """Context-manager stand-in for httpx.Client. `behavior` maps the called
    method ('post'/'get') to either a _FakeResp or an Exception to raise.
    Captures the last posted payload for assertions."""
    last_payload = None

    def __init__(self, behavior, capture):
        self._behavior = behavior
        self._capture = capture

    def __init_subclass__(cls, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        self._capture["payload"] = json
        return self._respond("post")

    def get(self, url):
        return self._respond("get")

    def _respond(self, verb):
        b = self._behavior.get(verb)
        if isinstance(b, Exception):
            raise b
        return b


def _install_client(monkeypatch, behavior):
    capture = {}

    def factory(*a, **k):
        return _FakeClient(behavior, capture)

    monkeypatch.setattr(lm.httpx, "Client", factory)
    return capture


def test_generate_success_parses_metrics(monkeypatch):
    _install_client(monkeypatch, {"post": _FakeResp({
        "response": "hello", "prompt_eval_count": 12, "eval_count": 8,
        "eval_duration": 999, "model": "qwen2.5-coder:14b",
    })})
    r = lm.generate("hi")
    assert r.success is True
    assert r.text == "hello"
    assert r.prompt_eval_count == 12 and r.eval_count == 8
    assert r.model == "qwen2.5-coder:14b"


def test_generate_sets_json_format_and_options(monkeypatch):
    cap = _install_client(monkeypatch, {"post": _FakeResp({"response": "{}"})})
    lm.generate("hi", format_json=True, options={"temperature": 0.0})
    assert cap["payload"]["format"] == "json"
    assert cap["payload"]["options"] == {"temperature": 0.0}
    assert cap["payload"]["stream"] is False


def test_generate_http_error_returns_failure(monkeypatch):
    _install_client(monkeypatch, {"post": httpx.ConnectError("refused")})
    r = lm.generate("hi")
    assert r.success is False
    assert "ConnectError" in r.error


def test_ping_true_false(monkeypatch):
    _install_client(monkeypatch, {"get": _FakeResp({}, status_code=200)})
    assert lm.ping() is True
    _install_client(monkeypatch, {"get": httpx.ConnectError("x")})
    assert lm.ping() is False


def test_list_models(monkeypatch):
    _install_client(monkeypatch, {"get": _FakeResp(
        {"models": [{"name": "qwen2.5-coder:14b"}, {"name": "llama3"}]})})
    assert lm.list_models() == ["qwen2.5-coder:14b", "llama3"]
    _install_client(monkeypatch, {"get": httpx.ConnectError("x")})
    assert lm.list_models() == []
