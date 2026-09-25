import sys
import json, os, subprocess
from pathlib import Path
import pytest
from maestro.core import Maestro, maestro_user_dir
from maestro.cli import _normalize_argv, _scope_for_list

def git_repo(tmp):
    project=tmp/'project'; project.mkdir();
    subprocess.run(['git','init','-q'],cwd=project,check=True)
    subprocess.run(['git','config','user.email','t@e'],cwd=project,check=True)
    subprocess.run(['git','config','user.name','T'],cwd=project,check=True)
    (project/'README').write_text('x')
    subprocess.run(['git','add','README'],cwd=project,check=True)
    subprocess.run(['git','commit','-qm','init'],cwd=project,check=True)
    subprocess.run(['git','worktree','add','-q',str(tmp/'w1'),'-b','one'],cwd=project,check=True)
    subprocess.run(['git','worktree','add','-q',str(tmp/'w2'),'-b','two'],cwd=project,check=True)
    return project,tmp/'w1',tmp/'w2'

def env(tmp, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp/'home'))

def _seed(m, title="A"):
    """Register a task the way the daemon does (claims + registry record)."""
    import uuid as _uuid
    tid = f"task-seed-{_uuid.uuid4().hex[:10]}"
    number = m._new_task_number()
    episode = m.mem.add(f"Approved design handoff. Task: {tid}. Title: {title}", role="system", ts=m._now())
    m._register_task(tid, title, number)
    for pred, val in (("task_status", "DESIGNED"), ("task_workspace", str(m.root)), ("task_number", str(number)), ("task_title", title)):
        m._write_claim(tid, pred, val, episode.episode_ids)
    return {"task_id": tid, "task_number": number}

def test_user_scope_cross_worktree(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch); project,w1,w2=git_repo(tmp_path)
    a=Maestro(w1); t1=_seed(a); a.close()
    b=Maestro(w2); t2=_seed(b,'B'); assert [x['task_id'] for x in b.list_tasks(project_filter=project)] == [t1['task_id'], t2['task_id']]; b.close()

def test_global_status_from_other_worktree(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch); _,w1,w2=git_repo(tmp_path)
    a=Maestro(w1); t=_seed(a); a.close()
    b=Maestro(w2); s=b.status(t['task_id']); assert s['workspace']==str(w1); b.close()

def test_project_filter_and_workspace_filter(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch); project,w1,w2=git_repo(tmp_path)
    a=Maestro(w1); t1=_seed(a); a.close(); b=Maestro(w2); t2=_seed(b,'B')
    assert len(b.list_tasks(workspace_filter=str(w2)))==1 and b.list_tasks(workspace_filter=str(w2))[0]['task_id']==t2['task_id']
    assert len(b.list_tasks(project_filter=str(project)))==2; b.close()

def test_daemon_rooted_registry_record_scopes_to_task_project(tmp_path, monkeypatch):
    # The daemon roots its Maestro at the state directory but registers tasks
    # for other workspaces: the registry record must carry the TASK workspace's
    # project root, or `task list --workspace/--project` never matches (regression).
    env(tmp_path, monkeypatch); project,w1,_=git_repo(tmp_path)
    home=tmp_path/'home'; home.mkdir()
    m=Maestro(home)  # root = state dir, exactly like MaestroDaemon
    try:
        tid='task-20260101-000000-scoped'
        m._register_task(tid,'Scoped task',1,project_root=str(Maestro._resolve_project_root(w1)))
        m._write_claim(tid,'task_workspace',str(w1))
        assert m.status(tid)['project_root']==str(project)
        assert len(m.list_tasks(project_filter=str(project)))==1
        assert len(m.list_tasks(workspace_filter=str(w1)))==1
    finally:
        m.close()

def test_unique_numbers_across_worktrees(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch); _,w1,w2=git_repo(tmp_path)
    a=Maestro(w1); n1=_seed(a)['task_number']; a.close(); b=Maestro(w2); n2=_seed(b,'B')['task_number']; b.close(); assert (n1,n2)==(1,2)

def test_home_override(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path/'custom')); assert maestro_user_dir()==(tmp_path/'custom').resolve()

