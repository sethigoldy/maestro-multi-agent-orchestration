"""Generic (declarative) adapter: onboards any CLI from its registry spec."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import AdapterPreflight, BaseAdapter


class GenericAdapter(BaseAdapter):
    """Runs a user-declared command template.

    The registry spec's ``command`` may contain ``{prompt}`` and ``{workspace}``
    placeholders; with ``input_mode = "stdin"`` the prompt is piped instead.
    ``output_format`` controls how structured lines are interpreted:
    ``jsonl`` parses each JSON line for usage/question hints, ``rpc``/``text``
    treat output as plain text (rpc mode itself lands with its adapters in M4).
    """

    kind = "generic"
    mode = "spawn"

    def __init__(self, spec: AgentSpec) -> None:
        super().__init__(spec)
        if spec is None or not spec.command:
            raise ValueError("GenericAdapter requires a registry spec with a launch command")
        # An http(s) "command" is a remote REST task server, not a CLI.
        if spec.command.startswith(("http://", "https://")):
            self.mode = "api"

    def preflight(self) -> AdapterPreflight:
        """For api-mode agents the reachability check replaces the binary probe."""
        if self.mode == "api":
            base_url = self.api_base_url({})
            if not base_url:
                return AdapterPreflight(ok=False, error="No api base URL configured for this generic agent")
            import urllib.error
            import urllib.request

            try:
                with urllib.request.urlopen(base_url + "/", timeout=10):
                    pass
            except urllib.error.HTTPError:
                return AdapterPreflight(ok=True, binary=base_url)  # it answered; that is reachability
            except Exception as exc:  # noqa: BLE001 - report any failure verbatim
                return AdapterPreflight(ok=False, binary=base_url, error=f"Remote task server unreachable at {base_url}: {exc}")
            return AdapterPreflight(ok=True, binary=base_url)
        return super().preflight()

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        assert self.spec is not None and self.spec.command
        template = self.spec.command
        if self.input_mode() == "stdin":
            rendered = template.replace("{workspace}", str(workspace)).replace("{task_id}", task_id)
        else:
            # Quote the prompt so it survives shlex.split as a single argument.
            rendered = template.replace("{prompt}", shlex.quote(prompt)).replace("{workspace}", shlex.quote(str(workspace))).replace("{task_id}", task_id)
        return shlex.split(rendered)

    def parse_line(self, line: str) -> dict[str, Any] | None:
        if self.spec is None or self.spec.output_format != "jsonl":
            return None
        stripped = line.strip()
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        out: dict[str, Any] = {}
        cost = event.get("cost_usd") or event.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            out["usage"] = {"cost_usd": float(cost)}
        question = event.get("question")
        if isinstance(question, str) and question.strip():
            out["question"] = question.strip()
        return out or None

    def detect_question(self, output_text: str) -> str | None:
        # Generic CLIs have no shared convention; only jsonl "question" events count.
        return None
