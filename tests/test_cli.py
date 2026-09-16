from pathlib import Path
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
