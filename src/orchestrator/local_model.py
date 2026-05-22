"""Thin client for the local Ollama HTTP API.

Default endpoint: http://localhost:11434

We use the /api/generate endpoint with stream=False so the call blocks until
the response is complete. For the triage classifier we pass format="json" to
constrain Ollama's output to a JSON object.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:14b-instruct-q6_K"


@dataclass
class OllamaResult:
    """Structured result of a /api/generate call."""

    success: bool
    text: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    prompt_eval_count: int = 0  # tokens in prompt (Ollama metric)
    eval_count: int = 0  # tokens generated
    eval_duration_ns: int = 0
    model: str = ""
    raw_json: dict[str, Any] = field(default_factory=dict)


def generate(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
    format_json: bool = False,
    options: dict[str, Any] | None = None,
    timeout: float = 180.0,
    keep_alive: str | int | None = None,
) -> OllamaResult:
    """Call Ollama's /api/generate non-streaming.

    Args:
        prompt: Prompt text.
        model: Model name. Default is the qwen coder model pulled in step 1.
        base_url: Ollama server URL (default http://localhost:11434).
        format_json: If True, constrain Ollama output to a JSON object.
        options: Sampling overrides — e.g. {"temperature": 0.0, "num_ctx": 8192}.
        timeout: HTTP timeout in seconds.
        keep_alive: How long Ollama should keep the model loaded after this
            request, e.g. "10m" or 0. If None, Ollama's default applies.

    Returns:
        OllamaResult. On HTTP/network failure, success=False, error populated.
    """
    url = f"{base_url.rstrip('/')}/api/generate"
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if format_json:
        payload["format"] = "json"
    if options:
        payload["options"] = options
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive

    start = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        return OllamaResult(
            success=False,
            error=f"{type(e).__name__}: {e}",
            duration_seconds=time.perf_counter() - start,
            model=model,
        )

    duration = time.perf_counter() - start

    return OllamaResult(
        success=True,
        text=data.get("response", ""),
        duration_seconds=duration,
        prompt_eval_count=data.get("prompt_eval_count", 0),
        eval_count=data.get("eval_count", 0),
        eval_duration_ns=data.get("eval_duration", 0),
        model=data.get("model", model),
        raw_json=data,
    )


def ping(base_url: str = DEFAULT_OLLAMA_URL, timeout: float = 5.0) -> bool:
    """Return True if the Ollama server is reachable."""
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{base_url.rstrip('/')}/api/tags")
            return r.status_code == 200
    except httpx.HTTPError:
        return False


def list_models(base_url: str = DEFAULT_OLLAMA_URL, timeout: float = 5.0) -> list[str]:
    """Return names of locally available models, or [] on error."""
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{base_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
    except (httpx.HTTPError, KeyError, ValueError):
        return []
