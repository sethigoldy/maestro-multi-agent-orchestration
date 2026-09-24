"""Codex CLI adapter (generalizes the 0.8.x worker command).

The flags that let ``codex exec`` work without asking moved across codex
releases. Current CLIs (0.146 and later) take ``--sandbox workspace-write``:
they print "--full-auto is deprecated; use --sandbox workspace-write
instead" and reject ``--approve-for-me``. Older CLIs take ``--full-auto``,
and some take ``--approve-for-me``. The adapter reads ``codex exec --help``
once per instance and picks the flag set that the help lists, preferring
``--approve-for-me``, then ``--sandbox workspace-write``, then
``--full-auto``; with no usable help it assumes the current surface.

The help is a first guess only. When the installed CLI rejects the chosen
flag at argument-parse time (clap's ``error: unexpected argument ...
found``), the adapter tries each remaining flag set once, in the order
above, and keeps the one that works for this instance.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..agents import AgentSpec
from .base import AdapterResult, BaseAdapter


SANDBOX_FLAGS = ["--sandbox", "workspace-write"]
# Every autonomy flag set, in the order they are tried after a rejection.
AUTONOMY_FLAG_SETS = [SANDBOX_FLAGS, ["--full-auto"], ["--approve-for-me"]]


def classify_codex_help(help_text: str) -> list[str]:
    """Map a ``codex exec --help`` dump onto the autonomy flags it supports."""
    if "--approve-for-me" in help_text:
        # Implies the workspace-write sandbox; cannot be combined with --sandbox.
        return ["--approve-for-me"]
    if "--sandbox" in help_text:
        return list(SANDBOX_FLAGS)
    if "--full-auto" in help_text:
        return ["--full-auto"]
    return list(SANDBOX_FLAGS)  # no usable help: assume the current surface


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
            [path, "exec", "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, stdin=subprocess.DEVNULL,
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
        tried = [self._exec_flags()]  # probe + cache before the first attempt
        result = super().run(
            prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir,
            on_line=on_line, should_cancel=should_cancel,
        )
        # Version skew: the installed CLI rejected the chosen autonomy flag.
        # Try each remaining flag set once, and keep the one that works.
        while not result.ok and _flag_rejection(result.error, tried[-1][:1]):
            remaining = [flags for flags in AUTONOMY_FLAG_SETS if flags not in tried]
            if not remaining:
                break
            self._autonomy_flags = list(remaining[0])
            tried.append(self._autonomy_flags)
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
