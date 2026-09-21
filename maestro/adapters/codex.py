"""Codex CLI adapter (generalizes the 0.8.x worker command).

The autonomous-execution flags moved across codex releases: older CLIs take
``--full-auto``; current ones (0.15x) take ``--approve-for-me`` (which implies
the workspace-write sandbox) and reject ``--full-auto`` outright. The adapter
probes ``codex exec --help`` once per instance and picks the flag set the
installed CLI actually accepts, so both old and new installs work unmodified.

The probe is a first guess only: when the installed CLI accepts neither flag
(or the probe itself fails), the run dies at argument-parse time with clap's
``error: unexpected argument ... found``. In that case the adapter retries
once with the *other* autonomy-flag set and caches the choice, so any version
skew self-heals within a single attempt instead of failing every daemon retry.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..agents import AgentSpec
from .base import AdapterResult, BaseAdapter


def classify_codex_help(help_text: str) -> list[str]:
    """Map a ``codex exec --help`` dump onto the autonomy flags it supports."""
    if "--full-auto" in help_text:
        return ["--full-auto"]
    # Current CLIs (0.15x): --approve-for-me implies the workspace-write sandbox
    # and cannot be combined with an explicit --sandbox flag.
    return ["--approve-for-me"]


def _flag_rejection(error: str, flags: list[str]) -> bool:
    """True when the failure is the CLI rejecting one of our autonomy flags."""
    return any(f"unexpected argument '{flag}'" in error for flag in flags)


def probe_codex_autonomy_flags(binary: str | None = None) -> list[str]:
    """Probe the installed codex CLI once and return its autonomy flags."""
    path = binary or shutil.which("codex")
    if not path:
        return classify_codex_help("")  # no CLI visible: assume the current surface
    try:
        probe = subprocess.run(
            [path, "exec", "--help"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL
        )
        help_text = (probe.stdout or "") + (probe.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return classify_codex_help("")
    return classify_codex_help(help_text)


class CodexAdapter(BaseAdapter):
    kind = "codex"
    mode = "spawn"

    def __init__(self, spec: AgentSpec | None = None) -> None:
        super().__init__(spec)
        self._autonomy_flags: list[str] | None = None

    def _exec_flags(self) -> list[str]:
        if self._autonomy_flags is None:
            self._autonomy_flags = probe_codex_autonomy_flags(self.binary())
        return self._autonomy_flags

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        command = ["codex", "exec", *self._exec_flags()]
        model = settings.get("model") or (self.spec.model if self.spec else None)
        effort = settings.get("effort") or (self.spec.effort if self.spec else None)
        if model:
            command.extend(["--model", str(model)])
        if effort:
            command.extend(["--config", f'model_reasoning_effort="{effort}"'])
        command.append("-")  # prompt arrives on stdin
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
        on_line: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AdapterResult:
        first_flags = self._exec_flags()  # probe + cache before the first attempt
        result = super().run(
            prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir,
            on_line=on_line, should_cancel=should_cancel,
        )
        if not result.ok and _flag_rejection(result.error, first_flags):
            # Version skew: the installed CLI rejected the probed autonomy flag.
            # Retry once with the alternate surface (and remember it for this instance).
            self._autonomy_flags = ["--full-auto"] if "--approve-for-me" in first_flags else ["--approve-for-me"]
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
