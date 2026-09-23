"""The MCP server sends its calls to the daemon that owns the state directory.

When a daemon already owns the state directory (for example one started with
``maestro daemon start``), the MCP server's ``get_daemon()`` returns a
:class:`maestro.daemon_client.DaemonClient` instead of starting a second daemon.
Every task then runs in the owner, so the owner can cancel it and the
one-active-task-per-workspace rule holds. When the owner stops answering, the
MCP server starts its own daemon.
"""

from __future__ import annotations

import http.client
import json
import os
import stat
import subprocess
import threading
import time
import urllib.error
import uuid
from pathlib import Path

import pytest

import maestro.daemon as dm
from maestro import daemonctl, mcp_server
from maestro.a2a_client import post_jsonrpc
from maestro.agents import AgentSpec
from maestro.daemon import MaestroDaemon
from maestro.daemon_client import DaemonClient, DaemonUnavailable
from maestro.handoff import HandoffDoc


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git_repo(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "initial"]):
        subprocess.run(["git", "-C", str(ws), *args], env=env, check=True, capture_output=True)
    return ws


def _doc(target: str, **kw) -> HandoffDoc:
    base = dict(title="Do the thing", request="Implement it", verification="none", commit_policy="no-commit", target_agent=target, explicit_target=True)
    base.update(kw)
    return HandoffDoc(**base)


def _drain(daemon: MaestroDaemon) -> None:
    for tid, record in list(daemon._tasks.items()):
        if record.get("state") not in ("completed", "failed", "canceled"):
            try:
                daemon.cancel(tid, reason="test teardown")
            except (KeyError, ValueError):
                pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any("_run_task" in (t.name or "") for t in threading.enumerate()):
        time.sleep(0.05)


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(path))
    monkeypatch.setattr(dm, "_instance", None)
    yield path
    instance = dm._instance
    dm._instance = None
    if isinstance(instance, MaestroDaemon):
        _drain(instance)
        instance.stop()


@pytest.fixture
def agents(tmp_path, monkeypatch):
    """Fake agent binaries: 'quick' finishes at once, 'sleepy_<id>' sleeps, 'asker' asks a question."""
    bin_dir = tmp_path / "bin"
    sleepy = f"sleepy_{uuid.uuid4().hex[:8]}"
    _fake_bin(bin_dir, "quick", "cat > /dev/null\necho done")
    _fake_bin(bin_dir, sleepy, "cat > /dev/null\nsleep 30")
    _fake_bin(bin_dir, "asker", "cat > /dev/null\necho '{\"question\": \"which db?\"}'")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return sleepy


@pytest.fixture
def owner(home, agents):
    """A daemon that owns ``home`` and serves HTTP, standing in for 'maestro daemon start'."""
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    d.registry.save(AgentSpec(name="quick", kind="generic", command="quick --go"))
    d.registry.save(AgentSpec(name=agents, kind="generic", command=f"{agents} --go"))
    d.registry.save(AgentSpec(name="asker", kind="generic", command="asker --go", output_format="jsonl"))
    yield d
    _drain(d)
    d.stop()


def _agent_running(name: str) -> bool:
    return subprocess.run(["pgrep", "-f", name], capture_output=True).returncode == 0


# ------------------------------------------------------------ routing to the owner
def test_get_daemon_returns_a_client_for_the_owner(owner, home):
    client = dm.get_daemon()
    assert isinstance(client, DaemonClient)
    assert client.url == f"http://127.0.0.1:{owner.port}" and client.pid == os.getpid()
    assert dm.get_daemon() is client  # reused while the owner answers
    client.stop()  # nothing to release for a client


def test_owner_can_cancel_a_task_the_mcp_server_delegated(owner, home, agents, tmp_path):
    """Reviewer's r7_cancel.py: the owner's tasks/cancel must stop the agent the MCP server started."""
    client = dm.get_daemon()
    ws = _git_repo(tmp_path)
    started = client.delegate(_doc(agents), ws)
    task_id = started["task_id"]
    assert task_id in owner._tasks  # the owner runs it, not the MCP server's process
    owner.wait(task_id, timeout=20, stop_states=("working",))
    deadline = time.monotonic() + 10
    while not _agent_running(agents) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _agent_running(agents)
    result = post_jsonrpc(f"http://127.0.0.1:{owner.port}", "tasks/cancel", {"id": task_id, "reason": "user"})
    assert result["task"]["status"]["state"] == "canceled"
    final = client.wait(task_id, timeout=20)
    assert final["status"]["state"] == "canceled"
    deadline = time.monotonic() + 10
    while _agent_running(agents) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _agent_running(agents), "the agent kept running after the owner canceled the task"


