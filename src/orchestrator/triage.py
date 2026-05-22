"""Triage: decide whether a task should be handled by the local model or Claude.

Two-stage decision:
    1. Rule-based pre-filter — fast string/regex matches catch obvious cases.
       If a rule fires, we skip Llama entirely.
    2. Llama JSON-mode classifier — for anything the rules can't cleanly
       categorize. The classifier returns a small structured object.

Key design choice (from earlier discussion):
    The classifier emits a CLASSIFICATION ONLY. It does NOT rewrite the prompt.
    The user's original task is forwarded verbatim to whichever handler is
    chosen. This prevents Llama from hallucinating context or dropping
    important specifics on the way to Claude.

Safe-default policy:
    If anything goes wrong with the classifier (network error, malformed JSON,
    unknown handler value), the decision falls back to "claude". Better to
    burn a Claude call than silently route a hard task to Llama.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

from . import local_model

Handler = Literal["local", "claude"]
Via = Literal["rule", "llama", "fallback"]
Complexity = Literal["trivial", "small", "medium", "large"]


@dataclass
class TriageDecision:
    handler: Handler
    reason: str
    complexity: Complexity | None = None
    estimated_files: int | None = None
    via: Via = "llama"
    rule_name: str | None = None
    raw_classifier_output: str | None = None
    duration_seconds: float = 0.0


# ---------- Rule-based pre-filter -------------------------------------------

# Trivial / mechanical tasks → local. Patterns are matched against lowercased text.
_TRIVIAL_RULES: tuple[tuple[str, str], ...] = (
    (r"\brename\s+\w+\s+to\s+\w+\b", "rename-symbol"),
    (r"\bfix(?:\s+a)?\s+typo\b", "fix-typo"),
    (r"\b(reformat|run\s+black|run\s+ruff|run\s+prettier)\b", "format"),
    (r"\bbump\s+(?:the\s+)?version\b", "bump-version"),
    (r"\bremove\s+trailing\s+whitespace\b", "remove-whitespace"),
    (r"\badd\s+a?\s*docstring\s+to\s+\w+\b", "add-docstring-one-symbol"),
    # Short questions ending with `?` — must start with a question lead-in to
    # avoid catching long imperatives that happen to end with `?`. The middle
    # can contain non-word chars like `()` so things like "what is sorted() in
    # Python?" match. Cap the middle at ~150 chars so we don't catch essays.
    (r"^\s*(what\s+is|what\s+does|how\s+do(?:\s+i)?|explain)\b.{0,150}\?\s*$", "short-question"),
)

# Tasks that clearly need Claude → skip local triage entirely.
_CLAUDE_RULES: tuple[tuple[str, str], ...] = (
    (r"\b(architecture|architectural)\b", "architecture"),
    (r"\brefactor\b.{0,40}\bacross\b", "refactor-across-files"),
    (r"\bmulti[-\s]?file\b", "multi-file"),
    (r"\bdesign\s+(a|the)\b", "design-task"),
    (r"\binvestig(ate|ation)\b.{0,40}\b(bug|issue|failure)\b", "investigate-bug"),
    (r"\b(implement|build|add)\s+(?:a|the)\s+\w+(\s+\w+){0,4}\s+(feature|module|system)\b", "implement-feature"),
)


def _rule_filter(task: str) -> TriageDecision | None:
    """Return a TriageDecision if a rule fires, else None."""
    lower = task.lower()

    for pattern, name in _TRIVIAL_RULES:
        if re.search(pattern, lower):
            return TriageDecision(
                handler="local",
                reason=f"Matched trivial rule: {name}",
                via="rule",
                rule_name=name,
                complexity="trivial",
            )

    for pattern, name in _CLAUDE_RULES:
        if re.search(pattern, lower):
            return TriageDecision(
                handler="claude",
                reason=f"Matched Claude rule: {name}",
                via="rule",
                rule_name=name,
            )

    return None


# ---------- Llama classifier ------------------------------------------------

_TRIAGE_PROMPT_TEMPLATE = """You are a triage classifier deciding which model should handle a coding task.

