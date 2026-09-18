import pytest
from maestro.core import Maestro
from maestro.models import Phase


def _seed_task(m, task_id="task-20260101-000000-abcd12", title="Feature"):
    """Register a task the way the daemon does (claims + registry record)."""
    number = m._new_task_number()
    episode = m.mem.add(f"Approved design handoff. Task: {task_id}. Title: {title}", role="system", ts=m._now())
    m._register_task(task_id, title, number)
    for pred, val in (("task_status", Phase.DESIGNED.value), ("task_workspace", str(m.root)), ("task_number", str(number)), ("task_title", title)):
        m._write_claim(task_id, pred, val, episode.episode_ids)
    return task_id


def test_filesystem_task_lifecycle_and_user_state(tmp_path, monkeypatch):
    home=tmp_path/'home'; home.mkdir(); monkeypatch.setenv('MAESTRO_HOME',str(home))
    ws=tmp_path/'project'; ws.mkdir(); m=Maestro(ws)
    try:
        tid=_seed_task(m)
        m._write_claim(tid,'task_model','gpt-5.6-luna'); m._write_claim(tid,'task_effort','max')
        assert m.resolve_task(tid)==tid
        s=m.status(tid); assert s['phase']==Phase.DESIGNED.value and s['model']=='gpt-5.6-luna' and s['effort']=='max'
        assert m.list_tasks()[0]['task_id']==tid
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
        task_id = _seed_task(m1, task_id="task-20260101-000000-wt0001", title="Test task")
    finally:
        m1.close()

    assert (home / "registry.json").is_file()

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