def test_one_active_task_per_workspace_holds_across_processes(owner, home, agents, tmp_path):
    client = dm.get_daemon()
    ws = _git_repo(tmp_path)
    first = client.delegate(_doc(agents), ws)
    second = owner.delegate(_doc("quick"), ws)
    assert first["queued"] is False and second["queued"] is True
    third = client.delegate(_doc("quick"), ws)
    assert third["queued"] is True


def test_mcp_tools_work_through_the_owner(owner, home, agents, tmp_path):
    ws = _git_repo(tmp_path)
    handoff = tmp_path / "quick.json"
    handoff.write_text(json.dumps(_doc("quick", commit_policy="branch").to_dict()), encoding="utf-8")

    done = json.loads(mcp_server.delegate(str(ws), str(handoff)))
    assert done["status"]["state"] == "completed" and done["timed_out"] is False
    task_id = done["id"]
    assert task_id in owner._tasks

    waited = json.loads(mcp_server.task_wait(str(ws), task_id))
    assert waited["status"]["state"] == "completed"
    number = str(owner.maestro._claims(task_id)["task_number"])
    assert json.loads(mcp_server.task_wait(str(ws), number))["id"] == task_id  # numbers resolve on the owner

    listing = json.loads(mcp_server.agents_list())
    assert {"quick", agents, "asker"} <= {agent["name"] for agent in listing} and all("status" in a for a in listing)

    renamed = json.loads(mcp_server.rename_task_branch(str(ws), task_id, "feat/renamed"))
    assert renamed["branch"] == "feat/renamed"

    again = json.loads(mcp_server.followup(str(ws), task_id, "add a changelog entry"))
    assert again["status"]["state"] == "completed" and again["id"] == task_id

    finished = json.loads(mcp_server.cancel_task(str(ws), task_id))
    assert "already finished" in finished["error"]
    assert "error" in json.loads(mcp_server.task_wait(str(ws), "task-19700101-000000-deadbe"))
    assert "error" in json.loads(mcp_server.task_wait(str(ws), "999"))
    assert "error" in json.loads(mcp_server.rename_task_branch(str(ws), task_id, "bad name"))
    assert "error" in json.loads(mcp_server.followup(str(ws), "task-19700101-000000-deadbe", "x"))

    bad = tmp_path / "self.json"
    bad.write_text(json.dumps(_doc("quick", origin_agent="quick").to_dict()), encoding="utf-8")
    assert "itself" in json.loads(mcp_server.delegate(str(ws), str(bad)))["error"]


def test_answer_and_cancel_work_through_the_owner(owner, home, tmp_path):
    ws = _git_repo(tmp_path)
    handoff = tmp_path / "ask.json"
    handoff.write_text(json.dumps(_doc("asker").to_dict()), encoding="utf-8")
    parked = json.loads(mcp_server.delegate(str(ws), str(handoff)))
    assert parked["status"]["state"] == "input-required"
    task_id = parked["id"]
    answered = json.loads(mcp_server.answer_task_question(str(ws), task_id, "postgres"))
    assert answered["task_id"] == task_id and answered["state"] in ("working", "submitted")
    assert "error" in json.loads(mcp_server.answer_task_question(str(ws), "task-19700101-000000-deadbe", "x"))
    owner.wait(task_id, timeout=20)
    if owner._tasks[task_id]["state"] == "input-required":
        canceled = json.loads(mcp_server.cancel_task(str(ws), task_id, reason="cleanup"))
        assert canceled == {"task_id": task_id, "state": "canceled"}
    assert "error" in json.loads(mcp_server.answer_task_question(str(ws), task_id, "late"))


def test_blocking_wait_honours_its_timeout(owner, home, agents, tmp_path, monkeypatch):
    monkeypatch.setattr(DaemonClient, "WAIT_SLICE_S", 0.3)
    ws = _git_repo(tmp_path)
    client = dm.get_daemon()
    started = client.delegate(_doc(agents), ws)
    began = time.monotonic()
    out = json.loads(mcp_server.task_wait(str(ws), started["task_id"], timeout=1.0))
    elapsed = time.monotonic() - began
    assert out["status"]["state"] in ("submitted", "working") and 0.9 <= elapsed < 10


