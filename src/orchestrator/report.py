"""`orch report` — summarize the orchestrator's autonomous activity.

Reads the append-only event log + the dedup/graduation state and rolls them
up into the numbers a human actually wants after an unattended run:

  - fix outcomes (fixed / already_fixed / escalated / regressed / capped / ...)
  - tier split: how many fixes the LOCAL model resolved alone (free, fast)
    vs. how many needed the tier-2 Claude fallback
  - triage routing breakdown (handler + via)
  - Claude spend (calls, rate-limits, tokens, reported cost) and local usage
  - verify-then-graduate progress per action-type
  - dedup/cap state + idle-streak high-water mark

Pure + side-effect-free: `build_report` reads files (or accepts pre-loaded
data for testing) and returns a dict; `format_report` renders text. The CLI
layer in cli.py owns argument parsing and printing.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

from .auto_fix import MAX_FAILED_ATTEMPTS, MAX_REGRESSIONS

# Attempt statuses that represent a REAL fix attempt with a pass/fail outcome
# (used as the denominator for the success rate). already_fixed / skipped_* /
# would_apply are excluded — they aren't attempts the model can "succeed" at.
_REAL_OUTCOMES = ("fixed", "escalated", "regressed", "apply_failed", "error")


def load_events(path: Path) -> list[dict]:
    """Parse events.jsonl into a list of dicts. Skips blank/corrupt lines."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _load_json(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_report(
    project_root,
    *,
    events: Optional[list[dict]] = None,
    seen: Optional[dict] = None,
    graduation: Optional[dict] = None,
) -> dict:
    """Roll up events + state into a summary dict. Pass `events`/`seen`/
    `graduation` explicitly to bypass file loads (tests)."""
    data_dir = Path(project_root) / "data"
    if events is None:
        events = load_events(data_dir / "events.jsonl")
    if seen is None:
        seen = _load_json(data_dir / "auto_fix_seen.json")
    if graduation is None:
        graduation = _load_json(data_dir / "graduation_state.json")

    by_type = Counter(e.get("event", "?") for e in events)

    attempts = [e for e in events if e.get("event") == "auto_fix_attempt"]
    status_counts = Counter(a.get("status", "?") for a in attempts)
    real = [a for a in attempts if a.get("status") in _REAL_OUTCOMES]
    fixed = status_counts.get("fixed", 0)
    tier1_fixed = sum(1 for a in attempts
                      if a.get("status") == "fixed" and not a.get("claude_retry_used"))
    tier2_fixed = sum(1 for a in attempts
                      if a.get("status") == "fixed" and a.get("claude_retry_used"))
    claude_retry_attempts = sum(1 for a in attempts if a.get("claude_retry_used"))

    claude_calls = [e for e in events if e.get("event") == "claude_call"]
    local_calls = [e for e in events if e.get("event") == "local_call"]
    triage_evs = [e for e in events if e.get("event") == "triage"]
    idle_evs = [e for e in events if e.get("event") == "idle_streak"]

    def _isum(items, key):
        return sum(int(i.get(key, 0) or 0) for i in items)

    capped = sum(
        1 for v in seen.values()
        if isinstance(v, dict) and (
            int(v.get("attempt_count", 0) or 0) >= MAX_FAILED_ATTEMPTS
            or int(v.get("regressions", 0) or 0) >= MAX_REGRESSIONS
        )
    )

    return {
        "events_total": len(events),
        "by_type": dict(by_type),
        "fixes": {
            "attempts_total": len(attempts),
            "real_attempts": len(real),
            "by_status": dict(status_counts),
            "success_rate": round(fixed / len(real), 3) if real else None,
        },
        "tiers": {
            "tier1_local_fixed": tier1_fixed,
            "tier2_claude_fixed": tier2_fixed,
            "claude_retry_attempts": claude_retry_attempts,
        },
        "triage": {
            "handler": dict(Counter((e.get("decision") or {}).get("handler", "?")
                                    for e in triage_evs)),
            "via": dict(Counter((e.get("decision") or {}).get("via", "?")
                                for e in triage_evs)),
        },
        "claude": {
            "calls": len(claude_calls),
            "successes": sum(1 for e in claude_calls if e.get("success")),
            "rate_limited": sum(1 for e in claude_calls
                                if e.get("error_type") == "rate_limit"),
            "input_tokens": _isum(claude_calls, "input_tokens"),
            "output_tokens": _isum(claude_calls, "output_tokens"),
            "cost_usd_reported": round(
                sum(float(e.get("cost_usd_reported", 0) or 0) for e in claude_calls), 4),
        },
        "local": {
            "calls": len(local_calls),
            "successes": sum(1 for e in local_calls if e.get("success")),
            "output_tokens": _isum(local_calls, "output_tokens"),
        },
        "graduation": {
            k: {"successes": int((v or {}).get("successes", 0) or 0),
                "graduated": bool((v or {}).get("graduated", False))}
            for k, v in graduation.items() if isinstance(v, dict)
        },
        "dedup": {
            "tracked_failures": len(seen),
            "capped": capped,
        },
        "idle": {
            "max_streak": max((int(e.get("streak", 0) or 0) for e in idle_evs), default=0),
        },
    }


def format_report(r: dict) -> str:
    """Render the report dict as readable text."""
    L: list[str] = []
    L.append("=== Orchestrator activity report ===")
    L.append(f"events logged: {r['events_total']}  ({_kv(r['by_type'])})")
    L.append("")

    fx = r["fixes"]
    sr = fx["success_rate"]
    sr_s = f"{sr:.0%}" if sr is not None else "n/a"
    L.append(f"Fixes: {fx['attempts_total']} attempts "
             f"({fx['real_attempts']} real), success rate {sr_s}")
    if fx["by_status"]:
        L.append(f"  by status: {_kv(fx['by_status'])}")

    t = r["tiers"]
    L.append(f"  tier-1 local fixed: {t['tier1_local_fixed']}   "
             f"tier-2 Claude fixed: {t['tier2_claude_fixed']}   "
             f"(claude retry attempts: {t['claude_retry_attempts']})")
    L.append("")

    tr = r["triage"]
    L.append(f"Triage: handler {_kv(tr['handler'])} | via {_kv(tr['via'])}")

    c = r["claude"]
    L.append(f"Claude: {c['calls']} calls, {c['successes']} ok, "
             f"{c['rate_limited']} rate-limited | "
             f"tokens in/out {c['input_tokens']}/{c['output_tokens']} | "
             f"reported ${c['cost_usd_reported']:.4f}")
    lo = r["local"]
    L.append(f"Local:  {lo['calls']} calls, {lo['successes']} ok, "
             f"{lo['output_tokens']} output tokens")
    L.append("")

    grad = r["graduation"]
    if grad:
        parts = [f"{k}={'GRADUATED' if v['graduated'] else v['successes']}"
                 for k, v in grad.items()]
        L.append(f"Graduation: {', '.join(parts)}")
    else:
        L.append("Graduation: (none recorded)")

    d = r["dedup"]
    L.append(f"Dedup: {d['tracked_failures']} failures tracked, {d['capped']} capped")
    L.append(f"Idle: max streak {r['idle']['max_streak']}")
    return "\n".join(L)


def _kv(d: dict) -> str:
    if not d:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in sorted(d.items()))
