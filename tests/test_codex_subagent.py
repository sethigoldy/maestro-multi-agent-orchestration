"""The Codex custom agent ``maestro_worker``, which shows Maestro tasks in Codex's subagent panel.

Codex lists a thread in its subagent panel only when Codex itself spawned it
with ``spawn_agent``. Maestro therefore installs a Codex custom agent file
(``~/.codex/agents/maestro-worker.toml``) and a Codex-only section in the
``~/.codex/AGENTS.md`` block that tells Codex to spawn it for each Maestro
task. These tests cover the packaged files, their install and removal, and the
rule that a file Maestro did not write is never overwritten or deleted.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from maestro import cli, integrations
from maestro.integrations import (
    CODEX_AGENT_NAME,
    INTEGRATIONS,
    SkillManager,
    codex_agent_source,
    codex_subagents_source,
    load_skill_content,
)

CONTENT = load_skill_content()


def _codex():
    return INTEGRATIONS["codex"]()


def _agent_path(home: Path) -> Path:
    return home / ".codex" / "agents" / "maestro-worker.toml"


# ------------------------------------------------------------ packaged files

def test_agent_file_matches_the_codex_role_file_format():
    """The packaged file must load in Codex: Codex rejects unknown keys and blank values."""
    text = codex_agent_source().read_text(encoding="utf-8")
    assert text.startswith(integrations._CODEX_AGENT_MARKER)
    data = tomllib.loads(text)
    # Codex's role file accepts name, description, nickname_candidates and
    # config.toml keys (developer_instructions is one); anything else fails to load.
    assert set(data) == {"name", "description", "nickname_candidates", "developer_instructions"}
    assert data["name"] == CODEX_AGENT_NAME
    assert data["description"].strip() and data["developer_instructions"].strip()
    nicknames = data["nickname_candidates"]
    assert nicknames and len(set(nicknames)) == len(nicknames)
    assert all(re.fullmatch(r"[A-Za-z0-9 _-]+", n.strip()) for n in nicknames)


def test_agent_instructions_hand_the_work_to_maestro_and_never_recurse():
    instructions = tomllib.loads(codex_agent_source().read_text(encoding="utf-8"))["developer_instructions"]
    assert "printenv MAESTRO_AGENT_CONTEXT" in instructions  # recursion guard comes first
    assert instructions.index("MAESTRO_AGENT_CONTEXT") < instructions.index("maestro delegate")
    assert "Do not spawn subagents" in instructions  # the worker must not spawn another worker
    assert "Do not edit files" in instructions  # the worker never implements the work
    assert "maestro daemon status --json" in instructions
    assert "maestro task answer <task-id>" in instructions
    assert "maestro task continue <task-id>" in instructions
    assert "input-required" in instructions


def test_codex_section_names_the_agent_and_the_fallback():
    section = codex_subagents_source().read_text(encoding="utf-8")
    assert f'agent_type = "{CODEX_AGENT_NAME}"' in section
    assert "spawn_agent" in section and "wait_agent" in section
    assert "~/.codex/agents/maestro-worker.toml" in section
    # Without the tool or the agent type, Codex keeps using the CLI.
    assert "delegate with the CLI as in section 5" in section
    # A worker reads the same AGENTS.md, so the section must tell it to skip this part.
    assert "If you are a `maestro_worker` yourself, ignore this section" in section


def test_packaged_files_ship_in_the_wheel_and_sdist():
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"]["maestro"]
    for source in (codex_agent_source(), codex_subagents_source()):
        relative = source.relative_to(Path(integrations.__file__).resolve().parent).as_posix()
        assert any(Path(relative).match(p) for p in patterns), relative


# ------------------------------------------------------------------ install

def test_install_writes_the_agent_file_and_the_codex_section(tmp_path):
    home = tmp_path / "home"
    result = _codex().install_skill(home, CONTENT)
    assert result.ok and result.action == "installed"
    assert str(_agent_path(home)) in result.detail
    assert _agent_path(home).read_text(encoding="utf-8") == codex_agent_source().read_text(encoding="utf-8")
    agents_md = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert "# Maestro-Driven Development" in agents_md
    assert "## Codex: run each Maestro task as a subagent" in agents_md
    # The Codex section sits inside the managed block, so uninstall removes it too.
    begin, end = agents_md.index(integrations._BEGIN), agents_md.index(integrations._END)
    assert begin < agents_md.index("## Codex: run each Maestro task") < end
    status = _codex().status(home)
    assert status["subagent_installed"] is True and status["subagent_path"] == str(_agent_path(home))


def test_the_codex_section_is_not_given_to_other_agents(tmp_path):
    home = tmp_path / "home"
    SkillManager(home=home).install(all_agents=True)
    assert "## Codex: run each Maestro task" not in (home / ".copilot" / "instructions.md").read_text(encoding="utf-8")
    assert "## Codex: run each Maestro task" not in (
        home / ".claude" / "skills" / integrations.SKILL_NAME / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_reinstall_is_idempotent(tmp_path):
    home = tmp_path / "home"
    _codex().install_skill(home, CONTENT)
    again = _codex().install_skill(home, CONTENT)
    assert again.ok and again.action == "already-installed"
    assert (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8").count("## Codex: run each Maestro task") == 1


def test_upgrade_from_a_release_without_the_agent_file_reports_installed(tmp_path):
    """A 0.16.2 install has the block but no agent file; the upgrade must say it installed something."""
    home = tmp_path / "home"
    _codex().install_skill(home, CONTENT)
    _agent_path(home).unlink()
    result = _codex().install_skill(home, CONTENT)
    assert result.ok and result.action == "installed"
    assert _agent_path(home).is_file()


def test_an_older_managed_agent_file_is_rewritten(tmp_path):
    home = tmp_path / "home"
    _agent_path(home).parent.mkdir(parents=True)
    _agent_path(home).write_text(f"{integrations._CODEX_AGENT_MARKER}\nname = \"old\"\n", encoding="utf-8")
    result = _codex().install_skill(home, CONTENT)
    assert result.ok
    assert _agent_path(home).read_text(encoding="utf-8") == codex_agent_source().read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "existing",
    [
        b'name = "maestro_worker"\ndescription = "mine"\ndeveloper_instructions = "mine"\n',
        b"\xff\xfe not utf-8",
    ],
    ids=["user-written", "unreadable"],
)
def test_a_file_maestro_did_not_write_is_left_alone(tmp_path, existing):
    home = tmp_path / "home"
    _agent_path(home).parent.mkdir(parents=True)
    _agent_path(home).write_bytes(existing)

    result = _codex().install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"
    assert "Maestro did not write" in result.detail and result.path == str(_agent_path(home))
    assert _agent_path(home).read_bytes() == existing
    assert _codex().status(home)["subagent_installed"] is False

    _codex().uninstall_skill(home)
    assert _agent_path(home).read_bytes() == existing


def test_a_directory_at_the_agent_path_is_left_alone(tmp_path):
    home = tmp_path / "home"
    _agent_path(home).mkdir(parents=True)
    result = _codex().install_skill(home, CONTENT)
    assert result.ok is False and "Maestro did not write" in result.detail
    assert _agent_path(home).is_dir()


def test_agent_file_write_error_is_reported(tmp_path, monkeypatch):
    home = tmp_path / "home"
    agent_path = _agent_path(home)
    real_write_text = Path.write_text

    def guarded_write_text(self, *args, **kwargs):
        if self == agent_path:
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    result = _codex().install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"
    assert "disk full" in result.detail and result.path == str(agent_path)


# ---------------------------------------------------------------- uninstall

def test_uninstall_removes_the_block_and_the_agent_file(tmp_path):
    home = tmp_path / "home"
    _codex().install_skill(home, CONTENT)
    result = _codex().uninstall_skill(home)
    assert result.ok and result.action == "uninstalled"
    assert "subagent file removed" in result.detail
    assert not _agent_path(home).exists() and not (home / ".codex" / "AGENTS.md").exists()
    assert _codex().status(home)["subagent_installed"] is False


def test_uninstall_removes_an_agent_file_left_without_its_block(tmp_path):
    home = tmp_path / "home"
    _codex().install_skill(home, CONTENT)
    (home / ".codex" / "AGENTS.md").unlink()

    results = SkillManager(home=home).uninstall()  # no selector: finds it through status
    assert [(r.kind, r.action, r.detail) for r in results] == [("codex", "uninstalled", "subagent file removed")]
    assert results[0].path == str(_agent_path(home))
    assert not _agent_path(home).exists()
    assert _codex().uninstall_skill(home).action == "skipped"


def test_agent_file_removal_error_is_reported(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _codex().install_skill(home, CONTENT)
    agent_path = _agent_path(home)
    real_unlink = Path.unlink

    def guarded_unlink(self, *args, **kwargs):
        if self == agent_path:
            raise OSError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    result = _codex().uninstall_skill(home)
    assert result.ok is False and result.action == "error"
    assert "read-only" in result.detail and result.path == str(agent_path)


# ---------------------------------------------------------------------- CLI

def test_cli_install_and_uninstall_manage_the_agent_file(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))

    assert cli.main(["skill", "install", "--agent", "codex"]) == 0
    assert _agent_path(home).is_file()
    capsys.readouterr()

    assert cli.main(["skill", "status"]) == 0
    codex = next(e for e in json.loads(capsys.readouterr().out) if e["kind"] == "codex")
    assert codex["subagent_installed"] is True

    assert cli.main(["skill", "uninstall", "--agent", "codex"]) == 0
    assert not _agent_path(home).exists()


# --------------------------------------------------- the worker's CLI commands

def _worker_instructions() -> str:
    return tomllib.loads(codex_agent_source().read_text(encoding="utf-8"))["developer_instructions"]


_PLACEHOLDERS = {
    '"<title>"': '"T"', '"<request>"': '"R"', '"<instruction>"': '"fix it"',
    "<absolute path>": "/ws", "<task-id>": "task-1", "<answer>": "codex", "<path>": "h.toml",
}


def _worker_commands() -> list[str]:
    """Every `maestro ...` command the worker is told to run, with sample values."""
    text = _worker_instructions()
    commands = re.findall(r"`(maestro [^`]+)`", text)
    commands += [line.strip() for line in text.splitlines() if line.strip().startswith("maestro delegate --")]
    out = []
    for command in commands:
        for placeholder, value in _PLACEHOLDERS.items():
            command = command.replace(placeholder, value)
        out.append(command)
    return sorted(set(out))


@pytest.mark.parametrize("command", _worker_commands())
def test_every_command_the_worker_runs_is_accepted_by_the_cli(command):
    """The worker's instructions restate CLI commands; this fails when the CLI changes under them."""
    import shlex

    assert "<" not in command, f"unreplaced placeholder in {command!r}"
    argv = shlex.split(command)[1:]
    try:
        cli.build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse rejects unknown commands and flags this way
        pytest.fail(f"the CLI rejects {command!r} (exit {exc.code})")


