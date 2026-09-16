import json
from maestro import mcp_server

def test_mcp_delegate_status_list_review(monkeypatch):
    events=[]
    class Fake:
        def __init__(self, root): self.root=root; self.project_root=root
        def create_handoff_from_file(self,f): events.append(('handoff',f)); return {'task_id':'t'}
        def implement_async(self,t): events.append(('implement',t)); return {'pid':1}
        def finalize_staged_handoff(self,f,t): events.append(('finalize',f,t)); return '/archived'
        def status(self,t): return {'task_id':t}
        def list_tasks(self, **kwargs): return [{'task_id':'t'}]
        def review(self,*args): events.append(('review',args)); return {'approved':args[-1]}
        def fix_async(self,t,r): events.append(('fix',t,r)); return {'pid':2}
        def codex_followup(self,t,i): events.append(('followup',t,i)); return {'pid':3}
        def close(self): events.append(('close',))
    monkeypatch.setattr(mcp_server,'_instance',lambda workspace: Fake(workspace))
    result=json.loads(mcp_server.delegate_to_codex('w','f')); assert result['handoff']['task_id']=='t'
    assert json.loads(mcp_server.task_status('w','t'))['task_id']=='t'
    assert json.loads(mcp_server.list_tasks('w'))[0]['task_id']=='t'
    assert json.loads(mcp_server.codex_followup('w','t','do it'))['pid']==3
    assert 'fix' not in json.loads(mcp_server.review_task('w','t','ok',True))
    assert 'fix' in json.loads(mcp_server.review_task('w','t','bad',False))

def test_mcp_main(monkeypatch):
    called=[]; monkeypatch.setattr(mcp_server.mcp,'run',lambda: called.append(True)); assert mcp_server.main() is None; assert called==[True]
