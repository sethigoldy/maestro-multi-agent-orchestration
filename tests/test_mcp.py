from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro import mcp_server


def test_instance_uses_git_root(monkeypatch, tmp_path: Path):
    called = []
    class Fake:
        @staticmethod
        def git_root(path):
            called.append(path)
            return tmp_path
        def __init__(self, root):
            called.append(root)
    monkeypatch.setattr(mcp_server, "Maestro", Fake)
    obj = mcp_server._instance("/active/worktree")
    assert isinstance(obj, Fake)
    assert called == ["/active/worktree", tmp_path]


def test_mcp_delegate_status_list_review(monkeypatch):
    events = []
    class Fake:
        def __init__(self, root): self.root = root
        def create_handoff_from_file(self, f): events.append(("handoff", f)); return {"task_id": "t"}
        def implement_async(self, t): events.append(("implement", t)); return {"pid": 1}
        def finalize_staged_handoff(self, f, t): events.append(("finalize", f, t)); return "/archived"
        def status(self, t): events.append(("status", t)); return {"task_id": t}
        def list_tasks(self): events.append(("list",)); return [{"task_id": "t"}]
        def review(self, *args): events.append(("review", args)); return {"approved": args[-1]}
        def fix_async(self, t, r): events.append(("fix", t, r)); return {"pid": 2}
        def close(self): events.append(("close",))
    monkeypatch.setattr(mcp_server, "_instance", lambda workspace: Fake(workspace))
    result = json.loads(mcp_server.delegate_to_codex("w", "f"))
    assert result["handoff"]["task_id"] == "t"
    assert json.loads(mcp_server.task_status("w", "t"))["task_id"] == "t"
    assert json.loads(mcp_server.list_tasks("w"))[0]["task_id"] == "t"
    approved = json.loads(mcp_server.review_task("w", "t", "ok", True))
    assert "fix" not in approved
    rejected = json.loads(mcp_server.review_task("w", "t", "bad", False))
    assert "fix" in rejected
    assert events.count(("close",)) == 5


def test_mcp_main(monkeypatch):
    called = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: called.append(True))
    assert mcp_server.main() is None
    assert called == [True]
