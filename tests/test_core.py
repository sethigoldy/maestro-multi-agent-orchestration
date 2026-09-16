import json
import pytest
from pathlib import Path
from maestro.core import Maestro
from maestro.models import Phase

def test_filesystem_task_lifecycle_and_user_state(tmp_path, monkeypatch):
    home=tmp_path/'home'; home.mkdir(); monkeypatch.setenv('MAESTRO_HOME',str(home))
    ws=tmp_path/'project'; ws.mkdir(); m=Maestro(ws)
    try:
        t=m.create_handoff('Feature','Implement feature','Design',model='gpt-5.6-luna',effort='max')
        assert t['model']=='gpt-5.6-luna' and t['effort']=='max'
        assert m.resolve_task(t['task_id'])==t['task_id']
        s=m.status(t['task_id']); assert s['phase']==Phase.DESIGNED.value and s['model']=='gpt-5.6-luna' and s['effort']=='max'
        assert m.list_tasks()[0]['task_id']==t['task_id']
        state=home/'state.jsonl'; assert state.is_file() and state.read_text()
    finally: m.close()

def test_empty_workspace_home(monkeypatch,tmp_path):
    monkeypatch.setenv('MAESTRO_HOME',str(tmp_path/'home'))
    m=Maestro(tmp_path); m.close()

def test_config_in_project_root(tmp_path):
    (tmp_path/'.maestro').mkdir(); (tmp_path/'.maestro'/'config.toml').write_text('[codex]\nmodel="gpt-5.6-luna"\neffort="max"\n')
    m=Maestro(tmp_path)
    try: assert m.codex_defaults()=={'model':'gpt-5.6-luna','effort':'max'}
    finally: m.close()

def test_task_state_is_user_level_and_survives_worktree_switch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    project = tmp_path / "project"
    project.mkdir()

    worktree_a = project / ".claude" / "worktrees" / "a"
    worktree_b = project / ".claude" / "worktrees" / "b"
    worktree_a.mkdir(parents=True)
    worktree_b.mkdir(parents=True)

    # A minimal fake git repository for this unit-level filesystem test.
    (project / ".git").mkdir()

    m1 = Maestro(worktree_a)
    try:
        task = m1.create_handoff(
            "Test task",
            "Do something",
            "Approved design",
        )
    finally:
        m1.close()

    task_id = task["task_id"]

    assert (home / "registry.json").is_file()
    assert (home / "designs" / f"{task_id}.md").is_file()

    assert not (worktree_a / ".maestro" / "tasks").exists()

    m2 = Maestro(worktree_b)
    try:
        assert m2.resolve_task(task_id) == task_id

        status = m2.status(task_id)
        assert status["workspace"] == str(worktree_a)
    finally:
        m2.close()

def test_task_lock_releases_lock(tmp_path, monkeypatch):
    m = Maestro(tmp_path)
    try:
        with m._task_lock():
            pass

        assert m.lock_path.is_file()
    finally:
        m.close()

def test_task_lock_releases_on_exception(tmp_path):
    m = Maestro(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            with m._task_lock():
                raise RuntimeError("boom")
    finally:
        m.close()

def test_create_handoff_without_model_or_effort(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    m = Maestro(tmp_path)
    try:
        task = m.create_handoff(
            "No defaults",
            "Test missing optional values",
            "Design",
            model=None,
            effort=None,
        )

        assert task["model"] is None
        assert task["effort"] is None

        status = m.status(task["task_id"])
        assert status["model"] is None
        assert status["effort"] is None
    finally:
        m.close()

def test_create_handoff_without_model_or_effort(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".maestro").mkdir()
    (workspace / ".maestro" / "config.toml").write_text(
        "[storage]\nbackend = \"filesystem\"\n"
    )

    m = Maestro(workspace)
    try:
        task = m.create_handoff(
            "No defaults",
            "Test missing optional values",
            "Design",
        )

        assert task["model"] is None
        assert task["effort"] is None
    finally:
        m.close()

def test_create_handoff_accepts_valid_effort(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".maestro").mkdir()
    (workspace / ".maestro" / "config.toml").write_text(
        '[storage]\nbackend="filesystem"\n'
    )

    m = Maestro(workspace)
    try:
        task = m.create_handoff(
            "Valid effort",
            "Test valid effort",
            "Design",
            effort="max",
        )

        assert task["effort"] == "max"
    finally:
        m.close()

@pytest.mark.parametrize("effort", ["max", None])
def test_create_handoff_effort_branches(tmp_path, monkeypatch, effort):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".maestro").mkdir()
    (workspace / ".maestro" / "config.toml").write_text(
        '[storage]\nbackend="filesystem"\n'
    )

    m = Maestro(workspace)
    try:
        task = m.create_handoff(
            "Effort branch",
            "Exercise effort branch",
            "Design",
            effort=effort,
        )

        assert task["effort"] == effort
    finally:
        m.close()

def test_create_handoff_effort_none_branch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.delenv("MAESTRO_CODEX_EFFORT", raising=False)
    monkeypatch.delenv("MAESTRO_CODEX_MODEL", raising=False)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    m = Maestro(workspace)
    try:
        # Force the effective config value to None so the first
        # condition in line 436 short-circuits.
        m.config["effort"] = None

        task = m.create_handoff(
            "No effort",
            "Exercise None effort branch",
            "Design",
            effort=None,
        )

        assert task["effort"] is None
    finally:
        m.close()

def test_create_handoff_rejects_invalid_effort(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".maestro").mkdir()
    (workspace / ".maestro" / "config.toml").write_text(
        '[storage]\nbackend="filesystem"\n'
    )

    m = Maestro(workspace)
    try:
        with pytest.raises(
            ValueError,
            match="Unsupported Codex reasoning effort: bad",
        ):
            m.create_handoff(
                "Invalid effort",
                "Exercise invalid effort branch",
                "Design",
                effort="bad",
            )
    finally:
        m.close()
