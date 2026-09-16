import json
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
