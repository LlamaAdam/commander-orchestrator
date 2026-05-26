"""Read-only orchestrator status snapshot.

`get_status_snapshot()` assembles a structured view of:
  - Ollama reachability + available models
  - Claude quota state (calls, tokens, current block status)
  - Event log summary (total count, last-24h count, by-type breakdown, last event)

`format_status_human(snapshot)` pretty-prints it for terminal display.

No model calls; safe to run frequently. Used by `orch status`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Union


@dataclass
class OllamaStatus:
    reachable: bool
    error: str = ""
    available_models: List[str] = field(default_factory=list)


@dataclass
class QuotaStatus:
    total_calls: int = 0
    total_successes: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    blocked: bool = False
    blocked_until: Optional[float] = None
    seconds_until_unblock: Optional[float] = None


@dataclass
class EventSummary:
    total_events: int = 0
    last_24h_events: int = 0
    by_event: Dict[str, int] = field(default_factory=dict)
    last_event_timestamp: Optional[float] = None
    last_event_type: str = ""


@dataclass
class StatusSnapshot:
    timestamp: float
    project_root: str
    ollama: OllamaStatus
    quota: QuotaStatus
    events: EventSummary
    quota_state_path: str
    events_log_path: str


def _read_quota(path: Path) -> QuotaStatus:
    qs = QuotaStatus()
    if not path.exists():
        return qs
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return qs
    qs.total_calls = int(data.get("total_calls", 0) or 0)
    qs.total_successes = int(data.get("total_successes", 0) or 0)
    qs.total_input_tokens = int(data.get("total_input_tokens", 0) or 0)
    qs.total_output_tokens = int(data.get("total_output_tokens", 0) or 0)
    bu = data.get("blocked_until")
    if bu:
        try:
            qs.blocked_until = float(bu)
            now = time.time()
            if qs.blocked_until > now:
                qs.blocked = True
                qs.seconds_until_unblock = qs.blocked_until - now
        except (TypeError, ValueError):
            pass
    return qs


def _summarize_events(path: Path, window_hours: float = 24.0) -> EventSummary:
    summary = EventSummary()
    if not path.exists():
        return summary
    now = time.time()
    cutoff = now - window_hours * 3600
    last_ts: Optional[float] = None
    last_type = ""
    by_event: Dict[str, int] = {}
    total = 0
    last24 = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                ev_type = str(ev.get("event", "?"))
                by_event[ev_type] = by_event.get(ev_type, 0) + 1
                ts = ev.get("timestamp")
                if isinstance(ts, (int, float)):
                    if ts >= cutoff:
                        last24 += 1
                    if last_ts is None or ts > last_ts:
                        last_ts = float(ts)
                        last_type = ev_type
    except OSError:
        pass
    summary.total_events = total
    summary.last_24h_events = last24
    summary.by_event = by_event
    summary.last_event_timestamp = last_ts
    summary.last_event_type = last_type
    return summary


def _check_ollama(host: str = "http://127.0.0.1:11434", timeout: float = 1.5) -> OllamaStatus:
    """Probe Ollama via the SAME httpx client the orchestrator uses for real
    calls (`local_model`), so status reflects what the fix loop actually sees.

    Previously this used `requests`, which isn't a dependency -- so on every
    install without it the status falsely reported 'reachable: NO (requests
    library not installed)' even while `local_model` (httpx) was successfully
    calling Ollama. Never raises -- status must be safe to run anytime."""
    from . import local_model  # httpx-based; the real runtime client
    try:
        models = local_model.list_models(base_url=host, timeout=timeout)
        if models:
            return OllamaStatus(reachable=True, available_models=models)
        # No models listed -- distinguish "up but empty" from "down".
        if local_model.ping(base_url=host, timeout=timeout):
            return OllamaStatus(reachable=True, available_models=[])
        return OllamaStatus(reachable=False, error=f"not reachable at {host}")
    except Exception as exc:  # defensive: a status probe must never crash
        return OllamaStatus(reachable=False, error=f"{type(exc).__name__}: {exc}")


def get_status_snapshot(
    project_root: Union[Path, str] = ".",
    *,
    quota_state_path: Optional[Path] = None,
    events_log_path: Optional[Path] = None,
    ollama_host: str = "http://127.0.0.1:11434",
) -> StatusSnapshot:
    root = Path(project_root).resolve()
    qs_path = Path(quota_state_path) if quota_state_path else (root / "data" / "quota_state.json")
    ev_path = Path(events_log_path) if events_log_path else (root / "data" / "events.jsonl")
    return StatusSnapshot(
        timestamp=time.time(),
        project_root=str(root),
        ollama=_check_ollama(ollama_host),
        quota=_read_quota(qs_path),
        events=_summarize_events(ev_path),
        quota_state_path=str(qs_path),
        events_log_path=str(ev_path),
    )


def format_status_human(s: StatusSnapshot) -> str:
    out: List[str] = []
    out.append("=" * 68)
    out.append("orchestrator status")
    out.append(f"  time:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.timestamp))}")
    out.append(f"  project: {s.project_root}")
    out.append("=" * 68)
    out.append("")
    out.append("[ollama]")
    if s.ollama.reachable:
        out.append("  reachable: yes")
        models = ", ".join(s.ollama.available_models) if s.ollama.available_models else "(none reported)"
        out.append(f"  models:    {models}")
    else:
        out.append(f"  reachable: NO ({s.ollama.error})")
    out.append("")
    out.append("[claude quota]")
    out.append(f"  total_calls:       {s.quota.total_calls}")
    out.append(f"  total_successes:   {s.quota.total_successes}")
    out.append(f"  total_input_toks:  {s.quota.total_input_tokens}")
    out.append(f"  total_output_toks: {s.quota.total_output_tokens}")
    if s.quota.blocked:
        unblock_at = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(s.quota.blocked_until or 0))
        out.append(f"  status:            BLOCKED until {unblock_at} "
                   f"({int(s.quota.seconds_until_unblock or 0)}s remaining)")
    else:
        out.append("  status:            clear (not rate-limited)")
    out.append("")
    out.append("[events]")
    out.append(f"  log:       {s.events_log_path}")
    out.append(f"  total:     {s.events.total_events}")
    out.append(f"  last 24h:  {s.events.last_24h_events}")
    if s.events.by_event:
        out.append("  by type:")
        for ev_type, count in sorted(s.events.by_event.items(), key=lambda x: -x[1]):
            out.append(f"    {ev_type:24s} {count}")
    if s.events.last_event_timestamp:
        when = time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(s.events.last_event_timestamp))
        out.append(f"  last:      {s.events.last_event_type} @ {when}")
    out.append("")
    return "\n".join(out)