def test_every_delegate_flag_the_worker_names_exists():
    parser = next(
        action for action in cli.build_parser()._actions if action.__class__.__name__ == "_SubParsersAction"
    ).choices["delegate"]
    known = {option for action in parser._actions for option in action.option_strings}
    named = set(re.findall(r"`(--[a-z-]+)", _worker_instructions()))
    assert named and named <= known, named - known


def test_the_worker_covers_queued_and_parked_tasks():
    text = _worker_instructions()
    assert "— queued: <reason>" in text  # the line cli.py prints for a queued task
    assert "maestro task status <task-id>" in text  # delegate does not print run_dir or branch
    assert "maestro task tail <task-id>" in text  # how to go on waiting after the command stopped
    assert "3600000" not in text  # no fixed one-hour limit


# ------------------------------------------------ files that are not Maestro's

def test_a_symlink_at_the_agent_path_is_never_written_through(tmp_path):
    home = tmp_path / "home"
    target = tmp_path / "dotfiles" / "missing.toml"  # a broken link
    _agent_path(home).parent.mkdir(parents=True)
    _agent_path(home).symlink_to(target)
    result = _codex().install_skill(home, CONTENT)
    assert result.ok is False and "Maestro did not write" in result.detail
    assert not target.exists() and _agent_path(home).is_symlink()