def test_project_state_migration(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch); project,w1,_=git_repo(tmp_path)
    state=project/'.maestro'; state.mkdir()
    events=[
      {'kind':'registry','record':{'number':7,'task_id':'task-20260916-120000-abcdef','title':'old','created_at':'2026-09-16T12:00:00+00:00','workspace':str(w1)}},
      {'kind':'claim','task_id':'task-20260916-120000-abcdef','predicate':'task_status','value':'REVIEWING'},
      {'kind':'claim','task_id':'task-20260916-120000-abcdef','predicate':'task_owner','value':'claude'},
      {'kind':'claim','task_id':'task-20260916-120000-abcdef','predicate':'task_implementer','value':'codex'},
      {'kind':'claim','task_id':'task-20260916-120000-abcdef','predicate':'task_workspace','value':str(w1)},
    ]
    (state/'project-state.jsonl').write_text('\n'.join(json.dumps(x) for x in events)+'\n')
    m=Maestro(w1); s=m.status('task-20260916-120000-abcdef'); assert s['phase']=='REVIEWING' and s['workspace']==str(w1); assert s['task_number']==1 and s['task_id'].startswith('task-'); m.close()
    assert (tmp_path/'home'/'migrations').is_dir()

def test_migration_idempotent(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch); project,w1,_=git_repo(tmp_path); d=project/'.maestro'; d.mkdir();
    tid='task-20260916-120000-aabbcc'; (d/'project-state.jsonl').write_text(json.dumps({'kind':'registry','record':{'number':4,'task_id':tid,'title':'x','workspace':str(w1)}})+'\n')
    m=Maestro(w1); first=m.list_tasks(project_filter=project); m.close(); n=Maestro(w1); second=n.list_tasks(project_filter=project); n.close(); assert len(first)==len(second)==1

def test_normalize_short_form():
    assert _normalize_argv(['task','abc'])==['task','status','abc']; assert _normalize_argv(['--workspace','/repo','task','abc'])==['--workspace','/repo','task','status','abc']; assert _normalize_argv(['task','list'])==['task','list']
    # 'continue' is a real subcommand, not a bare task id: it must not be rewritten to status.
    assert _normalize_argv(['task','continue','task-1','--request','x'])==['task','continue','task-1','--request','x']

def test_default_user_home_and_windows(monkeypatch, tmp_path):
    from maestro import core
    monkeypatch.delenv('MAESTRO_HOME', raising=False); monkeypatch.setattr(core.sys,'platform','linux'); monkeypatch.setattr(core.Path,'home',lambda:tmp_path); assert core.maestro_user_dir()==tmp_path/'.maestro'
    monkeypatch.setattr(core.sys,'platform','win32'); monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local')); assert core.maestro_user_dir()==tmp_path/'local'/'Maestro'
    monkeypatch.delenv('LOCALAPPDATA'); assert core.maestro_user_dir()==tmp_path/'AppData'/'Local'/'Maestro'

def test_file_state_corruption_and_episode(tmp_path):
    from maestro.core import _FileState
    st=_FileState(tmp_path/'s'); st.path.write_text('{bad}\n{}\n'+json.dumps({'subject':'s','predicate':'p','object':'o','episode_ids':['e']})+'\n')
    assert len(st.history('s','p'))==1; assert st.get_all()[0].object=='o'; ep=st.add('x','user'); assert len(ep.episode_ids)==1; st.close()
    st.path.unlink(); assert st.get_all()==[]

def test_git_helpers_and_bad_workspace(tmp_path):
    with pytest.raises(ValueError): Maestro(tmp_path/'missing')
    with pytest.raises(ValueError): Maestro.git_root(tmp_path)
    project=tmp_path/'p'; project.mkdir();
    subprocess.run(['git','init','-q'],cwd=project,check=True); subprocess.run(['git','config','user.email','t@e'],cwd=project,check=True); subprocess.run(['git','config','user.name','t'],cwd=project,check=True); (project/'x').write_text('x'); subprocess.run(['git','add','x'],cwd=project,check=True); subprocess.run(['git','commit','-qm','x'],cwd=project,check=True)
    assert Maestro.git_root(project)==project.resolve(); m=Maestro(project); assert m.project_root==project.resolve(); m.close()

