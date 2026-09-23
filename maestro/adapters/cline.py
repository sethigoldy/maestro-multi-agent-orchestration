"""Cline adapter — spawn mode with ``--json`` structured output.

Verified against the published CLI reference (docs.cline.bot/cli/cli-reference):

- Install: ``npm i -g cline``; one-shot task: ``cline "prompt"`` or a prompt
  piped on stdin (headless activates automatically).
- ``--json`` emits one JSON object per line: ``{"type": "say"|"ask", "text": ...,
  "ts": ..., "say"?: subtype, "partial"?: bool}``.
- A bare positional prompt starts in act mode with auto-approval enabled
  (``--auto-approve`` defaults to true), which is exactly the autonomous
  headless contract Maestro needs: the process never blocks on a permission
  prompt, so ``ask`` lines are transcript content, not questions.
- No usage/cost fields in the JSON schema; Cline billing is provider-side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import BaseAdapter

#: Reasoning levels cline accepts (``--thinking``).
_CLINE_THINKING = ("none", "low", "medium", "high", "xhigh")


class ClineAdapter(BaseAdapter):
    kind = "cline"
    mode = "spawn"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["cline", "--json"]
        # Settings already carry registry defaults merged with per-task
        # overrides (daemon._turn_settings), so a per-task value wins.
        model = settings.get("model") or (self.spec.model if self.spec is not None else None)
        if model:
            command += ["--model", str(model)]
        effort = settings.get("effort") or (self.spec.effort if self.spec is not None else None)
        if effort in _CLINE_THINKING:
            command += ["--thinking", str(effort)]
        return command
