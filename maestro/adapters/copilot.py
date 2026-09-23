"""Copilot adapter — spawn mode on the GitHub Copilot CLI.

Verified live against the installed CLI (``copilot --help`` + a real
non-interactive run):

- One-shot headless: ``copilot -p "prompt" --output-format json`` prints
  JSONL session events and exits after completion; the final line is
  ``{"type": "result", "exitCode": N, "usage": {...}}``.
- ``--yolo`` (== --allow-all-tools --allow-all-paths --allow-all-urls) makes
  headless runs autonomous; sensitive handoffs are gated before any agent
  runs, so it is a safe default here.
- ``-C <dir>`` changes the working directory before anything else loads;
  Maestro also sets the spawn cwd, so both agree.
- ``--usage-output-file <file>`` writes the final usage statistics as JSON
  after completion (token counts per category, premium request cost, model
  metrics). The file is read back after the run and merged into the result;
  it carries no explicit USD field, so Maestro records the raw counters.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import BaseAdapter


def _usage_file_for(task_id: str) -> Path:
    """Deterministic per-task location for the --usage-output-file payload."""
    return Path(tempfile.gettempdir()) / f"maestro-copilot-usage-{task_id}.json"


class CopilotAdapter(BaseAdapter):
    kind = "copilot"
    mode = "spawn"

    def input_mode(self) -> str:
        # The prompt is a command argument (-p), not stdin.
        return "arg"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = [self.binary() or "copilot", "-p", prompt, "--output-format", "json", "--yolo", "-C", str(workspace), "--usage-output-file", str(_usage_file_for(task_id))]
        # Settings already carry registry defaults merged with per-task
        # overrides (daemon._turn_settings), so a per-task value wins.
        model = settings.get("model") or (self.spec.model if self.spec is not None else None)
        if model:
            command += ["--model", str(model)]
        return command

    def parse_line(self, line: str) -> dict[str, Any] | None:
        """Extract usage from the final ``result`` line (the rich payload comes
        from the usage output file, read in :meth:`run`)."""
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
        for key in ("premiumRequests", "totalApiDurationMs", "sessionDurationMs"):
            if isinstance(usage.get(key), (int, float)):
                mapped[key] = usage[key]
        return {"usage": mapped} if mapped else None

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
    ) -> "Any":
        result = super().run(
            prompt,
            workspace,
            task_id,
            settings=settings,
            timeout=timeout,
            log_dir=log_dir,
            on_line=on_line,
            should_cancel=should_cancel,
        )
        usage_file = _usage_file_for(task_id)
        if usage_file.exists():
            try:
                data = json.loads(usage_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            finally:
                try:
                    usage_file.unlink()
                except OSError:
                    pass
            if isinstance(data, dict):
                merged = {**(result.usage or {}), **_map_usage_file(data)}
                result.usage = merged or None
        return result


def _map_usage_file(data: dict[str, Any]) -> dict[str, Any]:
    """Map the usage-output-file schema onto Maestro's flat usage counters."""
    mapped: dict[str, Any] = {}
    details = data.get("tokenDetails")
    if isinstance(details, dict):
        for key, target in {
            "input": "input_tokens",
            "output": "output_tokens",
            "cache_read": "cache_read_tokens",
            "cache_write": "cache_write_tokens",
        }.items():
            bucket = details.get(key)
            if isinstance(bucket, dict) and isinstance(bucket.get("tokenCount"), (int, float)):
                mapped[target] = bucket["tokenCount"]
    # modelMetrics may carry per-model token counts when tokenDetails is absent
    metrics = data.get("modelMetrics")
    if isinstance(metrics, dict):
        for model in metrics.values():
            if not isinstance(model, dict):
                continue
            usage = model.get("usage")
            if not isinstance(usage, dict):
                continue
            for key, target in {
                "inputTokens": "input_tokens",
                "outputTokens": "output_tokens",
                "cacheReadTokens": "cache_read_tokens",
                "cacheWriteTokens": "cache_write_tokens",
                "reasoningTokens": "reasoning_tokens",
            }.items():
                if isinstance(usage.get(key), (int, float)) and target not in mapped:
                    mapped[target] = usage[key]
    for key in ("totalPremiumRequestCost", "totalUserRequests", "totalNanoAiu", "totalApiDurationMs"):
        if isinstance(data.get(key), (int, float)):
            mapped[key] = data[key]
    return mapped