def test_delegate_tool_reports_a_queued_handoff(owner, home, agents, tmp_path):
    ws = _git_repo(tmp_path)
    owner.delegate(_doc(agents), ws)
    handoff = tmp_path / "quick.json"
    handoff.write_text(json.dumps(_doc("quick").to_dict()), encoding="utf-8")
    out = json.loads(mcp_server.delegate(str(ws), str(handoff)))
    assert out["queued"] is True


def test_client_status_and_list(owner, home, tmp_path):
    client = dm.get_daemon()
    ws = _git_repo(tmp_path)
    started = client.delegate(_doc("quick"), ws)
    client.wait(started["task_id"], timeout=20)
    assert client.status_a2a(started["task_id"])["status"]["state"] == "completed"
    assert started["task_id"] in {task["id"] for task in client.list_tasks()}
    assert client.resolve(started["task_id"]) == started["task_id"]
    assert owner.current_state(started["task_id"]) == "completed"  # a live record
    assert owner.current_state("task-19700101-000000-deadbe") is None  # an unknown task
    with pytest.raises(KeyError):
        client.followup("task-19700101-000000-deadbe", "go on", branch="feat/next")


def test_client_sends_the_owner_token(home, monkeypatch, tmp_path):
    """A daemon bound beyond loopback requires its token; the client reads it from the marker."""
    monkeypatch.setenv("MAESTRO_DAEMON_TOKEN", "sekrit-token")
    monkeypatch.setenv("MAESTRO_DISCOVERY", "0")
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, bind="0.0.0.0", max_retries=0, backoff_s=0)
    try:
        client = dm.get_daemon()
        assert isinstance(client, DaemonClient) and client.token == "sekrit-token"
        assert client.agents() == d.agents()
        wrong = DaemonClient(home, client.url, "wrong", client.pid, fallback=lambda: None)
        with pytest.raises(ValueError, match="rejected"):
            wrong.agents()
    finally:
        d.stop()


# ------------------------------------------------------------ the owner goes away
def test_mcp_server_takes_ownership_when_the_owner_stops(owner, home):
    client = dm.get_daemon()
    assert isinstance(client, DaemonClient)
    owner.stop()
    replacement = dm.get_daemon()
    assert isinstance(replacement, MaestroDaemon) and replacement._httpd is not None
    assert json.loads((home / "daemon.json").read_text())["port"] == replacement.port


def _no_local_daemon(**kwargs):
    raise AssertionError("a daemon was started in this process while another daemon owns the directory")


def test_mcp_server_never_runs_tasks_beside_an_owner_that_does_not_answer(home, monkeypatch):
    """While a live daemon holds the directory, the MCP server retries and then reports an error; it runs nothing itself."""
    hung = daemonctl.DaemonInfo(running=False, pid=424242, url="http://127.0.0.1:1", state_dir=home)
    monkeypatch.setattr(daemonctl, "live_owner", lambda state_dir: hung)
    monkeypatch.setattr(dm, "MaestroDaemon", _no_local_daemon)
    monkeypatch.setattr(dm, "OWNER_RETRY_S", 0.3)
    began = time.monotonic()
    with pytest.raises(DaemonUnavailable, match="does not answer") as caught:
        dm.get_daemon()
    assert "424242" in str(caught.value) and "maestro daemon restart" in str(caught.value)
    assert 0.3 <= time.monotonic() - began < 5
    assert dm._instance is None


def test_mcp_server_waits_for_an_owner_that_misses_a_few_probes(home, monkeypatch):
    hung = daemonctl.DaemonInfo(running=False, pid=4242, url="http://127.0.0.1:9", state_dir=home)
    back = daemonctl.DaemonInfo(running=True, pid=4242, url="http://127.0.0.1:9", state_dir=home)
    answers = iter([hung, hung, back])
    monkeypatch.setattr(daemonctl, "live_owner", lambda state_dir: next(answers))
    monkeypatch.setattr(dm, "MaestroDaemon", _no_local_daemon)
    client = dm.get_daemon()
    assert isinstance(client, DaemonClient) and client.url == "http://127.0.0.1:9"


def test_tools_report_an_owner_that_stays_silent(home, monkeypatch):
    hung = daemonctl.DaemonInfo(running=False, pid=4242, url="http://127.0.0.1:9", state_dir=home)
    monkeypatch.setattr(daemonctl, "live_owner", lambda state_dir: hung)
    monkeypatch.setattr(dm, "MaestroDaemon", _no_local_daemon)
    monkeypatch.setattr(dm, "OWNER_RETRY_S", 0.1)
    assert "does not answer" in json.loads(mcp_server.agents_list())["error"]


