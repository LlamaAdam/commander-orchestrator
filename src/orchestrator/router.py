"""Top-level router: take a task description, triage it, dispatch, log everything.

Flow:
    1. triage.triage(task) decides handler ("local" | "claude").
    2. If "claude", first check quota — if blocked, return immediately without
       hitting the CLI (caller is responsible for queueing).
    3. Dispatch: local_model.generate(...) or claude_cli.run_claude(...).
    4. Log every step to data/events.jsonl as append-only newline-delimited JSON.

Events written to events.jsonl include: triage decisions (rule or Llama),
quota-block events, dispatch outcomes. This is the audit trail the user spec
called out — every Claude invocation gets a reason recorded next to it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import claude_cli, local_model, quota, triage


@dataclass
class TaskResult:
    """End-to-end outcome of routing one task."""

    success: bool
    handler: str  # "local" | "claude"
    text: str = ""
    error: str = ""
    error_type: str = ""  # populated for claude failures
    triage_decision: dict | None = None
    duration_seconds: float = 0.0
    blocked: bool = False  # True if Claude was needed but quota gated
    seconds_until_unblock: float = 0.0


class Router:
    def __init__(
        self,
        *,
        events_path: str | Path = "data/events.jsonl",
        quota_path: str | Path = "data/quota_state.json",
        local_model_name: str = local_model.DEFAULT_MODEL,
        ollama_base_url: str = local_model.DEFAULT_OLLAMA_URL,
        claude_model: str | None = None,
    ):
        self.events_path = Path(events_path)
        self.local_model_name = local_model_name
        self.ollama_base_url = ollama_base_url
        self.claude_model = claude_model
        self.quota = quota.QuotaTracker(path=quota_path)

    # ---- logging ---------------------------------------------------------

    def _log(self, event: dict) -> None:
        """Append one JSON event to events.jsonl atomically-enough."""
        event.setdefault("timestamp", time.time())
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, default=str)
        # Append is atomic for short writes on Windows/Linux when opened in 'a' mode.
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---- main entry ------------------------------------------------------

    def handle(self, task: str) -> TaskResult:
        start = time.perf_counter()

        # 1. Triage
        decision = triage.triage(
            task,
            model=self.local_model_name,
            base_url=self.ollama_base_url,
        )
        decision_dict = triage.decision_to_log_dict(decision)
        self._log(
            {
                "event": "triage",
                "task_preview": task[:300],
                "decision": decision_dict,
            }
        )

        # 2. Dispatch
        if decision.handler == "claude":
            return self._dispatch_claude(task, decision_dict, start)
        return self._dispatch_local(task, decision_dict, start)

    def handle_claude_only(self, task: str, *, reason: str = "forced-fallback") -> TaskResult:
        """Force-dispatch to Claude, bypassing triage.

        Used by tier-2 retry in auto_fix: when the local model's proposed fix
        failed to apply or didn't reduce failures, we want to ask Claude
        directly without re-running the classifier. Still respects the quota
        gate. The synthesized triage decision is logged so the audit trail
        shows this call wasn't routed through normal triage.
        """
        start = time.perf_counter()
        decision_dict = {
            "handler": "claude",
            "reason": reason,
            "via": "forced-fallback",
        }
        self._log(
            {
                "event": "triage",
                "task_preview": task[:300],
                "decision": decision_dict,
            }
        )
        return self._dispatch_claude(task, decision_dict, start)

    # ---- dispatchers -----------------------------------------------------

    def _dispatch_claude(self, task: str, decision_dict: dict, start: float) -> TaskResult:
        blocked, secs_left = self.quota.is_blocked()
        if blocked:
            self._log(
                {
                    "event": "claude_blocked",
                    "seconds_until_unblock": secs_left,
                    "reason": self.quota.state.last_block_reason[:200],
                }
            )
            return TaskResult(
                success=False,
                handler="claude",
                error=(
                    f"Claude is rate-limited; retry in ~{int(secs_left)}s "
                    f"(reset at {self.quota.summary().get('blocked_until_iso')})."
                ),
                triage_decision=decision_dict,
                duration_seconds=time.perf_counter() - start,
                blocked=True,
                seconds_until_unblock=secs_left,
            )

        result = claude_cli.run_claude(task, model=self.claude_model)
        self.quota.record(result)

        self._log(
            {
                "event": "claude_call",
                "success": result.success,
                "error_type": result.error_type,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "duration_seconds": round(result.duration_seconds, 3),
                "cost_usd_reported": result.total_cost_usd,
            }
        )

        return TaskResult(
            success=result.success,
            handler="claude",
            text=result.text,
            error=result.error,
            error_type=result.error_type,
            triage_decision=decision_dict,
            duration_seconds=time.perf_counter() - start,
        )

    def _dispatch_local(self, task: str, decision_dict: dict, start: float) -> TaskResult:
        result = local_model.generate(
            task,
            model=self.local_model_name,
            base_url=self.ollama_base_url,
        )

        self._log(
            {
                "event": "local_call",
                "success": result.success,
                "prompt_tokens": result.prompt_eval_count,
                "output_tokens": result.eval_count,
                "duration_seconds": round(result.duration_seconds, 3),
            }
        )

        return TaskResult(
            success=result.success,
            handler="local",
            text=result.text,
            error=result.error,
            triage_decision=decision_dict,
            duration_seconds=time.perf_counter() - start,
        )
