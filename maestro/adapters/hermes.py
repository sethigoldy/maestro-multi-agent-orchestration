"""Hermes Agent (Nous Research) adapter — spawn mode, one-shot ``-z``.

Verified against the installed CLI (Hermes Agent v0.20.x):

- One-shot: ``hermes -z PROMPT`` prints ONLY the final response text to stdout
  (no banner, spinner, or session line); tools, memory, rules and AGENTS.md in
  the CWD load as normal.
- ``--usage-file PATH`` writes a JSON usage report after the run — even when it
  fails — with ``estimated_cost_usd``, token counts, model/provider, and a
  ``failed`` flag, so spend is always accounted for. The CLI exits 0 on
  API-level failures (e.g. ``HTTP 401: Access denied due to missing
  subscription key``), so the report's ``failed``/``failure`` fields are the
  authoritative failure signal; this adapter honors them and flips the result
  to a proper failure instead of reporting COMPLETED for a run that did nothing.
- ``--yolo`` bypasses dangerous-command approval prompts; autonomous headless
  delegation needs it (sensitive handoffs are gated before any agent runs).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import AdapterResult, BaseAdapter


def _map_usage_report(report: dict[str, Any]) -> dict[str, Any]:
    """Map the hermes usage-report schema onto Maestro's usage keys."""
    mapped: dict[str, Any] = {}
    if isinstance(report.get("estimated_cost_usd"), (int, float)):
        mapped["cost_usd"] = report["estimated_cost_usd"]
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "total_tokens"):
        if isinstance(report.get(key), (int, float)):
            mapped[key] = report[key]
    if isinstance(report.get("model"), str):
        mapped["model"] = report["model"]
    return mapped


def _failure_detail(report: dict[str, Any]) -> str:
    """Human-readable suffix for a failed-run error, from the report's failure field."""
    failure = report.get("failure")
    if isinstance(failure, str) and failure.strip():
        return f": {failure.strip()}"
    return ""


class HermesAdapter(BaseAdapter):
    kind = "hermes"
    mode = "spawn"

    def input_mode(self) -> str:
        # The prompt rides on argv (-z); stdin stays closed.
        return "arg"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["hermes", "-z", prompt]
        log_dir = settings.get("maestro_log_dir")
        if log_dir:
            command += ["--usage-file", str(Path(log_dir) / f"usage-{task_id}.json")]
        # Settings already carry registry defaults merged with per-task
        # overrides (daemon._turn_settings), so a per-task value wins.
        model = settings.get("model") or (self.spec.model if self.spec is not None else None)
        if model:
            command += ["-m", str(model)]
        effort = settings.get("effort") or (self.spec.effort if self.spec is not None else None)
        if effort:
            command += ["--reasoning", str(effort)]
        command.append("--yolo")
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
        result = super().run(
            prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir,
            on_line=on_line, should_cancel=should_cancel,
        )
        if log_dir is not None:
            usage_file = Path(log_dir) / f"usage-{task_id}.json"
            if usage_file.exists():
                try:
                    report = json.loads(usage_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report = None
                if isinstance(report, dict):
                    mapped = _map_usage_report(report)
                    if mapped:
                        result.usage = {**(result.usage or {}), **mapped}
                    # The CLI exits 0 on API-level failures (e.g. a missing
                    # subscription key), so the report is the authoritative
                    # failure signal; flip an otherwise-ok result to failed.
                    if report.get("failed") is True:
                        detail = _failure_detail(report)
                        result.ok = False
                        result.error = f"Agent {self.kind!r} reported a failed run{detail}"
        return result