def test_config_user_workspace_env_and_invalid(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); project,w1,_=git_repo(tmp_path); home=tmp_path/'home'; home.mkdir(); (w1/'.maestro').mkdir(exist_ok=True); (home/'config.toml').write_text('[codex]\nmodel="u"\neffort="max"\n[verification]\ncommand=["echo","ok"]\n[storage]\nbackend="filesystem"\n'); (w1/'.maestro'/'config.toml').write_text('[codex]\nmodel="w"\n'); m=Maestro(w1); assert m.codex_defaults()=={'model':'w','effort':'max'}; assert m.config['verification_command']==['echo','ok']; m.close()
    (w1/'.maestro'/'config.toml').write_text('[codex]\neffort="bad"');
    with pytest.raises(ValueError): Maestro(w1)
    (w1/'.maestro'/'config.toml').write_text('[storage]\nbackend="nope"');
    with pytest.raises(ValueError): Maestro(w1)

def test_registry_corrupt_and_unknown(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); project,w1,_=git_repo(tmp_path); home=tmp_path/'home'; home.mkdir(); (home/'registry.json').write_text('{bad'); m=Maestro(w1); assert m.list_tasks()==[]
    with pytest.raises(KeyError): m.resolve_task('999');
    with pytest.raises(KeyError): m.resolve_task('bogus')
    m.close()

def test_status_workspace_fallback(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); m=Maestro(w1); t=_seed(m,'x')
    tid=t['task_id']
    # registry-record fallback when the task_workspace claim is blank
    m._write_claim(tid,'task_workspace','')
    st=m.status(tid); assert st['workspace']==str(w1)
    m.close()

