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

# ---------------------------------------------------------------- [defaults] routing defaults

def _write_config(root, text):
    (root / '.maestro').mkdir(exist_ok=True)
    (root / '.maestro' / 'config.toml').write_text(text, encoding='utf-8')

def test_defaults_absent_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    m = Maestro(tmp_path)
    try: assert m.config['defaults'] == {}
    finally: m.close()

def test_defaults_full_table_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    _write_config(tmp_path, '[defaults]\nagent = "codex"\nfallback = ["claude_code", "opencode"]\nmodel = "gpt-5.6-luna"\neffort = "max"\n')
    m = Maestro(tmp_path)
    try: assert m.config['defaults'] == {'agent': 'codex', 'fallback': ['claude_code', 'opencode'], 'model': 'gpt-5.6-luna', 'effort': 'max'}
    finally: m.close()

def test_defaults_partial_table(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    _write_config(tmp_path, '[defaults]\nagent = "codex"\n')
    m = Maestro(tmp_path)
    try: assert m.config['defaults'] == {'agent': 'codex'}
    finally: m.close()

def test_defaults_not_a_table_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    _write_config(tmp_path, 'defaults = "codex"\n')
    with pytest.raises(ValueError, match=r'\[defaults\] must be a table'):
        Maestro(tmp_path)

def test_defaults_unknown_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    _write_config(tmp_path, '[defaults]\nagent = "codex"\nbogus = 1\n')
    with pytest.raises(ValueError, match='unknown keys: bogus'):
        Maestro(tmp_path)

def test_defaults_empty_agent_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    _write_config(tmp_path, '[defaults]\nagent = "   "\n')
    with pytest.raises(ValueError, match='agent must be a non-empty string'):
        Maestro(tmp_path)

def test_defaults_non_string_model_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    _write_config(tmp_path, '[defaults]\nmodel = 42\n')
    with pytest.raises(ValueError, match='model must be a non-empty string'):
        Maestro(tmp_path)

@pytest.mark.parametrize('bad,match', [
    ('fallback = "codex"', 'fallback must be a list'),
    ('fallback = ["codex", 1]', 'fallback must be a list'),
    ('fallback = [""]', 'fallback must be a list'),
])
def test_defaults_bad_fallback_rejected(tmp_path, monkeypatch, bad, match):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    _write_config(tmp_path, f'[defaults]\n{bad}\n')
    with pytest.raises(ValueError, match=match):
        Maestro(tmp_path)

def test_defaults_bad_effort_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'home'))
    _write_config(tmp_path, '[defaults]\neffort = "turbo"\n')
    with pytest.raises(ValueError, match='Unsupported default reasoning effort'):
        Maestro(tmp_path)

def test_defaults_later_file_wins_per_key(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    (home / 'config.toml').write_text('[defaults]\nagent = "codex"\nmodel = "user-model"\n', encoding='utf-8')
    _write_config(tmp_path, '[defaults]\nagent = "opencode"\n')
    monkeypatch.setenv('MAESTRO_HOME', str(home))
    m = Maestro(tmp_path)
    try: assert m.config['defaults'] == {'agent': 'opencode', 'model': 'user-model'}
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


def test_status_shows_the_question_a_parked_task_waits_on(tmp_path, monkeypatch):
    # The phase claim maps input-required to REVIEWING, the same phase a
    # finished task has. A parked task's status must say it is waiting, what it
    # waits for, and the question, so a CLI user can answer it.
    import json
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path/'home'))
    m = Maestro(tmp_path)
    try:
        tid = _seed_task(m)
        runtime = {"state": "input-required", "awaiting": "routing", "question": "Which agent should run this task?"}
        m._write_claim(tid, 'task_runtime', json.dumps(runtime))
        s = m.status(tid)
        assert s['state'] == 'input-required'
        assert s['awaiting'] == 'routing'
        assert s['question'] == 'Which agent should run this task?'
    finally:
        m.close()


