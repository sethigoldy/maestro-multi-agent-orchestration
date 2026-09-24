"""The agent skill must describe the real interface.

maestro skill install copies SKILL.md into every agent (for example into
~/.codex/AGENTS.md), and the skill tells agents it is the complete contract,
so they do not look anywhere else. When it fell behind (0.14.0 added
`maestro task answer`, but the skill still said there was no CLI answer
command), a Codex session could not answer a parked task. These tests fail
whenever a command, flag, MCP tool or config key is missing from the skill.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from maestro import cli, mcp_server
from maestro.core import Maestro

SKILL = Path(cli.__file__).parent / "skills" / "maestro-driven-development" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _commands() -> list[str]:
    """Every "maestro <command> [<subcommand>]" the CLI accepts."""
    out = []
    for name, sub in _subparsers(cli.build_parser()).items():
        children = _subparsers(sub)
        out.extend(f"maestro {name} {child}" for child in children) if children else out.append(f"maestro {name}")
    return out


@pytest.mark.parametrize("command", _commands())
def test_every_cli_command_is_in_the_skill(command):
    assert command in _text(), f"SKILL.md does not document {command!r}"


def test_every_option_of_the_commands_agents_use_is_in_the_skill():
    text = _text()
    parsers = _subparsers(cli.build_parser())
    checked = {
        "delegate": parsers["delegate"],
        "task answer": _subparsers(parsers["task"])["answer"],
        "task cancel": _subparsers(parsers["task"])["cancel"],
        "task cleanup": _subparsers(parsers["task"])["cleanup"],
        "task continue": _subparsers(parsers["task"])["continue"],
        "daemon start": _subparsers(parsers["daemon"])["start"],
        "daemon restart": _subparsers(parsers["daemon"])["restart"],
    }
    missing = []
    for name, parser in checked.items():
        for action in parser._actions:
            for flag in action.option_strings:
                if flag.startswith("--") and flag not in ("--help", "--workspace", "--project") and flag not in text:
                    missing.append(f"{name} {flag}")
    assert not missing, f"SKILL.md does not document: {missing}"


def test_every_mcp_tool_is_in_the_skill():
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    tools = re.findall(r"@mcp\.tool\(\)\s*\ndef (\w+)\(", source)
    assert tools
    missing = [tool for tool in tools if f"`{tool}(" not in _text()]
    assert not missing, f"SKILL.md does not document MCP tools: {missing}"


def test_every_defaults_key_and_new_config_setting_is_in_the_skill():
    text = _text()
    for key in ("agent", "fallback", "model", "effort", "max_parallel"):
        Maestro._parse_defaults({key: 2} if key == "max_parallel" else {key: ["x"]} if key == "fallback" else {key: "max" if key == "effort" else "x"})
        assert f"\n{key}" in text or f" {key} " in text or f"{key} =" in text, f"SKILL.md does not document [defaults] {key}"
    for setting in ("timeout_s", "[daemon]", "MAESTRO_DAEMON_PORT", "agent_may_commit"):
        assert setting in text, f"SKILL.md does not document {setting}"


def test_the_skill_no_longer_says_there_is_no_cli_answer():
    assert "no CLI answer" not in _text()
    assert "maestro task answer" in _text()
