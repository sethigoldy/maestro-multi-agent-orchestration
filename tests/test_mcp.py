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


def test_mcp_followup_passes_context_mode(monkeypatch):
    calls = []

    class FakeDaemon:
        def resolve(self, ref):
            return f"resolved-{ref}"

        def followup(self, task_id, instruction, context_mode="reuse", branch=None):
            calls.append((task_id, instruction, context_mode))
            return {"task_id": task_id, "state": "submitted"}

        def wait(self, task_id, timeout=None):
            return {"status": {"state": "completed", "timestamp": "t"}, "metadata": {}}

    monkeypatch.setattr(mcp_server, "get_daemon", lambda: FakeDaemon())
    out = json.loads(mcp_server.followup("w", "task-1", "keep going"))
    assert calls == [("resolved-task-1", "keep going", "reuse")]
    assert out["timed_out"] is False and out["status"]["state"] == "completed"

    json.loads(mcp_server.followup("w", "task-1", "clean", context_mode="fresh"))
    assert calls[1][2] == "fresh"

    class Unknown(FakeDaemon):
        def followup(self, *a, **k):
            raise KeyError("Unknown task reference 'nope'")

    monkeypatch.setattr(mcp_server, "get_daemon", lambda: Unknown())
    assert "error" in json.loads(mcp_server.followup("w", "nope", "x"))

    class Depth(Unknown):
        def followup(self, *a, **k):
            raise ValueError("Max delegation depth exceeded")

    monkeypatch.setattr(mcp_server, "get_daemon", lambda: Depth())
    assert "depth" in json.loads(mcp_server.followup("w", "nope", "x"))["error"].lower()