def test_mcp_server_uses_a_daemon_that_took_ownership_first(home, monkeypatch):
    """Between the owner check and the start, another daemon may take the directory."""
    info = daemonctl.DaemonInfo(running=True, pid=4242, url="http://127.0.0.1:9", state_dir=home)
    answers = iter([None, info])
    monkeypatch.setattr(daemonctl, "live_owner", lambda state_dir: next(answers))

    def refuse(**kwargs):
        raise daemonctl.DaemonAlreadyRunning("another Maestro daemon already owns the state directory")

    monkeypatch.setattr(dm, "MaestroDaemon", refuse)
    client = dm.get_daemon(state_dir=str(home))
    assert isinstance(client, DaemonClient) and client.url == "http://127.0.0.1:9" and client.state_dir == home


def test_mcp_server_gives_up_when_the_directory_is_never_free_to_take(home, monkeypatch):
    """Every start loses the race, yet no daemon ever answers: bounded, then an error."""
    monkeypatch.setattr(daemonctl, "live_owner", lambda state_dir: None)

    def refuse(**kwargs):
        raise daemonctl.DaemonAlreadyRunning("another Maestro daemon already owns the state directory")

    monkeypatch.setattr(dm, "MaestroDaemon", refuse)
    monkeypatch.setattr(dm, "OWNER_RETRY_S", 0.2)
    with pytest.raises(DaemonUnavailable, match="could not start or reach"):
        dm.get_daemon()


def test_delegate_and_followup_report_a_wait_that_cannot_continue(home, monkeypatch, tmp_path):
    """A blocking tool whose daemon vanished and cannot be replaced returns an error instead of raising."""

    class Vanishing:
        def delegate(self, doc, workspace):
            return {"task_id": "task-20260101-000000-abcdef", "queued": False}

        def resolve(self, ref):
            return ref

        def followup(self, *args, **kwargs):
            return {"task_id": "task-20260101-000000-abcdef"}

        def wait(self, task_id, timeout=None):
            raise DaemonUnavailable("the Maestro daemon (pid 1) owns the state directory but does not answer")

    monkeypatch.setattr(mcp_server, "get_daemon", lambda: Vanishing())
    handoff = tmp_path / "h.json"
    handoff.write_text(json.dumps(_doc("quick").to_dict()), encoding="utf-8")
    assert "does not answer" in json.loads(mcp_server.delegate(str(tmp_path), str(handoff)))["error"]
    assert "does not answer" in json.loads(mcp_server.followup(str(tmp_path), "task-20260101-000000-abcdef", "go on"))["error"]


# ------------------------------------------------------------ background owner (item 6)
@pytest.fixture
def background(home, agents, monkeypatch):
    """The MCP server's production mode: start a detached background daemon when nobody owns the directory."""
    monkeypatch.setattr(dm, "BACKGROUND_OWNER", True)
    yield home
    # Stop the background daemon this test started, never this test process
    # (when the daemon runs inside it, the home fixture stops that one).
    if daemonctl.status(home).pid not in (None, os.getpid()):
        daemonctl.stop(home, grace_s=5)


def test_mcp_server_starts_a_background_daemon_when_no_daemon_owns_the_directory(background, agents, tmp_path, monkeypatch):
    """Closing the session must not kill tasks: the owner is a separate process, not the MCP server."""
    home = background
    client = dm.get_daemon()
    assert isinstance(client, DaemonClient)
    marker = json.loads((home / "daemon.json").read_text())
    assert marker["pid"] != os.getpid() and client.pid == marker["pid"]
    # The fake agent is only on this process's PATH, so the background daemon
    # can run it only because it inherited the MCP server's environment.
    from maestro.agents import AgentRegistry

    AgentRegistry(home).save(AgentSpec(name=agents, kind="generic", command=f"{agents} --go"))
    ws = _git_repo(tmp_path)
    started = client.delegate(_doc(agents), ws)
    deadline = time.monotonic() + 20
    while client.status_a2a(started["task_id"])["status"]["state"] != "working" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert client.status_a2a(started["task_id"])["status"]["state"] == "working"
    # The session closes: its MCP server shuts down. The task keeps running in the background daemon.
    dm.shutdown_daemon()
    assert daemonctl.status(home).running is True
    time.sleep(0.5)
    assert dm.get_daemon().status_a2a(started["task_id"])["status"]["state"] == "working"


