"""Tests for the agent-integration layer and the global skill source."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from maestro import integrations
from maestro.integrations import (
    AGENT_ALIASES,
    INTEGRATIONS,
    SKILL_NAME,
    AgentIntegration,
    SkillManager,
    _BlockIntegration,
    _managed_block,
    _remove_block,
    _replace_block,
    _strip_frontmatter,
    load_skill_content,
    skill_source,
)

CONTENT = load_skill_content()


def _fake_executable(path: Path, body: str = "echo ok") -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# ---------------------------------------------------------------- skill source

def test_skill_source_exists_and_is_complete():
    assert skill_source().is_file()
    text = load_skill_content()
    # The operational contract the rest of the feature set depends on:
    assert "name: maestro-driven-development" in text
    assert "MAESTRO_AGENT_CONTEXT" in text  # recursion guard
    assert "maestro daemon status --json" in text  # health check
    assert "maestro daemon start" in text  # auto-start
    assert "maestro delegate" in text  # handoff
    assert "Do not pick the agent" in text or "Do not hardcode which agent" in text  # selection via Maestro
    assert "Fallback" in text  # graceful degradation


def test_strip_frontmatter():
    with_fm = "---\nname: x\n---\nbody"
    assert _strip_frontmatter(with_fm) == "body"
    assert _strip_frontmatter("plain body") == "plain body"
    assert _strip_frontmatter("---\nno closing fence") == "---\nno closing fence"


def test_managed_block_helpers():
    block = _managed_block("hello")
    new_text, present = _replace_block("", block)
    assert present is False and "hello" in new_text
    new_text2, present2 = _replace_block(new_text, _managed_block("world"))
    assert present2 is True and "world" in new_text2 and "hello" not in new_text2
    assert new_text2.count(integrations._BEGIN) == 1  # replaced, not duplicated
    stripped, removed = _remove_block(new_text2)
    assert removed is True and "world" not in stripped
    unchanged, removed2 = _remove_block("foreign text")
    assert removed2 is False


# ------------------------------------------------------------- block integrations

BLOCK_KINDS = ["codex", "copilot", "hermes", "pi", "cline", "opencode"]


@pytest.mark.parametrize("kind", BLOCK_KINDS)
def test_block_install_uninstall_cycle(tmp_path, kind):
    integration = INTEGRATIONS[kind]()
    home = tmp_path / "home"

    assert integration.status(home)["installed"] is False

    result = integration.install_skill(home, CONTENT)
    assert result.ok is True and result.action == "installed"
    path = Path(result.path)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert integrations._BEGIN in text and integrations._END in text
    assert "Maestro-Driven Development" in text

    # Reinstall: idempotent, no duplicate managed region.
    again = integration.install_skill(home, CONTENT)
    assert again.action == "already-installed"
    assert Path(result.path).read_text(encoding="utf-8").count(integrations._BEGIN) == 1

    assert integration.status(home)["installed"] is True

    removed = integration.uninstall_skill(home)
    assert removed.ok is True and removed.action == "uninstalled"
    assert not Path(result.path).is_file()  # file only contained our block

    # Uninstall again: skipped, not an error.
    noop = integration.uninstall_skill(home)
    assert noop.ok is True and noop.action == "skipped"


@pytest.mark.parametrize("kind", BLOCK_KINDS)
def test_block_preserves_user_content(tmp_path, kind):
    integration = INTEGRATIONS[kind]()
    home = tmp_path / "home"
    path = integration.instructions_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("# my rules\nbe careful with rm -rf\n", encoding="utf-8")

    integration.install_skill(home, CONTENT)
    text = path.read_text(encoding="utf-8")
    assert "be careful with rm -rf" in text and integrations._BEGIN in text

    integration.uninstall_skill(home)
    text = path.read_text(encoding="utf-8")
    assert "be careful with rm -rf" in text and integrations._BEGIN not in text


@pytest.mark.parametrize("kind", BLOCK_KINDS)
def test_block_uninstall_when_not_installed(tmp_path, kind):
    integration = INTEGRATIONS[kind]()
    home = tmp_path / "home"
    path = integration.instructions_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("user text only\n", encoding="utf-8")
    result = integration.uninstall_skill(home)
    assert result.ok is True and result.action == "skipped"
    assert path.read_text(encoding="utf-8") == "user text only\n"


def test_block_install_read_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["codex"]()
    home = tmp_path / "home"
    path = integration.instructions_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("user content\n", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("read denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    result = integration.install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"


def test_block_install_write_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["codex"]()
    home = tmp_path / "home"

    def _boom(self, *a, **k):
        raise OSError("write denied")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = integration.install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"


def test_block_uninstall_write_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["codex"]()
    home = tmp_path / "home"
    path = integration.instructions_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("user text\n" + _managed_block("x") + "\n", encoding="utf-8")

    def _boom(self, *a, **k):
        raise OSError("write denied")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = integration.uninstall_skill(home)
    assert result.ok is False and result.action == "error"


def test_block_uninstall_read_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["codex"]()
    home = tmp_path / "home"
    path = integration.instructions_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("user text\n" + _managed_block("x") + "\n", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("read denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    result = integration.uninstall_skill(home)
    assert result.ok is False and result.action == "error"


def test_block_status_read_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["codex"]()
    home = tmp_path / "home"
    path = integration.instructions_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(integrations._BEGIN, encoding="utf-8")

    def _boom(self, *a, **k):
        raise OSError("read denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert integration.status(home)["installed"] is False


# ------------------------------------------------------------------- claude code

def test_claude_code_cycle(tmp_path):
    integration = INTEGRATIONS["claude_code"]()
    home = tmp_path / "home"
    target = home / ".claude" / "skills" / SKILL_NAME / "SKILL.md"

    result = integration.install_skill(home, CONTENT)
    assert result.ok is True and result.action == "installed"
    assert target.is_file() and target.read_text(encoding="utf-8") == CONTENT

    again = integration.install_skill(home, CONTENT)
    assert again.action == "already-installed"

    # A user edit makes the next install a fresh write.
    target.write_text("edited by user", encoding="utf-8")
    updated = integration.install_skill(home, CONTENT)
    assert updated.action == "installed"

    assert integration.status(home)["installed"] is True
    removed = integration.uninstall_skill(home)
    assert removed.ok is True and removed.action == "uninstalled"
    assert not target.exists() and not (home / ".claude" / "skills" / SKILL_NAME).exists()

    noop = integration.uninstall_skill(home)
    assert noop.ok is True and noop.action == "skipped"


def test_claude_code_read_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["claude_code"]()
    home = tmp_path / "home"
    target = home / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    def _boom(self, *a, **k):
        raise OSError("read denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    result = integration.install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"


def test_claude_code_write_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["claude_code"]()
    home = tmp_path / "home"

    def _boom(self, *a, **k):
        raise OSError("write denied")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = integration.install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"


def test_claude_code_uninstall_error(tmp_path, monkeypatch):
    import shutil as _shutil

    integration = INTEGRATIONS["claude_code"]()
    home = tmp_path / "home"
    target = home / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(CONTENT, encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("rmtree denied")

    monkeypatch.setattr(_shutil, "rmtree", _boom)
    result = integration.uninstall_skill(home)
    assert result.ok is False and result.action == "error"


# ------------------------------------------------------------------------ cursor

def test_cursor_rule_rendering_and_cycle(tmp_path):
    integration = INTEGRATIONS["cursor"]()
    home = tmp_path / "home"
    target = home / ".cursor" / "rules" / f"{SKILL_NAME}.mdc"

    result = integration.install_skill(home, CONTENT)
    assert result.ok is True and result.action == "installed"
    text = target.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "alwaysApply: true" in text
    assert "name: maestro-driven-development" not in text  # original frontmatter stripped
    assert "Maestro-Driven Development" in text

    again = integration.install_skill(home, CONTENT)
    assert again.action == "already-installed"

    assert integration.status(home)["installed"] is True
    removed = integration.uninstall_skill(home)
    assert removed.ok is True and removed.action == "uninstalled"
    assert not target.exists()
    noop = integration.uninstall_skill(home)
    assert noop.ok is True and noop.action == "skipped"


def test_cursor_read_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["cursor"]()
    home = tmp_path / "home"
    target = home / ".cursor" / "rules" / f"{SKILL_NAME}.mdc"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    def _boom(self, *a, **k):
        raise OSError("read denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    result = integration.install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"


def test_cursor_write_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["cursor"]()
    home = tmp_path / "home"

    def _boom(self, *a, **k):
        raise OSError("write denied")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = integration.install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"


def test_cursor_uninstall_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["cursor"]()
    home = tmp_path / "home"
    target = home / ".cursor" / "rules" / f"{SKILL_NAME}.mdc"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    def _boom(self, *a, **k):
        raise OSError("unlink denied")

    monkeypatch.setattr(Path, "unlink", _boom)
    result = integration.uninstall_skill(home)
    assert result.ok is False and result.action == "error"


# ---------------------------------------------------------------------- openhands

def test_openhands_cycle(tmp_path):
    integration = INTEGRATIONS["openhands"]()
    home = tmp_path / "home"
    path = home / ".openhands" / "agent_settings.json"

    result = integration.install_skill(home, CONTENT)
    assert result.ok is True and result.action == "installed"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert integrations._BEGIN in data["custom_instructions"]

    again = integration.install_skill(home, CONTENT)
    assert again.action == "already-installed"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["custom_instructions"].count(integrations._BEGIN) == 1

    assert integration.status(home)["installed"] is True
    removed = integration.uninstall_skill(home)
    assert removed.ok is True and removed.action == "uninstalled"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "custom_instructions" not in data

    noop = integration.uninstall_skill(home)
    assert noop.ok is True and noop.action == "skipped"


def test_openhands_preserves_existing_settings(tmp_path):
    integration = INTEGRATIONS["openhands"]()
    home = tmp_path / "home"
    path = home / ".openhands" / "agent_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"custom_instructions": "always run tests", "llm": {"model": "x"}}), encoding="utf-8")

    integration.install_skill(home, CONTENT)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["llm"] == {"model": "x"}
    assert "always run tests" in data["custom_instructions"] and integrations._BEGIN in data["custom_instructions"]

    integration.uninstall_skill(home)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["llm"] == {"model": "x"}
    assert "always run tests" in data["custom_instructions"]
    assert integrations._BEGIN not in data["custom_instructions"]


def test_openhands_invalid_json(tmp_path):
    integration = INTEGRATIONS["openhands"]()
    home = tmp_path / "home"
    path = home / ".openhands" / "agent_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    result = integration.install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"


def test_openhands_non_object_json(tmp_path):
    integration = INTEGRATIONS["openhands"]()
    home = tmp_path / "home"
    path = home / ".openhands" / "agent_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2]", encoding="utf-8")
    result = integration.install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"


def test_openhands_uninstall_invalid_json(tmp_path):
    integration = INTEGRATIONS["openhands"]()
    home = tmp_path / "home"
    path = home / ".openhands" / "agent_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    result = integration.uninstall_skill(home)
    assert result.ok is False and result.action == "error"


def test_openhands_uninstall_missing_file(tmp_path):
    integration = INTEGRATIONS["openhands"]()
    result = integration.uninstall_skill(tmp_path / "home")
    assert result.ok is True and result.action == "skipped"


def test_openhands_write_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["openhands"]()
    home = tmp_path / "home"

    def _boom(self, *a, **k):
        raise OSError("write denied")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = integration.install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"


def test_openhands_uninstall_write_error(tmp_path, monkeypatch):
    integration = INTEGRATIONS["openhands"]()
    home = tmp_path / "home"
    path = home / ".openhands" / "agent_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"other": 1, "custom_instructions": _managed_block(CONTENT)}), encoding="utf-8")

    def _boom(self, *a, **k):
        raise OSError("write denied")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = integration.uninstall_skill(home)
    assert result.ok is False and result.action == "error"


def test_base_class_methods_raise_not_implemented(tmp_path):
    base = AgentIntegration()
    with pytest.raises(NotImplementedError):
        base.install_skill(tmp_path, CONTENT)
    with pytest.raises(NotImplementedError):
        base.uninstall_skill(tmp_path)
    with pytest.raises(NotImplementedError):
        base.status(tmp_path)
    block = _BlockIntegration()
    with pytest.raises(NotImplementedError):
        block.instructions_path(tmp_path)


def test_openhands_status_invalid_json(tmp_path):
    integration = INTEGRATIONS["openhands"]()
    home = tmp_path / "home"
    path = home / ".openhands" / "agent_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("nope", encoding="utf-8")
    assert integration.status(home)["installed"] is False


# ------------------------------------------------------------------ skill manager

def test_manager_resolve_variants(tmp_path):
    manager = SkillManager(home=tmp_path / "home")
    assert manager.resolve("codex").kind == "codex"
    assert manager.resolve("claude").kind == "claude_code"  # binary alias
    assert manager.resolve("cursor-agent").kind == "cursor"  # binary name
    assert manager.resolve("GitHub Copilot CLI").kind == "copilot"  # display name
    assert manager.resolve("CLAUDE_CODE").kind == "claude_code"  # case-insensitive kind
    assert manager.resolve("hermes agent").kind == "hermes"  # alias table
    assert manager.resolve("no-such-agent") is None


def test_manager_install_detected_only(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")
    _fake_executable(bindir / "claude")
    monkeypatch.setenv("PATH", str(bindir))
    home = tmp_path / "home"
    manager = SkillManager(home=home)

    results = manager.install()
    by_kind = {r.kind: r for r in results}
    assert by_kind["codex"].action == "installed"
    assert by_kind["claude_code"].action == "installed"
    for kind in ("copilot", "cursor", "hermes", "pi", "cline", "openhands"):
        assert by_kind[kind].action == "not-detected" and by_kind[kind].ok is True
    # Only detected agents received files.
    assert (home / ".codex" / "AGENTS.md").is_file()
    assert (home / ".claude" / "skills" / SKILL_NAME / "SKILL.md").is_file()
    assert not (home / ".copilot" / "instructions.md").exists()


def test_manager_install_all(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))  # nothing detected
    home = tmp_path / "home"
    manager = SkillManager(home=home)

    results = manager.install(all_agents=True)
    assert len(results) == len(INTEGRATIONS)
    assert all(r.ok for r in results)
    assert (home / ".cursor" / "rules" / f"{SKILL_NAME}.mdc").is_file()
    data = json.loads((home / ".openhands" / "agent_settings.json").read_text(encoding="utf-8"))
    assert integrations._BEGIN in data["custom_instructions"]


def test_manager_install_explicit_agent_undetected(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))  # nothing detected
    home = tmp_path / "home"
    manager = SkillManager(home=home)

    results = manager.install(agent="codex")
    assert len(results) == 1 and results[0].ok is True and results[0].action == "installed"
    assert "not found on PATH" in results[0].detail


def test_manager_install_explicit_agent_detected(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")  # codex IS detected
    monkeypatch.setenv("PATH", str(bindir))
    home = tmp_path / "home"
    manager = SkillManager(home=home)

    results = manager.install(agent="codex")
    assert len(results) == 1 and results[0].ok is True and results[0].action == "installed"
    assert "not found on PATH" not in results[0].detail


def test_manager_install_unknown_agent(tmp_path):
    manager = SkillManager(home=tmp_path / "home")
    results = manager.install(agent="vim")
    assert len(results) == 1 and results[0].ok is False and results[0].action == "error"


def test_manager_uninstall_everywhere(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")
    _fake_executable(bindir / "claude")
    monkeypatch.setenv("PATH", str(bindir))
    home = tmp_path / "home"
    manager = SkillManager(home=home)

    manager.install()  # codex + claude only (detected)
    results = manager.uninstall()
    assert {r.kind for r in results} == {"codex", "claude_code"}
    assert all(r.action == "uninstalled" for r in results)
    assert not (home / ".codex" / "AGENTS.md").exists()

    # Nothing installed anymore.
    assert manager.uninstall() == []


def test_manager_uninstall_explicit_and_unknown(tmp_path):
    home = tmp_path / "home"
    manager = SkillManager(home=home)
    INTEGRATIONS["pi"]().install_skill(home, CONTENT)
    results = manager.uninstall(agent="pi")
    assert len(results) == 1 and results[0].action == "uninstalled"
    bad = manager.uninstall(agent="emacs")
    assert bad[0].ok is False and bad[0].action == "error"


def test_manager_status_shape(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")
    monkeypatch.setenv("PATH", str(bindir))
    home = tmp_path / "home"
    manager = SkillManager(home=home)
    INTEGRATIONS["codex"]().install_skill(home, CONTENT)

    entries = {e["kind"]: e for e in manager.status()}
    assert set(entries) == set(INTEGRATIONS)
    codex = entries["codex"]
    assert codex["detected"] is True and codex["installed"] is True
    assert codex["binary"] == "codex" and codex["display_name"] == "Codex"
    assert "mechanism" in codex and "path" in codex
    assert entries["claude_code"]["detected"] is False and entries["claude_code"]["installed"] is False


def test_manager_default_home_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    manager = SkillManager()
    assert manager.home == (tmp_path / "home")


def test_aliases_cover_supported_agents():
    assert set(INTEGRATIONS) == {"codex", "claude_code", "copilot", "cursor", "hermes", "pi", "cline", "opencode", "openhands"}
    assert AGENT_ALIASES["claude"] == "claude_code"