def test_model_effort_and_storage_memvara_error(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    project, w1, _ = git_repo(tmp_path)

    (w1 / ".maestro").mkdir(exist_ok=True)
    (w1 / ".maestro" / "config.toml").write_text(
        '[storage]\nbackend="memvara"\n'
    )

    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "memvara":
            raise ImportError("memvara intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    with pytest.raises(
        RuntimeError,
        match="Memvara backend requested",
    ):
        Maestro(w1)

def test_legacy_memvara_migration(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); project,w1,_=git_repo(tmp_path)
    # Fake legacy Memvara module and DB adapter to exercise the migration boundary.
    import types, sys as pysys
    class C:
        def __init__(self,*a,**k): self.claims=[]
        def get_all(self): return [types.SimpleNamespace(subject='maestro:registry',predicate='task',object=json.dumps({'number':1,'task_id':'task-legacy-a','title':'L'}))]
        def close(self): pass
        def remember(self,*a,**k): pass
    mod=types.ModuleType('memvara'); mod.Memvara=C; mod.NullLLM=lambda:None; pysys.modules['memvara']=mod
    m = Maestro(w1)
    legacy_dir = w1 / ".maestro"
    legacy_dir.mkdir(exist_ok=True)
    (legacy_dir / "memory.db").write_text("x")
    # Recreate after legacy DB exists so migration executes.
    m.close(); m=Maestro(w1); result=m.migrate_legacy_memvara(); assert result['migrated']; m.close()

def test_lock_context_and_windows_fallback(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); m=Maestro(w1)
    with m._task_lock(): pass
    # Exercise missing fcntl import branches.
    import builtins
    orig=builtins.__import__
    def imp(name,*a,**k):
        if name=='fcntl': raise ImportError('x')
        return orig(name,*a,**k)
    monkeypatch.setattr(builtins,'__import__',imp)
    with m._task_lock(): pass
    m.close()

def test_fake_memvara_backend(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); (w1/'.maestro').mkdir(exist_ok=True)
    import types, sys
    class FakeClient:
        def __init__(self,*args,**kwargs): self.data=[]
        def remember(self,*args,**kwargs): self.data.append(types.SimpleNamespace(subject=args[0],predicate=args[1],object=args[2]))
        def history(self,s,p): return [x for x in self.data if x.subject==s and x.predicate==p]
        def get_all(self): return list(self.data)
        def add(self,*args,**kwargs): return types.SimpleNamespace(episode_ids=['e'])
        def close(self): pass
    mod=types.ModuleType('memvara'); mod.Memvara=FakeClient; mod.NullLLM=lambda:None; monkeypatch.setitem(sys.modules,'memvara',mod)
    (w1/'.maestro/config.toml').write_text('[storage]\nbackend="memvara"\n')
    m=Maestro(w1); t=_seed(m,'M'); assert m.status(t['task_id'])['phase']=='DESIGNED'; assert len(m.list_tasks())==1; m.close()

def test_resolve_project_fallback_and_status_artifact_fallback(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); plain=tmp_path/'plain'; plain.mkdir(); m=Maestro(plain); assert m.project_root==plain.resolve(); m.close()
    _,w1,_=git_repo(tmp_path); m=Maestro(w1); t=_seed(m,'x'); tid=t['task_id'];
    # Replace the workspace claim with blank in a fake in-memory history by suppressing it in status.
    orig=m._claims
    def claims_without_workspace(tid2):
        d=orig(tid2); d.pop('task_workspace',None); return d
    monkeypatch.setattr(m,'_claims',claims_without_workspace); s=m.status(tid); assert s['workspace']==str(w1); m.close()

def test_config_and_file_error_branches(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); (w1/'.maestro').mkdir(exist_ok=True); bad=w1/'.maestro/config.toml'; bad.write_text('not = [valid'); m=Maestro(w1); m.close(); bad.write_text('[verification]\ncommand=3');
    with pytest.raises(ValueError,match='Verification command'): Maestro(w1)
    bad.write_text('[codex]\neffort="bad"');
    with pytest.raises(ValueError,match='reasoning effort'): Maestro(w1)

def test_legacy_journal_malformed_and_existing(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); project,w1,_=git_repo(tmp_path); d=project/'.maestro'; d.mkdir(); tid='task-20260916-120000-zzzzzz';
    lines=['{bad}', json.dumps({'kind':'claim','task_id':'bad','predicate':'x','value':'y'}), json.dumps({'kind':'registry','record':{'task_id':tid,'number':'x'}}), json.dumps({'kind':'claim','task_id':tid,'predicate':'task_title','value':'Old'})]
    (d/'project-state.jsonl').write_text('\n'.join(lines)); m=Maestro(w1); assert len(m.list_tasks(project_filter=project))==1; m.close()
    # marker makes the second open a no-op
    m=Maestro(w1); assert len(m.list_tasks(project_filter=project))==1; m.close()

def test_legacy_memvara_strict_failure(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); (w1/'.maestro').mkdir(exist_ok=True); (w1/'.maestro/memory.db').write_text('x')
    import types,sys
    mod=types.ModuleType('memvara')
    class Bad:
        def __init__(self,*a,**k): raise RuntimeError('broken')
    mod.Memvara=Bad; mod.NullLLM=lambda:None; monkeypatch.setitem(sys.modules,'memvara',mod)
    m=Maestro(w1)
    with pytest.raises(RuntimeError,match='Unable to migrate'):
        m.migrate_legacy_memvara()
    m.close()

def test_cli_main_commands(tmp_path, monkeypatch, capsys):
    env(tmp_path,monkeypatch); project,w1,w2=git_repo(tmp_path); m=Maestro(w1); t=_seed(m,'CLI'); m.close()
    import maestro.cli as cli
    def run(argv): monkeypatch.setattr(sys,'argv',['maestro',*argv]); return cli.main()
    with pytest.raises(SystemExit) as exc: run(['--version'])
    assert exc.value.code == 0
    assert run(['task','list','--project',str(project)])==0
    assert run(['task',t['task_id'],'--workspace',str(w1)])==0
    assert run(['task','status',t['task_id'],'--workspace',str(w1)])==0
    assert run(['task','show',t['task_id'],'--workspace',str(w1)])==0
    assert run(['status',t['task_id'],'--workspace',str(w1)])==0
    assert run(['list','--project',str(project)])==0
    assert run(['config','--workspace',str(w1)])==0
    with pytest.raises(SystemExit) as exc: run(['task','-h'])
    assert exc.value.code == 0
    out=capsys.readouterr().out; assert t['task_id'] in out

def test_cli_storage_and_errors(monkeypatch,tmp_path,capsys):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); import maestro.cli as cli
    monkeypatch.setattr(sys,'argv',['maestro','storage','migrate-memvara','--workspace',str(w1)]); assert cli.main()==0
    monkeypatch.setattr(sys,'argv',['maestro','task']); assert cli.main()==2
    monkeypatch.setattr(sys,'argv',['maestro','task','status','bad','--workspace',str(w1)]); assert cli.main()==2
    with pytest.raises(SystemExit):
        monkeypatch.setattr(sys,'argv',['maestro','--version']); cli.main()

