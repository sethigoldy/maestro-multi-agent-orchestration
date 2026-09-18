"""Claude Code adapter (headless ``claude -p`` with stream-json output)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import BaseAdapter


class ClaudeCodeAdapter(BaseAdapter):
    kind = "claude_code"
    mode = "spawn"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["claude", "-p", "--output-format", "stream-json"]
        model = settings.get("model") or (self.spec.model if self.spec else None)
        if model:
            command.extend(["--model", str(model)])
        # Reserved key from the daemon's context injection (see maestro/context.py):
        # standing context rides in the system prompt, staged skills are discovered
        # from <skills_root>/.claude/skills/<label>/.
        context = settings.get("maestro_context")
        if isinstance(context, dict):
            system_file = context.get("system_file")
            if system_file and Path(system_file).is_file():
                command.extend(["--append-system-prompt-file", str(system_file)])
            skills_root = context.get("skills_root")
            if skills_root and Path(skills_root).is_dir():
                command.extend(["--add-dir", str(skills_root)])
        return command  # prompt arrives on stdin

    def parse_line(self, line: str) -> dict[str, Any] | None:
        stripped = line.strip()
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        if event.get("type") == "result":
            usage: dict[str, Any] = {}
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                usage["cost_usd"] = float(cost)
            duration_ms = event.get("duration_ms")
            if isinstance(duration_ms, (int, float)):
                usage["duration_ms"] = int(duration_ms)
            return {"usage": usage} if usage else None
        return None
