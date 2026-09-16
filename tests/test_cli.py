from __future__ import annotations

from pathlib import Path

from maestro import cli


def test_version_constant() -> None:
    assert cli.VERSION == "0.5.5"


def test_workspace_prefers_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAESTRO_WORKSPACE", str(tmp_path))
    assert cli._workspace(None) == tmp_path.resolve()


def test_explicit_workspace_beats_environment(monkeypatch, tmp_path: Path) -> None:
    env_workspace = tmp_path / "env"
    explicit_workspace = tmp_path / "explicit"
    env_workspace.mkdir()
    explicit_workspace.mkdir()
    monkeypatch.setenv("MAESTRO_WORKSPACE", str(env_workspace))
    assert cli._workspace(str(explicit_workspace)) == explicit_workspace.resolve()


def test_normalize_task_reference() -> None:
    assert cli._normalize_argv(["maestro", "task", "10"]) == ["maestro", "task", "status", "10"]
    assert cli._normalize_argv(["maestro", "task", "task-abc", "--workspace", "/tmp/x"]) == [
        "maestro", "task", "status", "task-abc", "--workspace", "/tmp/x"
    ]
    assert cli._normalize_argv(["maestro", "task", "list"]) == ["maestro", "task", "list"]
    assert cli._normalize_argv(["maestro", "task", "status", "10"]) == ["maestro", "task", "status", "10"]


def test_discover_workspaces_only_existing_maestro_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    worktrees = project / ".claude" / "worktrees"
    worktrees.mkdir(parents=True)

    (project / ".maestro").mkdir()
    (project / ".maestro" / "memory.db").write_bytes(b"")

    valid = worktrees / "feature-a"
    valid.mkdir()
    (valid / ".maestro").mkdir()
    (valid / ".maestro" / "tasks.json").write_text("[]", encoding="utf-8")

    empty = worktrees / "feature-empty"
    empty.mkdir()

    assert cli._discover_workspaces(project) == [project.resolve(), valid.resolve()]


def test_project_tasks_aggregates_worktrees(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root = project / ".maestro"
    root.mkdir()
    (root / "tasks.json").write_text("[]", encoding="utf-8")
    wt = project / ".claude" / "worktrees" / "feature-a"
    (wt / ".maestro").mkdir(parents=True)
    (wt / ".maestro" / "tasks.json").write_text("[]", encoding="utf-8")

    class FakeMaestro:
        def __init__(self, workspace):
            self.workspace = Path(workspace)

        def list_tasks(self):
            return [{"number": 10, "task_id": "task-10", "workspace": str(self.workspace)}]

        def close(self):
            pass

    monkeypatch.setattr(cli, "Maestro", FakeMaestro)
    result = cli._project_tasks(project)
    assert [item["workspace"] for item in result] == [str(project.resolve()), str(wt.resolve())]


def test_project_status_finds_unique_task(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    wt = project / ".claude" / "worktrees" / "feature-a"
    (wt / ".maestro").mkdir(parents=True)
    (wt / ".maestro" / "tasks.json").write_text("[]", encoding="utf-8")

    class FakeMaestro:
        def __init__(self, workspace):
            self.workspace = Path(workspace)

        def status(self, task_ref):
            if task_ref == "task-10":
                return {"task_id": task_ref, "workspace": str(self.workspace)}
            raise KeyError(task_ref)

        def close(self):
            pass

    monkeypatch.setattr(cli, "Maestro", FakeMaestro)
    result = cli._project_status(project, "task-10")
    assert result["workspace"] == str(wt.resolve())


def test_main_uses_argument_list_without_executable(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["/fake/.venv/bin/maestro", "task", "list", "--workspace", str(tmp_path)])
    class FakeMaestro:
        def __init__(self, workspace):
            self.workspace = Path(workspace)
        def list_tasks(self):
            return [{"workspace": str(self.workspace), "number": 1}]
        def close(self):
            pass
    monkeypatch.setattr(cli, "Maestro", FakeMaestro)
    assert cli.main() == 0
    assert '"number": 1' in capsys.readouterr().out