def test_worker_branches(tmp_path,monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path)
    import maestro.worker as worker
    assert worker._verification_command(w1,['echo','x'])[0]==['echo','x'];
    (w1/'package.json').write_text(json.dumps({'scripts':{'test':'x'}})); assert worker._verification_command(w1)[0][0]=='npm'; (w1/'package.json').unlink(); (w1/'go.mod').write_text('module x'); assert worker._verification_command(w1)[0][0]=='go'; (w1/'go.mod').unlink(); (w1/'Cargo.toml').write_text('[package]'); assert worker._verification_command(w1)[0][0]=='cargo'; (w1/'Cargo.toml').unlink()
    (w1/'pyproject.toml').write_text(''); monkeypatch.setattr(worker.subprocess,'run',lambda *a,**k:type('R',(),{'returncode':1,'stdout':'','stderr':''})()); cmd,note=worker._verification_command(w1); assert cmd[:2]==['git','diff'] and note

def test_cli_helpers_extra(tmp_path, monkeypatch):
    import maestro.cli as cli
    with pytest.raises(ValueError): cli._project(str(tmp_path/'missing'))
    monkeypatch.delenv('MAESTRO_WORKSPACE',raising=False); assert cli._workspace(None)==Path.cwd().resolve()
    project,w1,_=git_repo(tmp_path); monkeypatch.setenv('MAESTRO_WORKSPACE',str(project)); ns=type('N',(),{})(); ns.project=None; ns.workspace=None; base,scope=cli._scope_for_list(ns); assert base==project.resolve(); assert scope==str(project.resolve())
    ns.workspace=str(w1); monkeypatch.delenv('MAESTRO_WORKSPACE',raising=False); base,scope=cli._scope_for_list(ns); assert scope==str(w1.resolve())
    tasks=[{'project_root':str(project),'workspace':str(w1)},{'project_root':'x','workspace':'y'}]; assert len(cli._filter_tasks(tasks,project_root=str(project)))==1; assert len(cli._filter_tasks(tasks,workspace=str(w1)))==1; assert len(cli._filter_tasks(tasks))==2

def test_cli_exception_paths(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); import maestro.cli as cli
    monkeypatch.setattr(cli.Path,'read_text',lambda *a,**k: (_ for _ in ()).throw(OSError('bad')))
    monkeypatch.setattr(sys,'argv',['maestro','delegate','--title','t','--request','r','--target','codex','--design-file',str(w1/'d'),'--workspace',str(w1)])
    assert cli.main()==2

def test_worker_more_branches(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path)
    import maestro.worker as worker
    monkeypatch.setenv('MAESTRO_PYTHON',str(w1/'missing')); (w1/'.venv/bin').mkdir(parents=True); (w1/'.venv/bin/python').write_text('x'); assert worker._python_executable(w1)==sys.executable
    (w1/'.venv/bin/python').unlink(); (w1/'.venv/bin/python').write_text('x'); os.chmod(w1/'.venv/bin/python',0o755); assert worker._python_executable(w1)==str(w1/'.venv/bin/python')
    (w1/'package.json').write_text('{bad'); assert worker._verification_command(w1)[0][0]=='git'; (w1/'package.json').write_text(json.dumps({'scripts':{}})); assert worker._verification_command(w1)[0][0]=='git';
    (w1/'package.json').write_text(json.dumps({'scripts':{'test':'x'}})); (w1/'pnpm-lock.yaml').write_text(''); assert worker._verification_command(w1)[0]==['pnpm','test']; (w1/'pnpm-lock.yaml').unlink(); (w1/'yarn.lock').write_text(''); assert worker._verification_command(w1)[0]==['yarn','test']; (w1/'yarn.lock').unlink(); (w1/'package.json').unlink()
    class R: returncode=1; stdout=''; stderr=''
    monkeypatch.setattr(worker.subprocess,'run',lambda *a,**k:R()); (w1/'pyproject.toml').write_text(''); assert worker._verification_command(w1)[1]