def test_mcp_server_embeds_the_daemon_when_no_background_daemon_can_start(background, monkeypatch, capsys):
    def cannot_start(state_dir=None, **kwargs):
        raise RuntimeError("daemon exited while starting")

    monkeypatch.setattr(daemonctl, "start", cannot_start)
    d = dm.get_daemon()
    assert isinstance(d, MaestroDaemon) and d._httpd is not None
    err = capsys.readouterr().err
    assert "could not start a background daemon" in err and "stop when this MCP server exits" in err


def test_wait_moves_to_the_replacement_daemon_when_the_owner_stops_answering(home):
    class Replacement:
        def wait(self, task_id, timeout=None):
            return {"id": task_id, "status": {"state": "failed"}, "timeout": timeout}

    client = DaemonClient(home, f"http://127.0.0.1:{_closed_port()}", None, 1, fallback=Replacement)
    out = client.wait("task-20260101-000000-abcdef", timeout=5)
    assert out["status"]["state"] == "failed" and 0 < out["timeout"] <= 5
    unbounded = client.wait("task-20260101-000000-abcdef")
    assert unbounded["timeout"] is None


def test_client_replaces_itself_through_get_daemon(owner, home):
    client = dm.get_daemon()
    owner.stop()
    replacement = client._fallback()
    assert isinstance(replacement, MaestroDaemon) and dm._instance is replacement
    assert dm._replace_client(client) is replacement  # an already-replaced client gets the current daemon
    # After shutdown_daemon cleared the singleton, a late fallback opens a daemon again:
    # here a client of the replacement, which still owns the directory.
    dm._instance = None
    try:
        again = dm._replace_client(client)
        assert isinstance(again, DaemonClient) and again.url == f"http://127.0.0.1:{replacement.port}"
    finally:
        dm._instance = None
        replacement.stop()


def test_client_reports_an_owner_that_went_away(home):
    client = DaemonClient(home, f"http://127.0.0.1:{_closed_port()}", None, 1, fallback=lambda: None)
    with pytest.raises(DaemonUnavailable, match="did not answer"):
        client.agents()
    assert client.alive() is False


def _closed_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Reply:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError("http://x/", code, "error", {}, io.BytesIO(body))


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (_http_error(401, b""), (ValueError, "rejected")),
        (_http_error(400, json.dumps({"error": {"code": -32004, "message": "Unknown task"}}).encode()), (KeyError, "Unknown task")),
        (_http_error(400, json.dumps({"error": {"code": -32602, "message": "bad params"}}).encode()), (ValueError, "bad params")),
        (_http_error(500, json.dumps({"error": {"code": -32603, "message": "Internal error: X"}}).encode()), (ValueError, "Internal error")),
        (_http_error(502, b"<html>bad gateway</html>"), (DaemonUnavailable, "HTTP 502")),
        (_http_error(400, b'{"error": "forbidden host"}'), (ValueError, "forbidden host")),
        (http.client.BadStatusLine("SSH-2.0"), (DaemonUnavailable, "did not answer")),
        (_Reply(b"not json"), (DaemonUnavailable, "not JSON-RPC")),
        (_Reply(b"[1]"), (DaemonUnavailable, "not JSON-RPC")),
        (_Reply(b'{"jsonrpc": "2.0", "id": 1}'), (DaemonUnavailable, "not JSON-RPC")),
    ],
    ids=["401", "not-found", "invalid-params", "internal", "not-json-error", "plain-error", "non-http", "not-json", "not-object", "no-result"],
)
def test_client_maps_owner_replies_to_the_errors_the_tools_expect(home, monkeypatch, outcome, expected):
    import maestro.daemon_client as dc

    def fake_urlopen(request, timeout):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(dc.urllib.request, "urlopen", fake_urlopen)
    client = DaemonClient(home, "http://127.0.0.1:9", None, 1, fallback=lambda: None)
    error_type, text = expected
    with pytest.raises(error_type) as caught:
        client.agents()
    assert text in str(caught.value)
    if error_type is DaemonUnavailable:
        assert isinstance(caught.value, ValueError)  # the MCP tools report it as an error, not a crash


def test_list_tasks_reports_an_owner_that_went_away(home):
    client = DaemonClient(home, f"http://127.0.0.1:{_closed_port()}", None, 1, fallback=lambda: None)
    with pytest.raises(DaemonUnavailable):
        client.list_tasks()


