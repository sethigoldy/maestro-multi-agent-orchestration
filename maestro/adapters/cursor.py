"""Cursor adapter — spawn mode on ``cursor-agent`` print mode.

Verified against the installed CLI (cursor-agent) and the published docs
(cursor.com/docs/cli/headless, /docs/cli/reference/output-format):

- One-shot headless: ``cursor-agent -p "prompt"`` (print mode; also inferred
  from piped stdin). Has access to all tools including write and shell.
- ``--output-format json`` emits a single JSON object on success —
  ``{"type": "result", "subtype": "success", "is_error": false, "duration_ms",
  "result": "<full assistant text>", "session_id"}`` — with no cost/usage
  fields in the documented schema; live builds additionally carry a ``usage``
  object (inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens), which we
  parse when present. On failure the process exits non-zero and writes to stderr.
- ``--yolo`` (alias of ``--force``) force-allows commands so headless runs are
  autonomous; ``--trust`` accepts the workspace without a prompt. Sensitive
  handoffs are gated before any agent runs, so both are safe defaults here.
- Auth is probed with ``cursor-agent status`` (view authentication status).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import BaseAdapter


class CursorAdapter(BaseAdapter):
    kind = "cursor"
    mode = "spawn"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["cursor-agent", "-p", "--output-format", "json", "--yolo", "--trust"]
        # Settings already carry registry defaults merged with per-task
        # overrides (daemon._turn_settings), so a per-task value wins.
        model = settings.get("model") or (self.spec.model if self.spec is not None else None)
        if model:
            command += ["--model", str(model)]
        return command

    def parse_line(self, line: str) -> dict[str, Any] | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            obj = json.loads(stripped)
        except ValueError:
            return None
        if not isinstance(obj, dict) or obj.get("type") != "result":
            return None
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            return None
        mapped: dict[str, Any] = {}
        for key, target in {
            "inputTokens": "input_tokens",
            "outputTokens": "output_tokens",
            "cacheReadTokens": "cache_read_tokens",
            "cacheWriteTokens": "cache_write_tokens",
        }.items():
            if isinstance(usage.get(key), (int, float)):
                mapped[target] = usage[key]
        return {"usage": mapped} if mapped else None

    def auth_probe(self) -> list[str] | None:
        binary = self.binary()
        return [binary, "status"] if binary else None
