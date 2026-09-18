from pathlib import Path
import json
import stat
import sys

from maestro import cli

def test_version(): assert cli.VERSION=='0.8.4'

def test_normalize_task_shortcut(): assert cli._normalize_argv(['task','abc'])==['task','status','abc']

def test_empty_workspace_uses_home(monkeypatch):
    monkeypatch.setenv('MAESTRO_WORKSPACE','')
    assert cli._workspace(None)==Path.home().resolve()


def test_cli_env_and_raw_argv_paths(monkeypatch):
    monkeypatch.setenv("MAESTRO_WORKSPACE", "   ")
    assert cli._workspace(None) == Path.home().resolve()
    assert cli._workspace("") == Path.home().resolve()
    assert cli._normalize_argv(["prog", "task", "abc"]) == ["prog", "task", "status", "abc"]


def test_cli_scope_empty_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MAESTRO_WORKSPACE", "")
    class FakeMaestro:
        @staticmethod
        def _resolve_project_root(path): return path
    monkeypatch.setattr(cli, "Maestro", FakeMaestro)
    args = type("A", (), {"project": None, "workspace": None})()
    base, scope = cli._scope_for_list(args)
    assert base == Path.home().resolve() and scope is None

def test_task_lookup_is_global(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("MAESTRO_HOME", str(home))

    # Populate the global registry/task store first.
    # Then invoke lookup from a different workspace and assert
    # the task is still resolvable.

def test_normalize_argv_with_program_name():
    assert cli._normalize_argv(
        ["maestro", "task", "task-123"]
    ) == [
        "maestro",
        "task",
        "status",
        "task-123",
    ]

def test_normalize_argv_named_task_commands_unchanged():
    assert cli._normalize_argv(
        ["maestro", "task", "status", "task-123"]
    ) == ["maestro", "task", "status", "task-123"]

    assert cli._normalize_argv(
        ["task", "list"]
    ) == ["task", "list"]

def test_task_workspace_empty_explicit_uses_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert cli._task_workspace("") == tmp_path.resolve()


def test_task_workspace_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MAESTRO_WORKSPACE", str(tmp_path))
    assert cli._task_workspace(None) == tmp_path.resolve()


def test_task_workspace_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("MAESTRO_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli._task_workspace(None) == tmp_path.resolve()


def _fake_executable(path: Path, body: str = "echo 'codex 9.9.9'") -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_agents_cli_lifecycle(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "codex")
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("PATH", str(bindir))

    def run(*argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["maestro", *argv])
        return cli.main()

    assert run("agents", "add", "--name", "codex", "--kind", "codex",
               "--display-name", "Codex CLI", "--skill", "implementation") == 0
    added = json.loads(capsys.readouterr().out)
    assert added["name"] == "codex" and added["kind"] == "codex"
    assert added["skills"] == ["implementation"] and added["display_name"] == "Codex CLI"

    assert run("agents", "list") == 0
    items = json.loads(capsys.readouterr().out)
    assert [i["name"] for i in items] == ["codex"]

    assert run("agents", "discover") == 0
    found = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    assert found["codex"]["found"] is True and found["claude_code"]["found"] is False

    assert run("agents", "status", "codex") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["registered"] is True and status["found"] is True
    assert status["version"] == "codex 9.9.9"

    assert run("agents", "status", "ghost") == 0
    assert json.loads(capsys.readouterr().out) == {"name": "ghost", "registered": False}

    assert run("agents", "add", "--name", "mycli", "--kind", "generic",
               "--command", "mycli --run {prompt}", "--input-mode", "stdin",
               "--output-format", "jsonl", "--workspace-policy", "flag") == 0
    generic = json.loads(capsys.readouterr().out)
    assert generic["kind"] == "generic" and generic["input_mode"] == "stdin"
    assert generic["output_format"] == "jsonl" and generic["workspace_policy"] == "flag"

    assert run("agents", "remove", "mycli") == 0
    assert json.loads(capsys.readouterr().out) == {"name": "mycli", "removed": True}

    assert run("agents", "remove", "ghost") == 2
    assert "not registered" in capsys.readouterr().err