@pytest.mark.parametrize("where", ["agents-dir", "agents-subdir"])
def test_another_role_named_maestro_worker_blocks_the_subagent(tmp_path, where):
    home = tmp_path / "home"
    _codex().install_skill(home, CONTENT)  # an earlier install left Maestro's copy
    other = home / ".codex" / "agents" / ("mine.toml" if where == "agents-dir" else "team/mine.toml")
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text('name = "maestro_worker"\ndescription = "mine"\ndeveloper_instructions = "mine"\n', encoding="utf-8")

    result = _codex().install_skill(home, CONTENT)
    assert result.ok is False and result.action == "error"
    assert f"{other} already defines a Codex agent named maestro_worker" in result.detail
    assert "installed without the maestro_worker subagent" in result.detail
    # The skill itself is installed, without the section that would send Codex to the user's agent.
    agents_md = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert "# Maestro-Driven Development" in agents_md and "## Codex: run each Maestro task" not in agents_md
    assert not _agent_path(home).exists()  # Maestro's earlier copy would duplicate the role
    assert other.is_file()
    status = _codex().status(home)
    assert status["subagent_installed"] is False and "already defines" in status["subagent_conflict"]


def test_a_role_declared_in_config_toml_blocks_the_subagent(tmp_path):
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "x"\n[agents.maestro_worker]\ndescription = "mine"\n', encoding="utf-8")
    result = _codex().install_skill(home, CONTENT)
    assert result.ok is False and f"{config} already declares [agents.maestro_worker]" in result.detail
    assert not _agent_path(home).exists()


