from __future__ import annotations

from pathlib import Path

import pytest

from maestro import cli


def test_version_constant():
    assert cli.VERSION == "0.5.5"


def test_workspace_and_project_helpers(monkeypatch, tmp_path: Path):
    ws = tmp_path / "ws"; ws.mkdir()
    monkeypatch.setenv("MAESTRO_WORKSPACE", str(ws))
    assert cli._workspace(None) == ws.resolve()
    explicit = tmp_path / "explicit"; explicit.mkdir()
    assert cli._workspace(str(explicit)) == explicit.resolve()
    with pytest.raises(ValueError, match="does not exist"):
        cli._project(str(tmp_path / "missing"))
    assert cli._project(str(ws)) == ws.resolve()


def test_normalize_argv_cases():
    assert cli._normalize_argv(["maestro", "task", "10"]) == ["maestro", "task", "status", "10"]
    assert cli._normalize_argv(["maestro", "task", "show", "10"]) == ["maestro", "task", "show", "10"]
    assert cli._normalize_argv(["maestro", "task", "list"]) == ["maestro", "task", "list"]
    assert cli._normalize_argv(["maestro", "status", "10"]) == ["maestro", "status", "10"]
    assert cli._normalize_argv(["maestro", "task", "--help"]) == ["maestro", "task", "--help"]


def test_discovery_filters_and_deduplicates(tmp_path: Path):
    project = tmp_path / "project"; project.mkdir()
    state = project / ".maestro"; state.mkdir(); (state / "memory.db").write_bytes(b"")
    wt_root = project / ".claude" / "worktrees"; wt_root.mkdir(parents=True)
    good = wt_root / "feature"; good.mkdir(); (good / ".maestro").mkdir(); (good / ".maestro" / "tasks.json").write_text("[]")
    empty = wt_root / "empty"; empty.mkdir()
    assert cli._discover_workspaces(project) == [project.resolve(), good.resolve()]


