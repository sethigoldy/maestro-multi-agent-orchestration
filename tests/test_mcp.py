import json
from maestro import mcp_server


def test_mcp_state_tools(monkeypatch):
    events = []

    class Fake:
        def __init__(self, root):
            self.root = root
            self.project_root = root

        def status(self, t):
            return {"task_id": t}

        def list_tasks(self, **kwargs):
            return [{"task_id": "t"}]

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(mcp_server, "_instance", lambda workspace: Fake(workspace))
    assert json.loads(mcp_server.task_status("w", "t"))["task_id"] == "t"
    assert json.loads(mcp_server.list_tasks("w"))[0]["task_id"] == "t"


def test_mcp_main(monkeypatch):
    called = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: called.append(True))
    assert mcp_server.main() is None
    assert called == [True]
