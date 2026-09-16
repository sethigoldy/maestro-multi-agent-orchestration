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
