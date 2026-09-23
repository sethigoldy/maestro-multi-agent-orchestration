"""Pi (Earendil) adapter — RPC mode: JSONL protocol over stdin/stdout.

Verified against the published RPC docs (pi.dev/docs/latest/rpc):

- Start: ``pi --mode rpc [--provider ...] [--model pattern]`` in the workspace.
- Commands are JSON objects, one per line, on stdin; responses carry
  ``type: "response"`` with ``success``/``error``; agent events stream to stdout.
- The prompt command is ``{"type": "prompt", "message": ...}``; a
  ``success: false`` response means the prompt was rejected before acceptance.
- ``agent_settled`` marks the end of the session-level run (no retry, compaction
  retry, or queued continuation remains) — that is our completion signal.
- Usage snapshots arrive on ``message_update`` events (cumulative input/output
  tokens and cost); the last one wins per key.
- Abort is polite: send ``{"type": "abort"}``, wait for idle, then kill.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import BaseAdapter


class PiAdapter(BaseAdapter):
    kind = "pi"
    mode = "rpc"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["pi", "--mode", "rpc"]
        # Settings already carry registry defaults merged with per-task
        # overrides (daemon._turn_settings), so a per-task value wins.
        model = settings.get("model") or (self.spec.model if self.spec is not None else None)
        if model:
            command += ["--model", str(model)]
        return command

    def rpc_start_command(self, prompt: str, task_id: str) -> dict[str, Any]:
        return {"type": "prompt", "message": prompt, "id": f"maestro-{task_id}"}

    def rpc_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        etype = event.get("type")
        if etype == "response":
            if event.get("command") == "prompt" and not event.get("success"):
                return {"fail": f"pi rejected the prompt: {event.get('error') or 'unknown error'}"}
            return None
        if etype == "agent_settled":
            return {"done": True}
        if etype == "message_update":
            usage = event.get("usage")
            if isinstance(usage, dict):
                cost = usage.get("cost")
                cost = cost if isinstance(cost, dict) else {}
                mapped: dict[str, Any] = {}
                token_keys = {"input": "input_tokens", "output": "output_tokens", "cacheRead": "cache_read_tokens", "cacheWrite": "cache_write_tokens"}
                for key, target in token_keys.items():
                    if isinstance(usage.get(key), (int, float)):
                        mapped[target] = usage[key]
                if isinstance(usage.get("totalTokens"), (int, float)):
                    mapped["total_tokens"] = usage["totalTokens"]
                if isinstance(cost.get("total"), (int, float)):
                    mapped["cost_usd"] = cost["total"]
                return {"usage": mapped} if mapped else None
            return None
        if etype == "auto_retry_end" and event.get("finalError"):
            return {"fail": f"pi gave up after automatic retries: {event['finalError']}"}
        return None

    def rpc_abort_command(self) -> dict[str, Any] | None:
        return {"type": "abort"}
