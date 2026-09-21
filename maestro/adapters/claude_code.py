"""Claude Code adapter (headless ``claude -p`` with stream-json output).

Current Claude Code releases require ``--verbose`` together with
``--output-format stream-json`` in print mode and reject the combination
without it at argument-validation time. The adapter emits ``--verbose`` by
default; if the installed CLI rejects the flag (older surfaces), a single
bounded retry without it self-heals the version skew instead of failing the
task on every daemon retry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..agents import AgentSpec
from .base import AdapterResult, BaseAdapter


def _rejected_verbose(error: str) -> bool:
    """True when the failure is the CLI rejecting ``--verbose`` at arg-parse time."""
    return "unexpected argument '--verbose'" in error


class ClaudeCodeAdapter(BaseAdapter):
    kind = "claude_code"
    mode = "spawn"

    def __init__(self, spec: AgentSpec | None = None) -> None:
        super().__init__(spec)
        # Current CLIs require --verbose with stream-json; dropped only if the
        # installed CLI rejects it (see run()).
        self._verbose = True

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["claude", "-p", "--output-format", "stream-json"]
        if self._verbose:
            command.append("--verbose")
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

    def run(
        self,
        prompt: str,
        workspace: Path,
        task_id: str,
        *,
        settings: dict[str, Any] | None = None,
        timeout: float | None = None,
        log_dir: str | Path | None = None,
        on_line: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AdapterResult:
        result = super().run(
            prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir,
            on_line=on_line, should_cancel=should_cancel,
        )
        if not result.ok and self._verbose and _rejected_verbose(result.error):
            # Version skew: this CLI predates --verbose; retry once without it.
            self._verbose = False
            result = super().run(
                prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir,
                on_line=on_line, should_cancel=should_cancel,
            )
        return result

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
