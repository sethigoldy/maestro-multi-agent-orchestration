"""OpenHands adapter — spawn mode on the V1 CLI headless runner.

Verified against the published V1 docs (docs.openhands.dev, usage/cli/headless
and usage/cli/command-reference):

- One-shot headless: ``openhands --headless -t "task"`` (or ``-f file``) —
  always runs in always-approve mode; ``--json`` streams JSONL agent events.
- Exit codes are explicit: 0 success, 1 error/task failed, 2 invalid arguments.
- The LLM is configured by the user's OpenHands settings
  (``~/.openhands/agent_settings.json`` or ``LLM_*`` env vars); there is no
  per-invocation model flag, so ``spec.model`` is intentionally not forwarded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import AdapterResult, BaseAdapter


class OpenHandsAdapter(BaseAdapter):
    kind = "openhands"
    mode = "spawn"

    def input_mode(self) -> str:
        # The prompt rides on argv (-t) or a task file (-f); stdin stays closed.
        return "arg"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["openhands", "--headless", "--json", "--exit-without-confirmation"]
        log_dir = settings.get("maestro_log_dir")
        if log_dir:
            # -f avoids ARG_MAX limits for large handoffs; the file is written
            # by run() before launch and kept in the task dir as an artifact.
            command += ["-f", str(Path(log_dir) / f"prompt-{task_id}.txt")]
        else:
            command += ["-t", prompt]
        return command

    def run(
        self,
        prompt: str,
        workspace: Path,
        task_id: str,
        *,
        settings: dict[str, Any] | None = None,
        timeout: float | None = None,
        log_dir: str | Path | None = None,
        on_line=None,
        should_cancel=None,
    ) -> AdapterResult:
        if log_dir is not None:
            task_file = Path(log_dir) / f"prompt-{task_id}.txt"
            task_file.parent.mkdir(parents=True, exist_ok=True)
            task_file.write_text(prompt, encoding="utf-8")
        return super().run(
            prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir,
            on_line=on_line, should_cancel=should_cancel,
        )
