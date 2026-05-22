"""Periodic Claude self-review of orchestrator routing decisions.

`generate_health_report()` reads recent events, calls Claude (Haiku by default
for low cost — Haiku is plenty for this kind of light review), and writes a
Markdown report to `data/latest_health.md`.

The intent: spot routing drift over time without manually reading the JSONL.
The report flags:
  - tasks routed to Claude that local could have handled (over-routing)
  - tasks routed to local that look hard (under-routing)
  - repeated failures on either handler
  - quota burn rate concerns

NOTE on claude_cli interface: this module accesses fields on the run_claude
return value via `_get()`, which works whether run_claude returns a dict or a
dataclass. The fields expected:
  - success            (bool)
  - text               (str — the model response)
  - input_tokens       (int)
  - output_tokens      (int)
  - cost_usd_reported  (float)
  - error_type         (str)
  - blocked            (bool, optional)
If the actual interface differs slightly, _get's defaults will paper over it
and the report will still be written (just with zero counts for unknown fields).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Union

from . import claude_cli


DEFAULT_MAX_EVENTS = 100


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a dict or attribute on a dataclass-like object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _read_recent_events(path: Path, max_events: int) -> List[dict]:
    if not path.exists():
        return []
    raw_lines: List[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    raw_lines.append(line)
    except OSError:
        return []
    tail = raw_lines[-max_events:]
    parsed: List[dict] = []
    for line in tail:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed


def _build_prompt(events: List[dict], quota_summary: dict) -> str:
    parts = [
        "You are reviewing recent routing decisions made by a local orchestrator "
        "that splits coding tasks between a small local LLM (qwen2.5-coder:14b, "
        "fast and free) and Claude (smarter but rate-limited and metered).",
        "",
        "Your job: skim the events and flag drift. Look specifically for:",
        "  1. Tasks routed to Claude that local could plausibly have handled (over-routing).",
        "  2. Tasks routed to local that look hard (under-routing).",
        "  3. Failure patterns -- the same task type failing repeatedly on one handler.",
        "  4. Quota burn rate concerns -- Claude usage trending up unsustainably.",
        "",
        "Write a SHORT Markdown report (under 500 words). Sections:",
        "  - **Summary** -- 1-2 sentence overall verdict.",
        "  - **Concerns** (only if any exist) -- specific findings with task previews and reasoning.",
        "  - **Suggested tuning** (only if concrete) -- rule additions/removals, classifier hints.",
        "",
        "If nothing notable, say so in one sentence.",
        "",
        "---",
        "",
        "## Quota snapshot",
        "```json",
        json.dumps(quota_summary, indent=2),
        "```",
        "",
        f"## Recent events ({len(events)}, oldest first)",
        "```jsonl",
    ]
    for ev in events:
        parts.append(json.dumps(ev))
    parts.extend([
        "```",
        "",
        "Begin your Markdown report now (do not preface with 'Here is...'):",
    ])
    return "\n".join(parts)


@dataclass
class HealthReport:
    success: bool
    markdown: str
    error: str = ""
    n_events_reviewed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    model_used: str = ""
    written_to: Optional[str] = None


def generate_health_report(
    project_root: Union[Path, str] = ".",
    *,
    max_events: int = DEFAULT_MAX_EVENTS,
    model: str = "haiku",
    write_to: Optional[Path] = None,
    quota_state_path: Optional[Path] = None,
    events_log_path: Optional[Path] = None,
    dry_run: bool = False,
) -> HealthReport:
    """Run a Claude self-review and write data/latest_health.md.

    dry_run=True builds the prompt and verifies all I/O paths but does NOT call
    Claude; useful for testing the wiring without burning a call.
    """
    root = Path(project_root).resolve()
    qs_path = Path(quota_state_path) if quota_state_path else (root / "data" / "quota_state.json")
    ev_path = Path(events_log_path) if events_log_path else (root / "data" / "events.jsonl")

    events = _read_recent_events(ev_path, max_events)
    quota_summary: dict = {}
    if qs_path.exists():
        try:
            quota_summary = json.loads(qs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    prompt = _build_prompt(events, quota_summary)

    if dry_run:
        synthetic_md = (
            "# Orchestrator health report (DRY RUN)\n\n"
            f"- prompt chars: {len(prompt)}\n"
            f"- events reviewed: {len(events)}\n"
            f"- model that would be called: `{model}`\n\n"
            "_No Claude call was made; this is a dry-run sanity check._\n"
        )
        return HealthReport(
            success=True,
            markdown=synthetic_md,
            n_events_reviewed=len(events),
            model_used=model,
            written_to=None,
        )

    t0 = time.monotonic()
    result = claude_cli.run_claude(prompt, model=model)
    duration = time.monotonic() - t0

    if not _get(result, "success", False):
        return HealthReport(
            success=False,
            markdown="",
            error=str(_get(result, "error_type", "claude call failed")),
            n_events_reviewed=len(events),
            duration_seconds=round(duration, 3),
            model_used=model,
        )

    body = str(_get(result, "text", "")).strip()
    cost = float(_get(result, "cost_usd_reported", 0.0) or 0.0)
    in_toks = int(_get(result, "input_tokens", 0) or 0)
    out_toks = int(_get(result, "output_tokens", 0) or 0)

    header = (
        f"# Orchestrator health report\n\n"
        f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- model: `{model}`\n"
        f"- events reviewed: {len(events)}\n"
        f"- input/output tokens: {in_toks} / {out_toks}\n"
        f"- cost: ${cost:.4f}\n"
        f"- duration: {duration:.1f}s\n\n"
        f"---\n\n"
    )
    full_markdown = header + body

    if write_to is None:
        write_to = root / "data" / "latest_health.md"
    write_to = Path(write_to)
    try:
        write_to.parent.mkdir(parents=True, exist_ok=True)
        write_to.write_text(full_markdown, encoding="utf-8")
        written_path = str(write_to)
    except OSError as exc:
        return HealthReport(
            success=False,
            markdown=full_markdown,
            error=f"failed to write report: {exc}",
            n_events_reviewed=len(events),
            input_tokens=in_toks,
            output_tokens=out_toks,
            cost_usd=cost,
            duration_seconds=round(duration, 3),
            model_used=model,
        )

    return HealthReport(
        success=True,
        markdown=full_markdown,
        n_events_reviewed=len(events),
        input_tokens=in_toks,
        output_tokens=out_toks,
        cost_usd=cost,
        duration_seconds=round(duration, 3),
        model_used=model,
        written_to=written_path,
    )