def test_mcp_server_with_fake_fastmcp(tmp_path, monkeypatch):
    import types,sys
    class FakeFast:
        def __init__(self,*a,**k): pass
        def tool(self): return lambda f:f
        def run(self): return None
    pkg=types.ModuleType('mcp'); server=types.ModuleType('mcp.server'); fast=types.ModuleType('mcp.server.fastmcp'); fast.FastMCP=FakeFast; server.fastmcp=fast; pkg.server=server
    monkeypatch.setitem(sys.modules,'mcp',pkg); monkeypatch.setitem(sys.modules,'mcp.server',server); monkeypatch.setitem(sys.modules,'mcp.server.fastmcp',fast)
    import importlib; mod=importlib.reload(importlib.import_module('maestro.mcp_server'))
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); m=Maestro(w1); t=_seed(m,'M');
    assert t['task_id'] in mod.task_status(str(w1),t['task_id']); assert t['task_id'] in mod.list_tasks(str(w1)); m.close()

def test_cli_all_list_scopes_and_helpers(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); project,w1,_=git_repo(tmp_path); import maestro.cli as cli
    ns=type('N',(),{})(); ns.project=None; ns.workspace=None; monkeypatch.delenv('MAESTRO_WORKSPACE',raising=False); base,scope=cli._scope_for_list(ns); assert scope is None
    m=Maestro(w1); _seed(m,'x'); m.close()
    monkeypatch.setenv('MAESTRO_WORKSPACE',str(project)); monkeypatch.setattr(sys,'argv',['maestro','task','list']); assert cli.main()==0
    monkeypatch.setattr(sys,'argv',['maestro','list','--workspace',str(w1)]); assert cli.main()==0
    monkeypatch.setattr(sys,'argv',['maestro','config','--project',str(project)]); assert cli.main()==0

def test_cli_normalize_second_shape_and_filter_miss():
    import maestro.cli as cli
    # main() strips the program name, so the shorthand is found after top-level options.
    assert cli._normalize_argv(['--project','/p','task','abc'])==['--project','/p','task','status','abc']
    assert cli._normalize_argv(['task','list'])==['task','list']
    assert cli._filter_tasks([{'project_root':'p','workspace':'w'}],project_root='x')==[]

def test_core_legacy_branches_and_artifact_fallback(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); project,w1,_=git_repo(tmp_path); d=project/'.maestro'; d.mkdir();
    # Non-dict and registry-like malformed events exercise parser branches.
    tid='task-20260916-000000-abc123'; events=[json.dumps([]),json.dumps({'kind':'registry','record':{'task_id':tid,'number':1,'title':'X','workspace':str(w1)}}),json.dumps({'kind':'claim','task_id':tid,'predicate':'task_status','value':'DESIGNED'}),json.dumps({'kind':'claim','task_id':tid,'predicate':'task_title','value':'X'})]
    (d/'project-state.jsonl').write_text('\n'.join(events)); m=Maestro(w1); assert m.status(tid)['phase']=='DESIGNED'; m.close()
    # No journal read and marker path branches.
    empty=tmp_path/'empty'; empty.mkdir(); m=Maestro(empty); m.close()

def test_worker_remaining_paths(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); import maestro.worker as worker
    (w1/'.venv/bin').mkdir(parents=True); py=w1/'.venv/bin/python'; py.write_text(''); os.chmod(py,0o755); monkeypatch.setenv('MAESTRO_PYTHON',str(py)); assert worker._python_executable(w1)==str(py)
    (w1/'Makefile').write_text('check:\n\techo ok\n'); assert worker._verification_command(w1)[0]==['make','check']; (w1/'Makefile').unlink()
    (w1/'pyproject.toml').write_text('')
    class OK: returncode=0; stdout=''; stderr=''
    monkeypatch.setattr(worker.subprocess,'run',lambda *a,**k:OK()); assert worker._verification_command(w1)[0][1:] == ['-m','pytest']