def test_project_tasks_closes_instances(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"; project.mkdir(); (project / ".maestro").mkdir(); (project / ".maestro" / "memory.db").write_bytes(b"")
    closed = []
    class Fake:
        def __init__(self, workspace): self.workspace = Path(workspace)
        def list_tasks(self): return [{"number": 2, "workspace": str(self.workspace)}]
        def close(self): closed.append(str(self.workspace))
    monkeypatch.setattr(cli, "Maestro", Fake)
    assert cli._project_tasks(project)[0]["number"] == 2
    assert closed == [str(project.resolve())]


def test_project_status_not_found_and_ambiguous(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"; project.mkdir();
    for name in ("a", "b"):
        w = project / ".claude" / "worktrees" / name; w.mkdir(parents=True); (w / ".maestro").mkdir(); (w / ".maestro" / "tasks.json").write_text("[]")
    class Fake:
        def __init__(self, workspace): self.workspace = Path(workspace)
        def status(self, ref):
            if ref == "missing": raise KeyError(ref)
            return {"task_id": ref, "workspace": str(self.workspace)}
        def close(self): pass
    monkeypatch.setattr(cli, "Maestro", Fake)
    with pytest.raises(KeyError, match="Unknown task reference"):
        cli._project_status(project, "missing")
    with pytest.raises(KeyError, match="ambiguous"):
        cli._project_status(project, "same")


def test_main_modern_commands(monkeypatch, tmp_path: Path, capsys):
    class Fake:
        def __init__(self, workspace): self.workspace = Path(workspace)
        def list_tasks(self): return [{"number": 1, "workspace": str(self.workspace)}]
        def status(self, task_id): return {"task_id": task_id}
        def close(self): pass
        def codex_defaults(self): return {"model": "m", "effort": "high"}
        def create_handoff(self, *args, **kwargs): return {"task_id": "task-1"}
        def implement_async(self, task_id): return {"task_id": task_id, "pid": 1}
    monkeypatch.setattr(cli, "Maestro", Fake)
    cases = [
        ["maestro", "task", "list", "--workspace", str(tmp_path)],
        ["maestro", "task", "status", "1", "--workspace", str(tmp_path)],
        ["maestro", "task", "show", "1", "--workspace", str(tmp_path)],
        ["maestro", "status", "1", "--workspace", str(tmp_path)],
        ["maestro", "list", "--workspace", str(tmp_path)],
        ["maestro", "config", "--workspace", str(tmp_path)],
        ["maestro", "run", "1", "--workspace", str(tmp_path)],
    ]
    for argv in cases:
        monkeypatch.setattr(cli.sys, "argv", argv)
        assert cli.main() == 0
    out = capsys.readouterr().out
    assert '"task_id": "1"' in out


def test_main_task_without_subcommand_and_project(monkeypatch, tmp_path: Path, capsys):
    class Fake:
        def __init__(self, workspace): self.workspace = workspace
        def close(self): pass
    monkeypatch.setattr(cli, "Maestro", Fake)
    monkeypatch.setattr(cli.sys, "argv", ["maestro", "task"])
    assert cli.main() == 2
    capsys.readouterr()

    monkeypatch.setattr(cli, "_project_tasks", lambda p: [{"number": 1}])
    monkeypatch.setattr(cli.sys, "argv", ["maestro", "task", "list", "--project", str(tmp_path)])
    assert cli.main() == 0
    assert '"number": 1' in capsys.readouterr().out


def test_main_project_status_and_invalid_project_command(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(cli, "_project_status", lambda p, ref: {"task_id": ref})
    monkeypatch.setattr(cli.sys, "argv", ["maestro", "task", "status", "10", "--project", str(tmp_path)])
    assert cli.main() == 0
    assert '"task_id": "10"' in capsys.readouterr().out

    monkeypatch.setattr(cli.sys, "argv", ["maestro", "status", "10", "--project", str(tmp_path)])
    assert cli.main() == 2
    assert "supported with" in capsys.readouterr().err


def test_main_handoff_and_errors(monkeypatch, tmp_path: Path, capsys):
    design = tmp_path / "design.md"; design.write_text("D", encoding="utf-8")
    class Fake:
        def __init__(self, workspace): self.workspace = workspace
        def create_handoff(self, *args, **kwargs): return {"task_id": "t"}
        def implement_async(self, task_id): return {"task_id": task_id}
        def close(self): pass
    monkeypatch.setattr(cli, "Maestro", Fake)
    monkeypatch.setattr(cli.sys, "argv", ["maestro", "handoff", "--title", "T", "--request", "R", "--design-file", str(design), "--workspace", str(tmp_path)])
    assert cli.main() == 0
    assert '"task_id": "t"' in capsys.readouterr().out

    class Bad:
        def __init__(self, workspace): pass
        def status(self, _): raise KeyError("friendly")
        def close(self): pass
    monkeypatch.setattr(cli, "Maestro", Bad)
    monkeypatch.setattr(cli.sys, "argv", ["maestro", "status", "1", "--workspace", str(tmp_path)])
    assert cli.main() == 2
    assert "friendly" in capsys.readouterr().err

    class ValueBad:
        def __init__(self, workspace): raise ValueError("bad value")
    monkeypatch.setattr(cli, "Maestro", ValueBad)
    assert cli.main() == 2
    assert "bad value" in capsys.readouterr().err



def test_discovery_deduplicates_resolved_symlink(tmp_path: Path):
    project = tmp_path / "project"; project.mkdir()
    state = project / ".maestro"; state.mkdir(); (state / "memory.db").write_bytes(b"")
    wt = project / ".claude" / "worktrees"; wt.mkdir(parents=True)
    link = wt / "alias"; link.symlink_to(project, target_is_directory=True)
    assert cli._discover_workspaces(project) == [project.resolve()]


def test_project_status_unique_success(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"; project.mkdir()
    wt = project / ".claude" / "worktrees" / "feature"; wt.mkdir(parents=True)
    (wt / ".maestro").mkdir(); (wt / ".maestro" / "tasks.json").write_text("[]")
    class Fake:
        def __init__(self, workspace): self.workspace = workspace
        def status(self, ref): return {"task_id": ref, "workspace": str(self.workspace)}
        def close(self): pass
    monkeypatch.setattr(cli, "Maestro", Fake)
    assert cli._project_status(project, "task-1")["task_id"] == "task-1"


def test_root_workspace_auto_discovers_worktrees(monkeypatch, tmp_path: Path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    wt = project / ".claude" / "worktrees" / "feature"
    wt.mkdir(parents=True)
    (wt / ".maestro").mkdir()
    (wt / ".maestro" / "tasks.json").write_text("[]")

    monkeypatch.setattr(
        cli,
        "_project_tasks",
        lambda p: [{"workspace": str(p / ".claude" / "worktrees" / "feature"), "number": 1}],
    )
    monkeypatch.setattr(cli.sys, "argv", ["maestro", "task", "list", "--workspace", str(project)])
    assert cli.main() == 0
    assert '"number": 1' in capsys.readouterr().out


def test_root_workspace_auto_discovers_status_and_legacy_list(monkeypatch, tmp_path: Path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    wt = project / ".claude" / "worktrees" / "feature"
    wt.mkdir(parents=True)
    (wt / ".maestro").mkdir()
    (wt / ".maestro" / "tasks.json").write_text("[]")

    monkeypatch.setattr(cli, "_project_status", lambda p, ref: {"task_id": ref, "scope": str(p)})
    monkeypatch.setattr(cli, "_project_tasks", lambda p: [{"scope": str(p), "number": 2}])

    monkeypatch.setattr(cli.sys, "argv", ["maestro", "task", "status", "10", "--workspace", str(project)])
    assert cli.main() == 0
    assert '"task_id": "10"' in capsys.readouterr().out

    monkeypatch.setattr(cli.sys, "argv", ["maestro", "status", "10", "--workspace", str(project)])
    assert cli.main() == 0
    assert '"task_id": "10"' in capsys.readouterr().out

    monkeypatch.setattr(cli.sys, "argv", ["maestro", "list", "--workspace", str(project)])
    assert cli.main() == 0
    assert '"number": 2' in capsys.readouterr().out

def test_non_project_workspace_keeps_direct_maestro_behavior(monkeypatch, tmp_path: Path, capsys):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    class Fake:
        def __init__(self, path): self.path = path
        def list_tasks(self): return [{"workspace": str(self.path), "number": 3}]
        def close(self): pass
    monkeypatch.setattr(cli, "Maestro", Fake)
    monkeypatch.setattr(cli.sys, "argv", ["maestro", "task", "list", "--workspace", str(workspace)])
    assert cli.main() == 0
    assert '"number": 3' in capsys.readouterr().out


def test_project_root_non_task_command_keeps_direct_maestro(monkeypatch, tmp_path: Path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".claude" / "worktrees").mkdir(parents=True)

    class Fake:
        def __init__(self, path): self.path = path
        def codex_defaults(self): return {"model": "root-model", "effort": "high"}
        def close(self): pass

    monkeypatch.setattr(cli, "Maestro", Fake)
    monkeypatch.setattr(cli.sys, "argv", ["maestro", "config", "--workspace", str(project)])
    assert cli.main() == 0
    assert '"model": "root-model"' in capsys.readouterr().out