def test_shutdown_daemon_drops_a_client(owner, home):
    client = dm.get_daemon()
    assert isinstance(client, DaemonClient)
    dm.shutdown_daemon()
    assert dm._instance is None and owner._httpd is not None  # the owner keeps running


# ------------------------------------------------------------ the new JSON-RPC methods
def _rpc(daemon: MaestroDaemon, method: str, params) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=30)
    try:
        conn.request("POST", "/", body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}), headers={"Content-Type": "application/json"})
        return json.loads(conn.getresponse().read())
    finally:
        conn.close()


@pytest.mark.parametrize(
    "method, params, code",
    [
        ("tasks/delegate", {"workspace": "/tmp"}, -32602),
        ("tasks/delegate", {"handoff": {"handoff": {"title": "t", "request": "r"}}}, -32602),
        ("tasks/delegate", {"handoff": "text", "workspace": "/tmp"}, -32602),
        ("tasks/delegate", {"handoff": {"handoff": {"title": "t", "request": "r"}}, "workspace": "/does/not/exist"}, -32602),
        ("tasks/wait", {}, -32602),
        ("tasks/wait", {"id": "task-20260101-000000-abcdef", "timeout": "soon"}, -32602),
        ("tasks/wait", {"id": "task-20260101-000000-abcdef", "timeout": -1}, -32602),
        ("tasks/wait", {"id": "task-20260101-000000-abcdef", "timeout": True}, -32602),
        ("tasks/wait", {"id": "task-20260101-000000-abcdef"}, -32004),
        ("tasks/wait", {"id": "999"}, -32004),
        ("tasks/resolve", {}, -32602),
        ("tasks/resolve", {"id": "999"}, -32004),
        ("tasks/answer", {"id": "task-20260101-000000-abcdef"}, -32602),
        ("tasks/answer", {"answer": "x"}, -32602),
        ("tasks/answer", {"id": "task-20260101-000000-abcdef", "answer": "x"}, -32004),
        ("tasks/answer", {"id": "999", "answer": "x"}, -32004),
    ],
)
def test_new_methods_validate_their_params(owner, method, params, code):
    reply = _rpc(owner, method, params)
    assert reply["error"]["code"] == code, reply


def test_answer_method_reports_a_task_that_is_not_waiting(owner, tmp_path):
    ws = _git_repo(tmp_path)
    started = owner.delegate(_doc("quick"), ws)
    owner.wait(started["task_id"], timeout=20)
    reply = _rpc(owner, "tasks/answer", {"id": started["task_id"], "answer": "x"})
    assert reply["error"]["code"] == -32602 and "not awaiting input" in reply["error"]["message"]


def test_wait_method_caps_its_timeout(owner, agents, tmp_path, monkeypatch):
    import maestro.a2a as a2a

    monkeypatch.setattr(a2a, "MAX_RPC_WAIT_S", 0.2)
    ws = _git_repo(tmp_path)
    started = owner.delegate(_doc(agents), ws)
    began = time.monotonic()
    reply = _rpc(owner, "tasks/wait", {"id": started["task_id"], "timeout": 60})
    assert reply["result"]["task"]["status"]["state"] in ("submitted", "working") and time.monotonic() - began < 10
    unbounded = _rpc(owner, "tasks/wait", {"id": started["task_id"]})
    assert unbounded["result"]["task"]["id"] == started["task_id"]


def test_cancel_and_followup_results_carry_the_daemon_result(owner, agents, tmp_path):
    ws = _git_repo(tmp_path)
    started = owner.delegate(_doc(agents), ws)
    canceled = _rpc(owner, "tasks/cancel", {"id": started["task_id"]})
    assert canceled["result"]["cancel"] == {"task_id": started["task_id"], "state": "canceled"}
    again = _rpc(owner, "tasks/followup", {"id": started["task_id"], "instruction": "go on"})
    assert again["result"]["followup"]["task_id"] == started["task_id"]


def test_tools_report_an_owner_that_stopped_answering(home, monkeypatch):
    """task_wait and agents_list turn DaemonUnavailable into an error result, like the other tools."""
    gone = DaemonClient(home, f"http://127.0.0.1:{_closed_port()}", None, 1, fallback=lambda: None)
    monkeypatch.setattr(mcp_server, "get_daemon", lambda: gone)
    assert "did not answer" in json.loads(mcp_server.task_wait("/tmp", "1"))["error"]
    assert "did not answer" in json.loads(mcp_server.agents_list())["error"]
