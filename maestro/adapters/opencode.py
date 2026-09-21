"""OpenCode (sst) adapter — spawn mode, one-shot ``opencode run``.

Verified against the installed CLI (opencode 1.18.x):

- One-shot headless: ``opencode run [message..]`` runs a single non-interactive
  turn in the CWD (the daemon spawns with cwd=workspace) and exits when done.
- ``--format json`` emits one JSON event per line: ``step_start``, ``text``
  parts, and a terminal ``step_finish`` whose ``part`` carries ``tokens``
  (total/input/output/reasoning/cache.read/cache.write) and ``cost`` — the
  usage source for this adapter.
- API-level failures emit a final ``{"type": "error", ...}`` event and exit
  non-zero, so the base spawn mode's exit-code check is sufficient; no
  usage-report post-processing is needed (unlike hermes).
- ``--auto`` auto-approves permission prompts that would otherwise block a
  headless run (dangerous by design — sensitive handoffs are gated before any
  agent runs, same as the other adapters' bypass flags).
- Model selection: ``-m provider/model``; reasoning effort maps to
  ``--variant`` (provider-specific effort such as high/max/minimal).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import BaseAdapter


class OpenCodeAdapter(BaseAdapter):
    kind = "opencode"
    mode = "spawn"

    def input_mode(self) -> str:
        # The prompt rides on argv (positional message); stdin stays closed.
        return "arg"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["opencode", "run", "--format", "json", "--auto"]
        # Settings already carry registry defaults merged with per-task
        # overrides (daemon._turn_settings), so settings win by construction.
        model = settings.get("model") or (self.spec.model if self.spec is not None else None)
        if model:
            command += ["-m", str(model)]
        effort = settings.get("effort") or (self.spec.effort if self.spec is not None else None)
        if effort:
            command += ["--variant", str(effort)]
        command.append(prompt)
        return command

    def parse_line(self, line: str) -> dict[str, Any] | None:
        stripped = line.strip()
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            return None
        part = event.get("part")
        if not isinstance(part, dict):
            return None
        usage: dict[str, Any] = {}
        cost = part.get("cost")
        if isinstance(cost, (int, float)):
            usage["cost_usd"] = float(cost)
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            for key in ("input", "output", "reasoning", "total"):
                value = tokens.get(key)
                if isinstance(value, (int, float)):
                    usage[f"{key}_tokens"] = int(value)
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                for key in ("read", "write"):
                    value = cache.get(key)
                    if isinstance(value, (int, float)):
                        usage[f"cache_{key}_tokens"] = int(value)
        return {"usage": usage} if usage else None
