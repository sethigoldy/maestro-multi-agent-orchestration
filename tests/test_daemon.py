from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from maestro.agents import AgentSpec
from maestro.daemon import MaestroDaemon
from maestro.handoff import HandoffDoc, load_handoff_file


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git_repo(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(ws), *args], text=True, capture_output=True, env=env)

    git("init", "-q")
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "initial")
    return ws


def _doc(**kw) -> HandoffDoc:
    base = dict(title="Do the thing", request="Implement it", verification="none", commit_policy="no-commit")
    base.update(kw)
    return HandoffDoc(**base)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    yield d
    d.stop()


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    return bp


# ------------------------------------------------------------------ lifecycle
def test_delegate_success_end_to_end(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\necho implementing\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    assert started["queued"] is False and started["task_id"].startswith("task-")
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "completed"
    assert final["metadata"]["workspace"] == str(ws)
    assert final["metadata"]["origin_agent"] == "human" and final["metadata"]["target_agent"] == "codex"
    names = [a["name"] for a in final["artifacts"]]
    assert any(n.startswith("result-codex-") for n in names)
    claims = daemon.maestro._claims(started["task_id"])
    assert claims["task_origin_agent"] == "human" and claims["task_target_agent"] == "codex"
    assert claims["task_status"] == "REVIEWING"  # completed work awaits cross-review (0.8.x semantics)


def test_wait_returns_immediately_for_finished_task(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    daemon.wait(started["task_id"], timeout=60)
    again = daemon.wait(started["task_id"], timeout=5)
    assert again["status"]["state"] == "completed"


def test_wait_timeout(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nsleep 30')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    start = time.monotonic()
    sub = daemon.bus.subscribe()
    try:
        assert sub.wait(predicate=lambda e: e.task_id == started["task_id"] and e.data.get("state") == "completed", timeout=2) is None
    finally:
        sub.close()
    assert time.monotonic() - start < 5
    daemon.cancel(started["task_id"])


def test_failover_chain(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\necho boom\nexit 1')
    _fake_bin(binpath, "claude", 'cat > /dev/null\necho claude saved it\nexit 0')
    daemon.registry.save(AgentSpec(name="codex", kind="codex"))
    daemon.registry.save(AgentSpec(name="cc", kind="claude_code"))
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(target_agent="codex", fallback=["cc"]), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "completed"
    record = daemon._tasks[started["task_id"]]
    assert [a["agent"] for a in record["attempts"]] == ["codex", "cc"]
    assert record["agent"] == "cc"


def test_all_agents_fail_escalates(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 2')
    daemon.registry.save(AgentSpec(name="codex", kind="codex"))
    ws = _git_repo(tmp_path)
    sub = daemon.bus.subscribe()
    started = daemon.delegate(_doc(), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "failed"
    events = []
    while True:
        e = sub.get(timeout=0.3)
        if e is None:
            break
        events.append(e)
    assert any(e.type == "state" and e.data.get("escalation") for e in events)
    record = daemon._tasks[started["task_id"]]
    assert record["attempts"][0]["exit_code"] == 2


def test_unimplemented_kind_falls_over(daemon, tmp_path, binpath):
    # "copilot" is not implemented yet -> adapter unavailable -> chain continues
    _fake_bin(binpath, "claude", 'cat > /dev/null\nexit 0')
    daemon.registry.save(AgentSpec(name="cc", kind="claude_code"))
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(target_agent="copilot", fallback=["cc"]), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "completed"
    record = daemon._tasks[started["task_id"]]
    assert record["attempts"][0]["agent"] == "copilot" and "not implemented" in record["attempts"][0].get("error", "")


def test_preflight_failure_skips_agent(daemon, tmp_path, binpath):
    daemon.registry.save(AgentSpec(name="ghost", kind="generic", command="/nonexistent/agent --go"))
    _fake_bin(binpath, "claude", 'cat > /dev/null\nexit 0')
    daemon.registry.save(AgentSpec(name="cc", kind="claude_code"))
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(target_agent="ghost", fallback=["cc"]), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "completed"
    record = daemon._tasks[started["task_id"]]
    assert "not found" in record["attempts"][0].get("error", "")


def test_question_flow(daemon, tmp_path, binpath):
    _fake_bin(binpath, "asker", 'cat > /dev/null\necho \'{"question": "which database?"}\'\nexit 0')
    daemon.registry.save(AgentSpec(name="asker", kind="generic", command="asker --go", output_format="jsonl"))
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(target_agent="asker"), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "input-required"
    record = daemon._tasks[started["task_id"]]
    assert record["transcript"][0]["question"] == "which database?"

    # Now the agent answers and succeeds on the follow-up turn.
    _fake_bin(binpath, "asker", 'cat > /dev/null\necho done\nexit 0')
    result = daemon.answer_question(started["task_id"], "Use postgres")
    assert result["state"] == "working"
    final2 = daemon.wait(started["task_id"], timeout=60)
    assert final2["status"]["state"] == "completed"


def test_answer_rejects_wrong_state(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    daemon.wait(started["task_id"], timeout=60)
    with pytest.raises(ValueError):
        daemon.answer_question(started["task_id"], "late")
    with pytest.raises(KeyError):
        daemon.answer_question("task-19700101-000000-deadbeef", "x")


def test_sensitive_approval_gate(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(sensitive=True), ws)
    assert started["state"] == "input-required"
    # Nothing runs until approved:
    record = daemon._tasks[started["task_id"]]
    assert record["attempts"] == []
    daemon.answer_question(started["task_id"], "approved")
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "completed"


def test_max_depth_guard(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="depth"):
        daemon.delegate(_doc(max_depth_remaining=0), ws)
    with pytest.raises(ValueError, match="Workspace"):
        daemon.delegate(_doc(), tmp_path / "nope")


def test_cancel_running_task(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\ni=0\nwhile [ $i -lt 200 ]; do echo tick; sleep 0.1; i=$((i+1)); done')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    time.sleep(0.5)
    result = daemon.cancel(started["task_id"], reason="user stop")
    assert result["state"] == "canceled"
    final = daemon.status_a2a(started["task_id"])
    assert final["status"]["state"] == "canceled"
    with pytest.raises(ValueError):
        daemon.cancel(started["task_id"])


def test_cancel_releases_workspace_for_queue(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\ni=0\nwhile [ $i -lt 200 ]; do echo tick; sleep 0.1; i=$((i+1)); done')
    ws = _git_repo(tmp_path)
    first = daemon.delegate(_doc(title="first"), ws)
    time.sleep(0.3)
    queued = daemon.delegate(_doc(title="second"), ws)
    assert queued["queued"] is True
    daemon.cancel(first["task_id"])
    # The second task should now run to completion (fake still ticking, so cancel it too after it starts).
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        tasks = [t for t in daemon._tasks.values() if t["title"] == "second"]
        if tasks and tasks[0]["state"] != "submitted":
            break
        time.sleep(0.1)
    assert tasks, "queued task never started"
    daemon.cancel(tasks[0]["task_id"])


# ------------------------------------------------------------------ queueing
def test_one_active_task_per_workspace_fifo(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\necho tick; sleep 1.5; echo done\nexit 0')
    ws = _git_repo(tmp_path)
    first = daemon.delegate(_doc(title="first"), ws)
    time.sleep(0.3)  # let the first task start working
    second = daemon.delegate(_doc(title="second"), ws)
    assert second["queued"] is True
    other_ws = _git_repo(tmp_path, "other")
    parallel = daemon.delegate(_doc(title="parallel"), other_ws)
    assert parallel["queued"] is False  # different workspace runs concurrently
    final1 = daemon.wait(first["task_id"], timeout=60)
    assert final1["status"]["state"] == "completed"
    final2 = daemon.wait(second["task_id"], timeout=60)
    assert final2["status"]["state"] == "completed"
    daemon.wait(parallel["task_id"], timeout=60)


# ------------------------------------------------------------------ branches
def test_per_task_branch_in_git_workspace(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(commit_policy="branch"), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["metadata"]["branch"] == f"maestro/{started['task_id']}"
    current = subprocess.run(["git", "-C", str(ws), "rev-parse", "--abbrev-ref", "HEAD"], text=True, capture_output=True).stdout.strip()
    assert current == f"maestro/{started['task_id']}"


def test_no_commit_policy_skips_branch(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(commit_policy="no-commit"), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["metadata"]["branch"] is None


def test_non_git_workspace_skips_branch(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = tmp_path / "plain"
    ws.mkdir()
    started = daemon.delegate(_doc(commit_policy="branch"), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "completed" and final["metadata"]["branch"] is None


# ------------------------------------------------------------------ views
def test_list_tasks_and_resolve_numeric(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(title="List me"), ws)
    daemon.wait(started["task_id"], timeout=60)
    tasks = daemon.list_tasks()
    assert any(t["id"] == started["task_id"] for t in tasks)
    number = int(daemon.maestro._claims(started["task_id"])["task_number"])
    assert daemon.resolve(str(number)) == started["task_id"]


def test_status_a2a_durable_fallback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d1 = MaestroDaemon(state_dir=home, start_http=False)
    # Write durable claims directly, simulating a previous daemon run.
    tid = "task-20250101-000000-cafebabe"
    d1.maestro._register_task(tid, "Old task", 7)
    d1.maestro._write_claim(tid, "task_status", "REVIEWING")
    d1.maestro._write_claim(tid, "task_workspace", "/old/ws")
    d1.maestro._write_claim(tid, "task_branch", "maestro/old")
    obj = d1.status_a2a(tid)
    assert obj["status"]["state"] == "completed" and obj["metadata"]["workspace"] == "/old/ws"
    d1.stop()


def test_resolve_unknown_raises(daemon):
    with pytest.raises(KeyError):
        daemon.resolve("task-19700101-000000-deadbeef")


# ------------------------------------------------------------------ http/a2a
def test_agent_card_endpoint(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=True, port=0)
    try:
        d.registry.save(AgentSpec(name="codex", kind="codex", display_name="Codex CLI"))
        with urllib.request.urlopen(f"http://127.0.0.1:{d.port}/.well-known/agent.json") as resp:
            card = json.loads(resp.read().decode())
        assert card["name"] == "maestro-node" and card["url"] == f"http://127.0.0.1:{d.port}"
        codex_skill = next((s for s in card["skills"] if s["id"] == "codex"), None)
        assert codex_skill is not None and codex_skill["name"] == "Codex CLI"
    finally:
        d.stop()


def test_jsonrpc_over_http(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    home = daemon.state_dir
    port = daemon.start_http(0)
    ws = _git_repo(tmp_path)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "hello from a2a"}],
                "metadata": {"maestro": {"workspace": str(ws), "target_agent": "codex"}},
            }
        },
    }
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())
    task_id = result["result"]["task"]["id"]
    final = daemon.wait(task_id, timeout=60)
    assert final["status"]["state"] == "completed"

    get_body = {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task_id}}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(get_body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        got = json.loads(resp.read().decode())
    assert got["result"]["task"]["status"]["state"] == "completed"

    # 404 on unknown path, parse error on bad JSON
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=b"{not json", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400
        bad = json.loads(e.read().decode())
    assert bad["error"]["code"] == -32700


def test_sse_stream(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\necho working\nexit 0')
    port = daemon.start_http(0)
    ws = _git_repo(tmp_path)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "stream me"}], "metadata": {"maestro": {"workspace": str(ws)}}}},
    }
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        task_id = json.loads(resp.read().decode())["result"]["task"]["id"]

    conn = HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", f"/tasks/{task_id}/events")
    stream = conn.getresponse()
    assert stream.status == 200 and stream.getheader("Content-Type") == "text/event-stream"
    seen: list[str] = []
    data_lines: list[str] = []
    while True:
        line = stream.readline().decode().rstrip("\n")
        if not line:
            break  # EOF: the server closed the stream after the terminal event
        if line.startswith("event:"):
            seen.append(line.split(": ", 1)[1])
        elif line.startswith("data:"):
            data_lines.append(line.split(": ", 1)[1])
        if "state" in seen and any(json.loads(d).get("state") == "completed" for d in data_lines):
            break
    conn.close()
    assert "state" in seen


def test_daemon_json_marker(daemon, tmp_path):
    port = daemon.start_http(0)
    marker = json.loads((daemon.state_dir / "daemon.json").read_text())
    assert marker["port"] == port and marker["pid"] == os.getpid()
    daemon.stop()
    assert not (daemon.state_dir / "daemon.json").exists()


# ------------------------------------------------------------------ daemon_main
def test_run_daemon_returns_connection_info(tmp_path, monkeypatch):
    from maestro.daemon_main import run_daemon

    home = tmp_path / "home"
    info = run_daemon(state_dir=home, port=0)
    d = info["daemon"]
    assert info["port"] > 0 and info["state_dir"] == str(home)
    d.stop()


# ------------------------------------------------------------------ mcp tools
def _reset_singleton(monkeypatch, tmp_path):
    import maestro.daemon as dm

    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    old = dm._instance
    dm._instance = None
    return home, old


def test_mcp_tools_end_to_end(tmp_path, monkeypatch):
    import maestro.daemon as dm
    from maestro.mcp_server import agents_list, cancel_task, delegate, task_wait

    home, old = _reset_singleton(monkeypatch, tmp_path)
    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\necho ok\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    ws = _git_repo(tmp_path)
    handoff = tmp_path / "handoff.json"
    handoff.write_text(json.dumps(_doc().to_dict()), encoding="utf-8")
    try:
        out = json.loads(delegate(str(ws), str(handoff)))
        assert out["status"]["state"] == "completed"
        task_id = out["id"]

        listing = json.loads(agents_list())
        assert isinstance(listing, list)

        waited = json.loads(task_wait(str(ws), task_id))
        assert waited["status"]["state"] == "completed"

        # Cancel a finished task -> error surfaced as JSON.
        canceled = json.loads(cancel_task(str(ws), task_id))
        assert "error" in canceled
    finally:
        if dm._instance is not None:
            dm._instance.stop()
        dm._instance = old


def test_mcp_delegate_queued_and_unknown_task(tmp_path, monkeypatch):
    import maestro.daemon as dm
    from maestro.mcp_server import delegate, task_wait

    home, old = _reset_singleton(monkeypatch, tmp_path)
    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\nsleep 5')
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    ws = _git_repo(tmp_path)
    handoff = tmp_path / "h.json"
    handoff.write_text(json.dumps(_doc().to_dict()), encoding="utf-8")
    first: dict = {"id": None}
    try:
        monkeypatch.setenv("MAESTRO_DELEGATE_TIMEOUT", "2")
        first = json.loads(delegate(str(ws), str(handoff)))
        assert first["status"]["state"] == "working" and first.get("timed_out") is True
        queued = json.loads(delegate(str(ws), str(handoff)))
        assert queued["queued"] is True
        unknown = json.loads(task_wait(str(ws), "task-19700101-000000-deadbe"))
        assert "error" in unknown
    finally:
        if dm._instance is not None:
            try:
                dm._instance.cancel(first["id"], reason="test cleanup")
            except (KeyError, ValueError, TypeError):
                pass
            dm._instance.stop()
        dm._instance = old


# ------------------------------------------------------- coverage gap closure
def test_start_http_is_idempotent(daemon, tmp_path):
    port = daemon.start_http(0)
    again = daemon.start_http(9999)
    assert again == port  # second call is a no-op returning the live port


def test_stop_marker_unlink_failure(tmp_path, monkeypatch):
    home = tmp_path / "home"
    d = MaestroDaemon(state_dir=home, start_http=True, port=0)
    marker = home / "daemon.json"
    marker.unlink()
    marker.mkdir()  # a directory at the marker path: unlink raises OSError
    d.stop()  # must swallow the error and still shut down
    assert d._httpd is None


def test_defensive_unit_paths(daemon):
    daemon._persist("ghost")  # unknown task: no-op
    daemon._set_state("ghost", "weird")  # unknown task + unmapped state: no claims, event still published
    daemon._record_attempt("ghost", "a", None)  # unknown task: no-op
    assert daemon.status_a2a("ghost")["status"]["state"] == "working"  # durable fallback default


def test_start_queued_defensive_paths(daemon, tmp_path):
    daemon._start_queued("ghost")  # unknown id: no-op
    ws = _git_repo(tmp_path)
    doc = _doc(title="queued ghost")
    task_id, record = daemon._make_record(doc, str(ws))
    record["queued"] = True
    with daemon._lock:
        daemon._active[str(ws)] = "other-task"  # slot already taken
    daemon._start_queued(task_id)
    assert record["queued"] is True and record["state"] == "submitted"  # untouched


def test_release_drops_stale_queue_entries(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    with daemon._lock:
        daemon._queue.append("stale-id")
        daemon._active[str(ws)] = "current"
    daemon._release("current")
    assert daemon._queue == []  # stale entry dropped, current slot freed


def test_cancel_queued_task(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\ni=0\nwhile [ $i -lt 200 ]; do echo tick; sleep 0.1; i=$((i+1)); done')
    ws = _git_repo(tmp_path)
    first = daemon.delegate(_doc(title="first"), ws)
    time.sleep(0.3)
    queued = daemon.delegate(_doc(title="second"), ws)
    assert queued["queued"] is True and queued["task_id"]
    result = daemon.cancel(queued["task_id"], reason="no longer needed")
    assert result["state"] == "canceled"
    with daemon._lock:
        assert queued["task_id"] not in daemon._queue
    # The first task still owns the workspace.
    with daemon._lock:
        assert daemon._active[str(ws)] == first["task_id"]
    daemon.cancel(first["task_id"])


def test_cancel_stale_queued_record(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    doc = _doc(title="stale queued")
    task_id, record = daemon._make_record(doc, str(ws))
    record["queued"] = True  # not actually in the queue: remove() raises ValueError -> swallowed
    result = daemon.cancel(task_id)
    assert result["state"] == "canceled"


def test_answer_question_empty_rejected(daemon, tmp_path, binpath):
    _fake_bin(binpath, "asker", 'cat > /dev/null\necho \'{"question": "q?"}\'\nexit 0')
    daemon.registry.save(AgentSpec(name="asker", kind="generic", command="asker --go", output_format="jsonl"))
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(target_agent="asker"), ws)
    daemon.wait(started["task_id"], timeout=60)
    with pytest.raises(ValueError, match="empty"):
        daemon.answer_question(started["task_id"], "   ")


def test_doc_from_record_claims_fallback(daemon):
    from maestro.handoff import HandoffDoc

    tid = "task-20250101-000000-beef00"
    daemon.maestro._write_claim(tid, "task_request", "from claims")
    record = {"task_id": tid, "title": "T", "target_agent": "codex", "origin_agent": "human"}
    doc = daemon._doc_from_record(record)
    assert isinstance(doc, HandoffDoc) and doc.request == "from claims"


def test_prepare_branch_existing_and_forced_failure(daemon, tmp_path, monkeypatch):
    ws = _git_repo(tmp_path)
    first = daemon._prepare_branch(ws, "task-x", "branch")
    assert first == "maestro/task-x"
    second = daemon._prepare_branch(ws, "task-x", "branch")  # branch exists: plain checkout
    assert second == "maestro/task-x"

    import maestro.daemon as dm

    class _Ok:
        returncode = 0
        stdout = str(ws)
        stderr = ""

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "forced"

    def _fake_run(cmd, *args, **kwargs):
        if "rev-parse" in cmd:
            return _Ok()
        return _Fail()

    monkeypatch.setattr(dm.subprocess, "run", _fake_run)
    assert daemon._prepare_branch(ws, "task-y", "branch") is None


def test_verification_command_mode_failed(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(verification="command"), ws)  # request becomes the command: fails
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "completed"
    record = daemon._tasks[started["task_id"]]
    assert record["state"] == "completed"
    claims = daemon.maestro._claims(started["task_id"])
    assert claims["task_verification"].startswith("FAILED")


def test_verification_auto_passes(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(verification="auto"), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    claims = daemon.maestro._claims(started["task_id"])
    assert claims["task_verification"].startswith("PASSED")


def test_usage_event_published(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\necho \'{"total_cost_usd": 0.75}\'\nexit 0')
    ws = _git_repo(tmp_path)
    sub = daemon.bus.subscribe("usage")
    started = daemon.delegate(_doc(), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    event = sub.get(timeout=5)
    assert event is not None and event.data.get("cost_usd") == 0.75
    assert final["status"]["state"] == "completed"


def test_status_a2a_artifact_filter(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    daemon.wait(started["task_id"], timeout=60)
    task_dir = daemon.state_dir / "tasks" / started["task_id"]
    (task_dir / "notes.md").write_text("not an artifact suffix", encoding="utf-8")
    (task_dir / "subdir").mkdir()  # directories are skipped
    names = [a["name"] for a in daemon.status_a2a(started["task_id"])["artifacts"]]
    assert "notes.md" not in names and all(n.endswith((".json", ".txt", ".log")) for n in names)


def test_sse_keepalive(daemon, tmp_path):
    port = daemon.start_http(0)
    daemon.sse_heartbeat_s = 0.2
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/tasks/task-never/events")
    stream = conn.getresponse()
    line = stream.readline().decode().strip()
    assert line == ": keepalive"
    conn.close()


def test_sse_client_disconnect_is_safe(daemon, tmp_path):
    port = daemon.start_http(0)
    daemon.sse_heartbeat_s = 0.2
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/tasks/task-bye/events")
    stream = conn.getresponse()
    stream.read(0)
    conn.close()  # abrupt disconnect while the handler is waiting for events
    time.sleep(0.3)
    from maestro.events import TaskEvent

    daemon.bus.publish(TaskEvent(task_id="task-bye", type="state", data={"state": "working"}))
    # The daemon must still serve requests after the broken pipe.
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/.well-known/agent.json") as resp:
        assert json.loads(resp.read().decode())["name"] == "maestro-node"


def test_daemon_main_entry(tmp_path, monkeypatch, capsys):
    import maestro.daemon_main as dm

    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setattr(dm, "run_forever", lambda: None)  # no blocking
    rc = dm.main(["--port", "0", "--state-dir", str(home)])
    assert rc == 0
    out = capsys.readouterr().out
    info = json.loads(out)
    assert info["pid"] == os.getpid() and info["port"] > 0 and not (home / "daemon.json").exists()


def test_daemon_main_run_forever_with_signal():
    import maestro.daemon_main as dm

    timer = threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        dm.run_forever()  # main thread: handlers installed, SIGINT releases the wait
    finally:
        timer.cancel()


def test_daemon_main_run_forever_without_handlers():
    import maestro.daemon_main as dm

    timer = threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):  # no handler installed: default behavior applies
            dm.run_forever(install_handlers=False)
    finally:
        timer.cancel()


def test_daemon_main_parse_defaults(tmp_path):
    import maestro.daemon_main as dm

    args = dm._parse_args([])
    assert args.port == 0 and args.state_dir is None


def test_mcp_answer_task_question_and_error_paths(tmp_path, monkeypatch):
    import maestro.daemon as dm
    from maestro.mcp_server import answer_task_question, cancel_task

    home, old = _reset_singleton(monkeypatch, tmp_path)
    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "asker", 'cat > /dev/null\necho \'{"question": "which db?"}\'\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    d = dm.get_daemon()
    d.registry.save(AgentSpec(name="asker", kind="generic", command="asker --go", output_format="jsonl"))
    ws = _git_repo(tmp_path)
    started = d.delegate(_doc(target_agent="asker"), ws)
    d.wait(started["task_id"], timeout=60)
    try:
        out = json.loads(answer_task_question(str(ws), started["task_id"], "postgres"))
        assert out["state"] == "working"

        unknown = json.loads(answer_task_question(str(ws), "task-19700101-000000-deadbe", "x"))
        assert "error" in unknown
        # Cancel through the tool (success path) while the follow-up turn runs.
        cancelled = json.loads(cancel_task(str(ws), started["task_id"], reason="cleanup"))
        assert cancelled["state"] == "canceled"
        wrong_state = json.loads(answer_task_question(str(ws), started["task_id"], "late"))
        assert "error" in wrong_state  # no longer awaiting input
    finally:
        if dm._instance is not None:
            dm._instance.stop()
        dm._instance = old


def test_mcp_agents_list_with_registered_agent(tmp_path, monkeypatch):
    import maestro.daemon as dm
    from maestro.mcp_server import agents_list

    home, old = _reset_singleton(monkeypatch, tmp_path)
    d = dm.get_daemon()
    d.registry.save(AgentSpec(name="codex", kind="codex", display_name="Codex CLI"))
    try:
        listing = json.loads(agents_list())
        assert isinstance(listing, list) and len(listing) == 1
        assert listing[0]["name"] == "codex" and "status" in listing[0]
    finally:
        if dm._instance is not None:
            dm._instance.stop()
        dm._instance = old


def test_delegate_timeout_env_invalid(tmp_path, monkeypatch):
    from maestro.mcp_server import _delegate_timeout

    monkeypatch.setenv("MAESTRO_DELEGATE_TIMEOUT", "not-a-number")
    assert _delegate_timeout() == 3600.0


def test_start_queued_sensitive_promotion(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    with daemon._lock:
        daemon._active[str(ws)] = "busy-task"  # workspace occupied
    doc = _doc(title="sensitive queued", sensitive=True)
    started = daemon.delegate(doc, ws)
    assert started["queued"] is True
    # Free the slot: promotion must hit the approval gate, not run an agent.
    with daemon._lock:
        del daemon._active[str(ws)]
    daemon._release("busy-task")
    record = daemon._tasks[started["task_id"]]
    assert record["state"] == "input-required" and record["attempts"] == []


def test_set_state_none_data_skipped(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(title="none data"), ws)
    record = daemon._tasks[started["task_id"]]
    daemon._set_state(started["task_id"], "working", question=None, extra="kept")
    assert record.get("question") is None and record.get("extra") == "kept"
    daemon.cancel(started["task_id"])


def test_sse_skips_other_task_events(daemon, tmp_path):
    port = daemon.start_http(0)
    daemon.sse_heartbeat_s = 5
    from maestro.events import TaskEvent

    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/tasks/task-focus/events")
    stream = conn.getresponse()
    # A foreign task's event must not be forwarded; the focused one must be.
    daemon.bus.publish(TaskEvent(task_id="task-other", type="state", data={"state": "working"}))
    daemon.bus.publish(TaskEvent(task_id="task-focus", type="state", data={"state": "completed"}))
    got_focus = False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        line = stream.readline().decode().rstrip("\n")
        if not line:
            break
        if line.startswith("data:") and json.loads(line.split(": ", 1)[1])["task_id"] == "task-focus":
            got_focus = True
            break
    conn.close()
    assert got_focus


def test_post_empty_body_is_invalid_request(daemon, tmp_path):
    port = daemon.start_http(0)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=b"", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as excinfo:  # empty body is not a JSON-RPC request
        urllib.request.urlopen(req)
    assert excinfo.value.code == 400
    body = json.loads(excinfo.value.read().decode())
    assert body["error"]["code"] == -32602


def test_post_unknown_path_404(daemon, tmp_path):
    port = daemon.start_http(0)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/nope", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)
    assert excinfo.value.code == 404


def test_mcp_cancel_unknown_task(tmp_path, monkeypatch):
    import maestro.daemon as dm
    from maestro.mcp_server import answer_task_question, cancel_task

    home, old = _reset_singleton(monkeypatch, tmp_path)
    try:
        out = json.loads(cancel_task("/ws", "task-19700101-000000-deadbe"))
        assert "error" in out
        out2 = json.loads(answer_task_question("/ws", "task-19700101-000000-cafeba", "x"))
        assert "error" in out2
    finally:
        if dm._instance is not None:
            dm._instance.stop()
        dm._instance = old


def test_ghost_run_task_is_defensive(daemon, tmp_path, binpath):
    # Running a task id with no record must not crash: every lookup is defensive.
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    daemon._run_task("task-20990101-000000-ghost1", _doc(commit_policy="branch"), ws)
    # No record was created, no slot leaked.
    assert "task-20990101-000000-ghost1" not in daemon._tasks
    with daemon._lock:
        assert str(ws) not in daemon._active


def test_retry_with_backoff_then_escalate(tmp_path, binpath):
    home = tmp_path / "home"
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=1, backoff_s=0.2)
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 1')
    ws = _git_repo(tmp_path)
    sub = d.bus.subscribe("state")
    started = d.delegate(_doc(title="always fails"), ws)
    final = d.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "failed"
    record = d._tasks[started["task_id"]]
    assert len(record["attempts"]) == 2  # initial + one retry
    escalation = None
    while True:
        event = sub.get(timeout=1)
        if event is None or (event.data or {}).get("escalation"):
            escalation = event
            break
    assert escalation is not None and escalation.data.get("error")


def test_fifo_promotion_keeps_third_queued(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\ni=0\nwhile [ $i -lt 300 ]; do echo tick; sleep 0.1; i=$((i+1)); done')
    ws = _git_repo(tmp_path)
    first = daemon.delegate(_doc(title="a"), ws)
    time.sleep(0.3)
    second = daemon.delegate(_doc(title="b"), ws)
    third = daemon.delegate(_doc(title="c"), ws)
    assert second["queued"] and third["queued"]
    daemon.cancel(first["task_id"], reason="make room")
    # The second task is promoted; the third stays queued (workspace still busy).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not daemon._tasks[second["task_id"]]["queued"]:
            break
        time.sleep(0.1)
    assert daemon._tasks[second["task_id"]]["state"] in ("working", "completed")
    assert daemon._tasks[third["task_id"]]["queued"] is True
    daemon.cancel(second["task_id"])
    daemon.cancel(third["task_id"])


def test_list_tasks_includes_durable_records(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(title="durable me"), ws)
    daemon.wait(started["task_id"], timeout=60)
    d2 = MaestroDaemon(state_dir=daemon.state_dir, start_http=False)  # fresh memory, same state dir
    try:
        tasks = d2.list_tasks()
        ids = [t["id"] for t in tasks]
        assert started["task_id"] in ids  # recovered from durable claims
        states = {t["id"]: t["status"]["state"] for t in tasks}
        assert states[started["task_id"]] == "completed"
        # Waiting on a durable terminal task returns immediately (no bus wait).
        final = d2.wait(started["task_id"], timeout=5)
        assert final["status"]["state"] == "completed"
    finally:
        d2.stop()


def test_wait_unknown_task_raises_and_durable_working_times_out(tmp_path):
    home = tmp_path / "home"
    d = MaestroDaemon(state_dir=home, start_http=False)
    with pytest.raises(KeyError):
        d.wait("task-19700101-000000-deadbe")  # no record, no claims, no task dir
    # A durable non-terminal claim (e.g. a crashed run) waits until timeout.
    d.maestro._register_task("task-20250101-000000-beef01", "stuck", 3)
    d.maestro._write_claim("task-20250101-000000-beef01", "task_status", "IMPLEMENTING")
    start = time.monotonic()
    final = d.wait("task-20250101-000000-beef01", timeout=0.3)
    assert time.monotonic() - start >= 0.25
    assert final["status"]["state"] == "working"


def test_record_transcript_helpers():
    from maestro.daemon import record_transcript, record_transcript_answer, record_transcript_append

    assert record_transcript(None) == []
    record_transcript_append(None, "q")  # no-op on None
    record_transcript_answer(None, "a")  # no-op on None
    rec = {"transcript": []}
    record_transcript_append(rec, "q1")
    record_transcript_append(rec, "q2")
    record_transcript_answer(rec, "a2")
    record_transcript_answer(rec, "a1")
    record_transcript_answer(rec, "ignored")  # everything answered: loop exits without a break
    assert [entry["answer"] for entry in rec["transcript"]] == ["a1", "a2"]
    assert record_transcript(rec) == rec["transcript"]


def test_cancel_record_without_flag_is_defensive(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    task_id, record = daemon._make_record(_doc(title="flagless"), str(ws))
    with daemon._lock:
        del daemon._cancel_flags[task_id]  # simulate a record without a cancel flag
    result = daemon.cancel(task_id)
    assert result["state"] == "canceled"


def test_list_tasks_skips_corrupt_durable_record(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(title="good one"), ws)
    daemon.wait(started["task_id"], timeout=60)
    d2 = MaestroDaemon(state_dir=daemon.state_dir, start_http=False)
    try:
        real_status = d2.status_a2a

        def _flaky(task_id):
            if task_id == started["task_id"]:
                raise RuntimeError("corrupt journal")
            return real_status(task_id)

        d2.status_a2a = _flaky  # type: ignore[method-assign]
        tasks = d2.list_tasks()
        assert tasks == []  # the only durable record is corrupt: skipped, not fatal
    finally:
        d2.stop()


def test_release_skips_busy_workspace_then_continues(daemon, tmp_path):
    # Queue order: one startable (free ws), then two busy. The scan must skip the
    # busy entries and keep going (the 357->351 continue arc).
    ws1 = _git_repo(tmp_path, "ws1")
    ws2 = _git_repo(tmp_path, "ws2")
    a_id, _ = daemon._make_record(_doc(title="a"), str(ws1))
    busy_id, _ = daemon._make_record(_doc(title="busy"), str(ws2))
    c_id, _ = daemon._make_record(_doc(title="c"), str(ws1))
    d_id, _ = daemon._make_record(_doc(title="d"), str(ws2))
    e_id, _ = daemon._make_record(_doc(title="e"), str(ws2))
    with daemon._lock:
        daemon._active[str(ws1)] = a_id
        daemon._active[str(ws2)] = busy_id
        daemon._queue.extend([c_id, d_id, e_id])
    daemon._start_queued = lambda tid: None  # don't actually launch agents here
    daemon._release(a_id)  # frees ws1
    with daemon._lock:
        assert daemon._queue == [d_id, e_id]  # c promoted; d,e still queued (ws2 busy)
        assert str(ws1) not in daemon._active  # _start_queued was stubbed: slot stays free


# ------------------------------------------------------------------ M3: followup + guards
def test_followup_resumes_finished_task(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(title="original"), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "completed"
    attempts_before = len(daemon._tasks[started["task_id"]]["attempts"])

    result = daemon.followup(started["task_id"], "now also update the README")
    assert result["state"] == "submitted"
    final2 = daemon.wait(started["task_id"], timeout=60)
    assert final2["status"]["state"] == "completed"
    record = daemon._tasks[started["task_id"]]
    assert len(record["attempts"]) > attempts_before  # the follow-up turn ran
    task_dir = daemon.state_dir / "tasks" / started["task_id"]
    results = sorted(p.name for p in task_dir.iterdir() if p.name.startswith("result-"))
    assert len(results) >= 2


def test_followup_rejects_active_and_bad_input(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nsleep 5')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(title="busy"), ws)
    time.sleep(0.3)
    with pytest.raises(ValueError, match="still active"):
        daemon.followup(started["task_id"], "extra work")
    with pytest.raises(ValueError, match="empty"):
        daemon.followup(started["task_id"], "   ")
    with pytest.raises(KeyError):
        daemon.followup("task-19700101-000000-deadbe", "x")
    daemon.cancel(started["task_id"])


def test_followup_depth_guard(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(title="shallow", max_depth_remaining=1), ws)
    daemon.wait(started["task_id"], timeout=60)
    with pytest.raises(ValueError, match="depth"):
        daemon.followup(started["task_id"], "one more")


def test_delegate_rejects_self_delegation(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    doc = _doc(title="selfish", target_agent="codex", origin_agent="codex")
    with pytest.raises(ValueError, match="itself"):
        daemon.delegate(doc, ws)


def test_mcp_followup_and_self_delegation_errors(tmp_path, monkeypatch):
    import maestro.daemon as dm
    from maestro.mcp_server import delegate, followup

    home, old = _reset_singleton(monkeypatch, tmp_path)
    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    d = dm.get_daemon()
    ws = _git_repo(tmp_path)
    handoff = tmp_path / "h.json"
    doc = _doc(title="m3 flow")
    handoff.write_text(json.dumps(doc.to_dict()), encoding="utf-8")
    try:
        first = json.loads(delegate(str(ws), str(handoff)))
        assert first["status"]["state"] == "completed"
        out = json.loads(followup(str(ws), first["id"], "add a changelog entry"))
        assert out["status"]["state"] == "completed" and out.get("timed_out") is False

        # Unknown task and self-delegation surface as JSON errors, not exceptions.
        unknown = json.loads(followup(str(ws), "task-19700101-000000-deadbe", "x"))
        assert "error" in unknown

        # Following up an active task surfaces the ValueError as JSON too.
        _fake_bin(bp, "sleepy", 'cat > /dev/null\nsleep 8')
        d.registry.save(AgentSpec(name="sleepy", kind="generic", command="sleepy --go", output_format="jsonl"))
        busy_ws = _git_repo(tmp_path, "busy")
        busy_handoff = tmp_path / "busy.json"
        busy_handoff.write_text(json.dumps(_doc(title="busy m3", target_agent="sleepy").to_dict()), encoding="utf-8")
        started_busy = d.delegate(load_handoff_file(str(busy_handoff)), str(busy_ws))
        time.sleep(0.3)
        active_err = json.loads(followup(str(busy_ws), started_busy["task_id"], "extra"))
        assert "still active" in active_err["error"]
        d.cancel(started_busy["task_id"])
        selfish = tmp_path / "self.json"
        selfish.write_text(json.dumps(_doc(title="selfish", target_agent="codex", origin_agent="codex").to_dict()), encoding="utf-8")
        rejected = json.loads(delegate(str(ws), str(selfish)))
        assert "error" in rejected and "itself" in rejected["error"]
    finally:
        if dm._instance is not None:
            dm._instance.stop()
        dm._instance = old


def test_legacy_mcp_tools_keep_stable_signatures():
    import inspect

    from maestro import mcp_server

    expected = {
        "delegate_to_codex": ["workspace", "handoff_file"],
        "task_status": ["workspace", "task_id"],
        "list_tasks": ["workspace"],
        "codex_followup": ["workspace", "task_id", "instruction"],
        "review_task": ["workspace", "task_id", "review", "approved"],
    }
    for name, params in expected.items():
        sig = list(inspect.signature(getattr(mcp_server, name)).parameters)
        assert sig == params, f"{name} signature drifted: {sig}"