Choose "local" for a small local model (qwen2.5-coder:14b). Use "local" when the task is one of:
- A single-file, mechanical edit (rename, format, fix typo, add docstring, bump version)
- A self-contained question about syntax, an API, or how to do one specific thing
- A small, isolated function implementation under ~30 lines, with clear inputs and outputs
- Generating boilerplate from a clear spec

Choose "claude" for a powerful remote model (Claude Sonnet). Use "claude" when the task is one of:
- Spans or affects multiple files
- Requires understanding existing code before editing
- Involves debugging where the cause is unknown
- Requires design judgment, architectural decisions, or trade-off analysis
- Is novel or has ambiguous requirements
- Asks for a feature larger than a single function

Be conservative. When in doubt between local and claude, choose claude.

TASK:
{task}

Respond with ONLY a JSON object on a single line. Schema:
{{"handler": "local" | "claude", "reason": "one short sentence", "complexity": "trivial" | "small" | "medium" | "large", "estimated_files": integer}}
"""


def _fallback_decision(reason: str, raw: str | None = None) -> TriageDecision:
    return TriageDecision(
        handler="claude",
        reason=reason,
        via="fallback",
        raw_classifier_output=raw,
    )


def _classify_with_llama(
    task: str,
    *,
    model: str,
    base_url: str,
    timeout: float,
) -> TriageDecision:
    prompt = _TRIAGE_PROMPT_TEMPLATE.format(task=task)
    start = time.perf_counter()
    result = local_model.generate(
        prompt,
        model=model,
        base_url=base_url,
        format_json=True,
        options={"temperature": 0.0, "num_predict": 200},
        timeout=timeout,
    )
    duration = time.perf_counter() - start

    if not result.success:
        d = _fallback_decision(
            reason=f"Llama call failed: {result.error}. Defaulting to claude.",
        )
        d.duration_seconds = duration
        return d

    raw = (result.text or "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        d = _fallback_decision(
            reason=f"Llama returned non-JSON ({e}). Defaulting to claude.",
            raw=raw,
        )
        d.duration_seconds = duration
        return d

    if not isinstance(parsed, dict):
        d = _fallback_decision(
            reason="Llama returned non-object JSON. Defaulting to claude.",
            raw=raw,
        )
        d.duration_seconds = duration
        return d

    handler = parsed.get("handler")
    if handler not in ("local", "claude"):
        d = _fallback_decision(
            reason=f"Llama returned unknown handler {handler!r}. Defaulting to claude.",
            raw=raw,
        )
        d.duration_seconds = duration
        return d

    complexity = parsed.get("complexity")
    if complexity not in (None, "trivial", "small", "medium", "large"):
        complexity = None

    estimated_files = parsed.get("estimated_files")
    if not isinstance(estimated_files, int):
        estimated_files = None

    return TriageDecision(
        handler=handler,  # type: ignore[arg-type]
        reason=str(parsed.get("reason") or "")[:200],
        complexity=complexity,  # type: ignore[arg-type]
        estimated_files=estimated_files,
        via="llama",
        raw_classifier_output=raw,
        duration_seconds=duration,
    )


def triage(
    task: str,
    *,
    model: str = local_model.DEFAULT_MODEL,
    base_url: str = local_model.DEFAULT_OLLAMA_URL,
    timeout: float = 60.0,
) -> TriageDecision:
    """Decide whether `task` should be handled by the local model or Claude.

    Tries the rule pre-filter first. If no rule fires, calls Llama with a
    JSON-constrained prompt. On any Llama failure, falls back to claude.
    """
    rule_decision = _rule_filter(task)
    if rule_decision is not None:
        return rule_decision

    return _classify_with_llama(task, model=model, base_url=base_url, timeout=timeout)


def decision_to_log_dict(decision: TriageDecision) -> dict:
    """Convenience for logging — flattens to a JSON-friendly dict."""
    return asdict(decision)
