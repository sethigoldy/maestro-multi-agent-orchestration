"""Codex CLI adapter (generalizes the 0.8.x worker command)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import BaseAdapter


class CodexAdapter(BaseAdapter):
    kind = "codex"
    mode = "spawn"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["codex", "exec", "--full-auto"]
        model = settings.get("model") or (self.spec.model if self.spec else None)
        effort = settings.get("effort") or (self.spec.effort if self.spec else None)
        if model:
            command.extend(["--model", str(model)])
        if effort:
            command.extend(["--config", f'model_reasoning_effort="{effort}"'])
        command.append("-")  # prompt arrives on stdin
        return command

    def parse_line(self, line: str) -> dict[str, Any] | None:
        stripped = line.strip()
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        usage: dict[str, Any] = {}
        for key in ("total_cost_usd", "cost_usd"):
            value = event.get(key)
            if isinstance(value, (int, float)):
                usage["cost_usd"] = float(value)
                break
        tokens = event.get("tokens_used") or event.get("usage")
        if isinstance(tokens, (int, float)):
            usage["tokens"] = int(tokens)
        elif isinstance(tokens, dict):
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                if isinstance(tokens.get(k), (int, float)):
                    usage[f"{k}"] = int(tokens[k])
        return {"usage": usage} if usage else None