def test_core_edge_branches(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); project,w1,_=git_repo(tmp_path); import maestro.core as core
    # _resolve_project_root alternate common-dir path and git-root fallback.
    class R: returncode=0; stdout=str(tmp_path/'not-dotgit'); stderr=''
    monkeypatch.setattr(core.subprocess,'run',lambda *a,**k:R()); monkeypatch.setattr(Maestro,'git_root',staticmethod(lambda w: Path(w))); assert Maestro._resolve_project_root(w1)==w1
    # Config loader dict guard and string verification command.
    cfg=w1/'.maestro'; cfg.mkdir(exist_ok=True); (cfg/'config.toml').write_text('[verification]\ncommand="echo ok"\n')
    real_load=core.tomllib.load; monkeypatch.setattr(core.tomllib,'load',lambda fh: []); m=Maestro(w1); m.close(); monkeypatch.setattr(core.tomllib,'load',real_load)
    m=Maestro(w1); assert m.config['verification_command']==['echo','ok']; m.close()
    # Corrupt registry claims and Memvara-style malformed registry values.
    class Claim: 
        def __init__(self,s,p,o): self.subject=s; self.predicate=p; self.object=o
    orig_cfg=w1/'.maestro/config.toml'; orig_cfg.write_text('[storage]\nbackend="memvara"\n')
    import types,sys
    class C:
        def __init__(self,*a,**k): self.data=[Claim('maestro:registry','task','not-json'), Claim('maestro:registry','task',json.dumps({'task_id':'x','number':'bad'})), Claim('maestro:registry','task',json.dumps({'task_id':'ok','number':1}))]
        def history(self,s,p): return [c for c in self.data if c.subject==s and c.predicate==p]
        def get_all(self): return self.data
        def remember(self,*a,**k): self.data.append(Claim(a[0],a[1],a[2]))
        def add(self,*a,**k): return types.SimpleNamespace(episode_ids=['e'])
        def close(self): pass
    mod=types.ModuleType('memvara'); mod.Memvara=C; mod.NullLLM=lambda:None; monkeypatch.setitem(sys.modules,'memvara',mod); m=Maestro(w1); assert m._registry_records()[0]['task_id']=='ok'; m.close()

def test_legacy_memvara_private_branches(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); import types,sys
    (w1/'.maestro').mkdir(exist_ok=True); (w1/'.maestro/memory.db').write_text('x')
    class Bad:
        def __init__(self,*a,**k): raise RuntimeError('bad')
    mod=types.ModuleType('memvara'); mod.Memvara=Bad; mod.NullLLM=lambda:None; monkeypatch.setitem(sys.modules,'memvara',mod)
    m=Maestro(w1)
    # Config is filesystem by default, so soft failure is swallowed.
    assert m._migrate_legacy_memvara(False)==0
    with pytest.raises(RuntimeError): m._migrate_legacy_memvara(True)
    m.close()

def test_claim_fallback_and_status_artifacts(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); _,w1,_=git_repo(tmp_path); m=Maestro(w1); t=_seed(m,'x'); tid=t['task_id']
    # Remove index workspace and task workspace claim; result path supplies workspace.
    idx=m._load_index(); idx[0]['workspace']=None; idx[0]['project_root']=None; m._save_index(idx)
    m.mem.remember(m._subject(tid),'task_workspace','')
    m.mem.remember(m._subject(tid),'task_result',str(w1/'.maestro/tasks' / tid / 'result.json'))
    s=m.status(tid); assert s['workspace']==str(w1)
    # Invalid numeric claim is tolerated.
    m.mem.remember(m._subject(tid),'task_number','bad'); assert isinstance(m.status(tid)['task_number'],int)
    # Path without .maestro cannot produce workspace.
    assert Maestro._workspace_from_artifact_path('/tmp/nope/result.json') is None
    # Registry duplicate and missing status: list should skip invalid entries.
    idx=m._load_index(); idx.append({'number':99,'task_id':'task-missing','title':'missing','workspace':str(w1),'project_root':str(w1)}); m._save_index(idx); orig_status=m.status; m.status=lambda ref, *rest: (_ for _ in ()).throw(KeyError(ref)) if ref=='task-missing' else orig_status(ref, *rest); assert all(x['task_id']!='task-missing' for x in m.list_tasks())
    m.close()