def test_status_of_a_task_that_is_not_parked_has_no_question(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path/'home'))
    m = Maestro(tmp_path)
    try:
        tid = _seed_task(m)
        m._write_claim(tid, 'task_runtime', json.dumps({"state": "completed"}))
        s = m.status(tid)
        assert 'question' not in s and 'awaiting' not in s
    finally:
        m.close()


def test_status_ignores_an_unreadable_runtime_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path/'home'))
    m = Maestro(tmp_path)
    try:
        tid = _seed_task(m)
        m._write_claim(tid, 'task_runtime', '{not json')
        assert 'question' not in m.status(tid)
        m._write_claim(tid, 'task_runtime', '["not", "an", "object"]')
        assert 'question' not in m.status(tid)
    finally:
        m.close()


def test_verification_timeout_setting(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('MAESTRO_HOME', str(home))
    def load(text):
        (home / 'config.toml').write_text(text, encoding='utf-8')
        m = Maestro(tmp_path)
        try:
            return m.config['verification_timeout_s']
        finally:
            m.close()
    assert load('') == 1800  # 30 minutes by default
    assert load('[verification]\ntimeout_s = 600\n') == 600
    assert load('[verification]\ntimeout_s = 0\n') == 0  # no time limit
    for bad in ('-1', '"600"', 'true', '1.5'):
        with pytest.raises(ValueError, match="timeout_s must be a whole number of seconds"):
            load(f'[verification]\ntimeout_s = {bad}\n')


def test_status_always_reports_the_task_state(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path/'home'))
    m = Maestro(tmp_path)
    try:
        tid = _seed_task(m)
        assert 'state' not in m.status(tid)  # nothing recorded yet
        m._write_claim(tid, 'task_runtime', json.dumps({"state": "working"}))
        assert m.status(tid)['state'] == 'working'
    finally:
        m.close()


def test_defaults_max_parallel_parsed_and_validated():
    assert Maestro._parse_defaults({"max_parallel": 2}) == {"max_parallel": 2}
    assert Maestro._parse_defaults({}) == {}
    for bad in (0, -1, "4", 2.5, True):
        with pytest.raises(ValueError, match="max_parallel must be a whole number of at least 1"):
            Maestro._parse_defaults({"max_parallel": bad})


def test_status_reports_run_dir_and_defaults_to_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path/'home'))
    m = Maestro(tmp_path)
    try:
        tid = _seed_task(m)
        s = m.status(tid)
        # A task from before run directories existed ran in its workspace.
        assert s['run_dir'] == s['workspace'] and s['run_dir_kind'] == 'workspace'
        assert 'run_dir_removed' not in s
        m._write_claim(tid, 'task_run_dir', '/home/.maestro/worktrees/x')
        m._write_claim(tid, 'task_run_dir_kind', 'worktree')
        m._write_claim(tid, 'task_run_dir_removed', 'false')
        assert 'run_dir_removed' not in m.status(tid)
        m._write_claim(tid, 'task_run_dir_removed', 'true')
        s = m.status(tid)
        assert s['run_dir'] == '/home/.maestro/worktrees/x' and s['run_dir_kind'] == 'worktree' and s['run_dir_removed'] is True
    finally:
        m.close()


def test_status_says_when_a_worktree_run_dir_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path/'home'))
    m = Maestro(tmp_path)
    try:
        tid = _seed_task(m)
        wt = tmp_path / 'wt'
        m._write_claim(tid, 'task_run_dir', str(wt))
        m._write_claim(tid, 'task_run_dir_kind', 'worktree')
        assert m.status(tid)['run_dir_missing'] is True  # deleted by hand, for example
        wt.mkdir()
        assert 'run_dir_missing' not in m.status(tid)
        wt.rmdir()
        m._write_claim(tid, 'task_run_dir_removed', 'true')
        s = m.status(tid)
        assert s['run_dir_removed'] is True and 'run_dir_missing' not in s  # removed on purpose
    finally:
        m.close()
