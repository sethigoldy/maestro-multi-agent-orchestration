"""Generic (declarative) adapter: onboards any CLI from its registry spec."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from .base import AdapterPreflight, BaseAdapter

#: The placeholders a generic command template may use.
_PLACEHOLDER_RE = re.compile(r"\{(prompt|workspace|task_id)\}")

#: Programs whose ``-c`` argument is a script that the shell parses again.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})


def _fish_quote(value: str) -> str:
    """Quote ``value`` for fish, whose single quotes treat \\ and \\' as escapes."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _shell_script_index(words: list[str]) -> int | None:
    """Index of the script word when the template runs a shell with ``-c``.

    Returns None when the program (the file name of the first word) is not one
    of :data:`_SHELLS`, or when no ``-c`` option is given. Short options are
    read the way the shells read them: ``-c`` may be combined with other
    letters (``-lc``, ``-ec``), and an option cluster that ends in ``o`` or
    ``O`` takes the next word as its value (``-o pipefail``). fish also accepts
    ``--command SCRIPT`` and ``--command=SCRIPT``. The script is the first word
    after the ``-c`` option that is not itself an option; ``--`` ends the
    options. Words before ``-c`` that are not options (for example the value
    of ``--rcfile FILE``) are skipped, so the search errs towards quoting.
    """
    if not words or Path(words[0]).name not in _SHELLS:
        return None
    has_command_flag = False
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            return index if has_command_flag and index < len(words) else None
        if word.startswith("--command="):
            return index
        if word == "--command":
            has_command_flag = True
        elif len(word) > 1 and word[0] in "-+" and not word.startswith("--"):
            if word[0] == "-" and "c" in word[1:]:
                has_command_flag = True
            if word[-1] in "oO":
                index += 1  # the option's value, such as "pipefail"
        elif has_command_flag and not word.startswith("--"):
            return index  # the first word after -c that is not an option
        index += 1
    return None


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
        """Split the registry template into arguments, then fill placeholders.

        The template is split with shell rules first, and each placeholder is
        then replaced inside the single argument that holds it. Substituted
        values are never split or re-scanned, so a workspace path with spaces or
        an apostrophe stays one argument, and a prompt that happens to contain
        the text ``{workspace}`` is passed through unchanged. In ``stdin`` mode
        the prompt is piped, so ``{prompt}`` is left as written.

        One argument is treated differently: when the template starts a shell
        with ``-c`` (for example ``sh -c "mytool {prompt}"``), that shell parses
        its script argument again, so a raw prompt could run commands. Values
        placed in that script argument are shell-quoted; every other argument
        receives the value raw.
        """
        assert self.spec is not None and self.spec.command
        try:
            words = shlex.split(self.spec.command)
        except ValueError as exc:
            raise ValueError(f"Agent {self.spec.name!r} has an invalid command template ({exc}): {self.spec.command!r}") from exc
        values = {"workspace": str(workspace), "task_id": task_id}
        if self.input_mode() != "stdin":
            values["prompt"] = prompt
        script_index = _shell_script_index(words)
        quote = _fish_quote if script_index is not None and Path(words[0]).name == "fish" else shlex.quote

        def raw(match: re.Match[str]) -> str:
            return values.get(match.group(1), match.group(0))

        def quoted(match: re.Match[str]) -> str:
            name = match.group(1)
            return quote(values[name]) if name in values else match.group(0)

        return [_PLACEHOLDER_RE.sub(quoted if index == script_index else raw, word) for index, word in enumerate(words)]

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