@pytest.mark.parametrize(
    "name, content",
    [
        ("other.toml", b'name = "reviewer"\ndescription = "d"\ndeveloper_instructions = "i"\n'),
        ("broken.toml", b"name = = \n"),
        ("binary.toml", b"\xff\xfe"),
        ("notes.md", b'name = "maestro_worker"\n'),  # Codex reads only .toml files
    ],
)
def test_unrelated_or_unreadable_files_do_not_block_the_subagent(tmp_path, name, content):
    home = tmp_path / "home"
    other = home / ".codex" / "agents" / name
    other.parent.mkdir(parents=True)
    other.write_bytes(content)
    (home / ".codex" / "config.toml").write_bytes(b"\xff not toml")
    result = _codex().install_skill(home, CONTENT)
    assert result.ok and _agent_path(home).is_file()
    assert "subagent_conflict" not in _codex().status(home)


def test_a_conflict_reports_a_failure_to_remove_the_earlier_copy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _codex().install_skill(home, CONTENT)
    (home / ".codex" / "config.toml").write_text("[agents.maestro_worker]\n", encoding="utf-8")
    agent_path = _agent_path(home)
    real_unlink = Path.unlink

    def guarded_unlink(self, *args, **kwargs):
        if self == agent_path:
            raise OSError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    result = _codex().install_skill(home, CONTENT)
    assert result.ok is False and "already declares" in result.detail and "read-only" in result.detail


def test_missing_packaged_files_fail_codex_only(tmp_path, monkeypatch):
    """A broken Maestro install must not stop the skill install for the other agents."""
    home = tmp_path / "home"
    monkeypatch.setattr(integrations, "codex_subagents_source", lambda: tmp_path / "missing.md")
    results = {r.kind: r for r in SkillManager(home=home).install(all_agents=True)}
    assert results["codex"].ok is False and "reinstall Maestro" in results["codex"].detail
    assert not (home / ".codex" / "AGENTS.md").exists()
    assert results["claude_code"].ok and results["cursor"].ok