def test_final_branch_edges(tmp_path, monkeypatch):
    env(tmp_path,monkeypatch); project,w1,_=git_repo(tmp_path); import maestro.core as core
    # Force journal read OSError.
    original=core.Path.read_text
    def bad_read(self,*a,**k):
        if self == project/'.maestro/project-state.jsonl': raise OSError('read')
        return original(self,*a,**k)
    monkeypatch.setattr(core.Path,'read_text',bad_read); (project/'.maestro').mkdir(exist_ok=True); (project/'.maestro/project-state.jsonl').write_text('x');
    m=Maestro(w1); m.close()
    monkeypatch.setattr(core.Path,'read_text',original)
    # malformed/filtered memvara registry claim branches
    (w1 / ".maestro").mkdir(exist_ok=True)
    mfile = w1 / ".maestro/config.toml"
    mfile.write_text('[storage]\nbackend="memvara"\n')
    import types,sys
    class C:
        def __init__(self,*a,**k): self.data=[types.SimpleNamespace(subject='maestro:registry',predicate='task',object=json.dumps(['x'])),types.SimpleNamespace(subject='other',predicate='task',object='x')]
        def history(self,s,p): return self.data
        def get_all(self): return self.data
        def remember(self,*a,**k): pass
        def add(self,*a,**k): return types.SimpleNamespace(episode_ids=['e'])
        def close(self): pass
    mod=types.ModuleType('memvara'); mod.Memvara=C; mod.NullLLM=lambda:None; monkeypatch.setitem(sys.modules,'memvara',mod); m=Maestro(w1); assert m._registry_records()==[]; m.close()
    # legacy migration duplicate + non-maestro claims
    (w1/'.maestro/config.toml').write_text('[storage]\nbackend="filesystem"\n');
    d=project/'.maestro'; d.mkdir(exist_ok=True); tid='task-20260916-111111-aaa111'; (d/'project-state.jsonl').write_text(json.dumps({'kind':'registry','record':{'task_id':tid,'number':1,'title':'x','workspace':str(w1)}})+'\n')
    m=Maestro(w1); # marker from earlier read-OSError isn't written, so import
    m.close(); m=Maestro(w1); assert len(m.list_tasks(project_filter=project))>=1; m.close()

def test_remaining_core_cli_coverage(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch); project,w1,_=git_repo(tmp_path); m=Maestro(w1)
    t=_seed(m,'A'); tid=t['task_id']
    # Numeric resolution and live-claim-only resolution.
    assert m.resolve_task(str(t['task_number'])) == tid
    m._save_index([])
    assert m.resolve_task(tid) == tid
    assert Maestro._workspace_from_artifact_path(None) is None
    m.close()
    # No-target CLI path lists all user-level tasks.
    from maestro import cli
    monkeypatch.delenv('MAESTRO_WORKSPACE', raising=False)
    monkeypatch.delenv('MAESTRO_PROJECT', raising=False)
    monkeypatch.setattr(sys, 'argv', ['maestro','task','list'])
    assert cli.main() == 0

def test_status_artifact_loop_exhausts_without_workspace(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch); _,w1,_=git_repo(tmp_path); m=Maestro(w1); t=_seed(m,'A'); tid=t['task_id']
    m._save_index([{**m._load_index()[0], 'workspace': None}])
    for predicate in ('task_workspace','task_design','task_result','task_verification'):
        m._write_claim(tid, predicate, '')
    s=m.status(tid); assert s['workspace'] is None
    m.close()


def test_pyproject_optional_dependency_groups_are_arrays():
    import tomllib
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    optional = data["project"].get("optional-dependencies", {})
    assert all(isinstance(value, list) for value in optional.values())
    assert "memvara" in optional
    assert not isinstance(optional.get("author", []), str)


def test_pyproject_version_is_release_version():
    import tomllib
    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert data["project"]["version"] == "0.16.1"


def test_project_root_codex_config_overrides_user_for_worktree(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    project, w1, _ = git_repo(tmp_path)
    home = tmp_path / 'home'; home.mkdir()
    (home / 'config.toml').write_text('[codex]\nmodel="user-model"\neffort="high"\n', encoding='utf-8')
    (project / '.maestro').mkdir(exist_ok=True)
    (project / '.maestro' / 'config.toml').write_text('[codex]\nmodel="gpt-5.6-luna"\neffort="max"\n', encoding='utf-8')
    m = Maestro(w1)
    assert m.codex_defaults() == {'model': 'gpt-5.6-luna', 'effort': 'max'}
    m.close()
