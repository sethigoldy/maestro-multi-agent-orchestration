from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_version_is_090():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["version"] == "0.16.0"


def test_claude_rules_are_zero_discovery_and_codex_first():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "Do not read Maestro source code" in text
    assert "delegate to Maestro/Codex" in text
    assert "Do not use Claude subagents for implementation" in text
    assert "followup" in text


def test_maestro_skill_is_minimal_routing_contract():
    text = (ROOT / ".claude/skills/maestro/SKILL.md").read_text()
    assert "Do not open Maestro source/docs" in text
    assert "delegate" in text
    assert "followup" in text
    assert "task_wait" in text


def test_agent_rules_keep_codex_as_implementation_owner():
    text = (ROOT / "AGENTS.md").read_text()
    assert "Codex owns implementation" in text
    assert "Do not inspect Maestro internals" in text
