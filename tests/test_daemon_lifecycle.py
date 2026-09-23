"""One daemon per state directory, and resource limits on the local HTTP API.

The first half covers what happens when a second daemon starts on a state
directory that a live daemon already owns: a foreground ``maestro-daemon``
refuses to start, the MCP server sends its calls to the owner instead of
starting a daemon, and neither of them marks the live daemon's running tasks
as failed. It also covers when a leftover "working" task is failed at startup
(its recorded runner process is gone), and that the MCP server stops its own
daemon when it exits.

The second half covers the HTTP handler's limits: request bodies with a bad or
oversized Content-Length are refused before anything is read, a server-sent
events (SSE) subscriber that stops reading is disconnected instead of growing a
queue without bound, and a JSON-RPC request that makes the dispatcher raise gets
an error response instead of a dropped connection.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from maestro.daemon import MaestroDaemon
from maestro.events import EventBus, TaskEvent

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed_running_task(daemon: MaestroDaemon, tid: str, ws: Path) -> None:
    """Write the durable claims of a task that ``daemon`` is running right now."""
    daemon.maestro._register_task(tid, "Running task", 1, project_root=str(ws))
    daemon.maestro._write_claim(tid, "task_title", "Running task")
    daemon.maestro._write_claim(tid, "task_workspace", str(ws))
    daemon.maestro._write_claim(tid, "task_request", "Do it")
    daemon.maestro._write_claim(tid, "task_status", "IMPLEMENTING")
    daemon.maestro._write_claim(tid, "task_runtime", json.dumps({"state": "working", "workspace": str(ws), "title": "Running task"}))


def _marker(home: Path) -> dict:
    return json.loads((home / "daemon.json").read_text(encoding="utf-8"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(path))
    return path


@pytest.fixture
def owner(home, tmp_path):
    """A live daemon that owns ``home`` and is running one task."""
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_running_task(d, "task-20260101-000000-live01", ws)
    yield d
    d.stop()


# ------------------------------------------------- one daemon per state directory
def test_second_http_daemon_is_refused_and_does_not_fail_running_tasks(owner, home):
    before = _marker(home)
    with pytest.raises(RuntimeError, match="already owns"):
        MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    # The live daemon's marker is untouched and its running task is still running.
    assert _marker(home) == before
    assert owner.status_a2a("task-20260101-000000-live01")["status"]["state"] == "working"


def test_mcp_server_forwards_to_the_daemon_that_owns_the_directory(owner, home, monkeypatch):
    """The MCP server starts no second daemon; it sends its calls to the owner (see test_daemon_client.py)."""
    import maestro.daemon as dm
    from maestro.daemon_client import DaemonClient

    monkeypatch.setattr(dm, "_instance", None)
    before = _marker(home)
    guest = dm.get_daemon()
    try:
        assert isinstance(guest, DaemonClient) and guest.url == f"http://127.0.0.1:{owner.port}"
        assert _marker(home) == before  # the owner's marker was not overwritten
        assert guest.status_a2a("task-20260101-000000-live01")["status"]["state"] == "working"
        assert dm.get_daemon() is guest  # the singleton is reused
    finally:
        dm.shutdown_daemon()
    # Shutting the MCP server's side down must not remove the owner's marker either.
    assert _marker(home) == before and owner._httpd is not None


def test_embedded_daemon_owns_the_directory_when_it_is_alone(home, monkeypatch):
    import maestro.daemon as dm

    monkeypatch.setattr(dm, "_instance", None)
    d = dm.get_daemon()
    try:
        assert d._httpd is not None and _marker(home)["port"] == d.port
    finally:
        d.stop()
    assert not (home / "daemon.json").exists()


def test_reconciliation_waits_until_no_other_daemon_uses_the_directory(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    first = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    tid = "task-20260101-000000-live02"
    _seed_running_task(first, tid, ws)
    second = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        # The first process is still alive, so its task is not interrupted.
        assert second.status_a2a(tid)["status"]["state"] == "working"
    finally:
        second.stop()
        first.stop()
    # Once every daemon has stopped, the next one reconciles the leftover task.
    third = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        assert third.status_a2a(tid)["status"]["state"] == "failed"
    finally:
        third.stop()


def test_reconciliation_skipped_when_an_older_daemon_owns_the_directory(home, tmp_path, monkeypatch):
    """An older daemon holds no locks; the answering marker alone must stop reconciliation."""
    from maestro import daemonctl

    ws = tmp_path / "ws"
    ws.mkdir()
    seed = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    tid = "task-20260101-000000-live03"
    _seed_running_task(seed, tid, ws)
    seed.stop()
    monkeypatch.setattr(daemonctl, "live_owner", lambda state_dir: daemonctl.DaemonInfo(running=True, pid=1, url="http://127.0.0.1:1"))
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        assert d.status_a2a(tid)["status"]["state"] == "working"
    finally:
        d.stop()


def test_owner_lock_race_is_refused(home, monkeypatch):
    """A daemon that passes the marker check but then loses the lock race refuses to start."""
    from maestro import daemonctl

    monkeypatch.setattr(daemonctl, "acquire_owner_lock", lambda state_dir, wait_s=2.0: None)
    with pytest.raises(RuntimeError, match="already owns"):
        MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    assert not (home / "daemon.json").exists()


def test_bind_failure_releases_the_owner_lock(home):
    """A daemon that cannot bind its port must not keep the directory locked."""
    from maestro import daemonctl

    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        with pytest.raises(OSError):
            MaestroDaemon(state_dir=home, start_http=True, port=busy.getsockname()[1], max_retries=0, backoff_s=0)
    finally:
        busy.close()
    assert daemonctl.owner_lock_holder(home) is None
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    d.stop()


def test_foreground_main_refuses_a_second_daemon(owner, home, capsys):
    from maestro import daemon_main

    result: dict = {}
    thread = threading.Thread(target=lambda: result.setdefault("rc", daemon_main.main(["--state-dir", str(home), "--port", "0"])), daemon=True)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive(), "maestro-daemon started a second daemon instead of refusing"
    assert result["rc"] == 1
    err = capsys.readouterr().err
    assert "already owns" in err and str(owner.port) in err


def test_foreground_daemon_process_refuses_a_second_daemon(owner, home):
    """The refusal also holds across processes, where only the lock and the marker are shared."""
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), MAESTRO_HOME=str(home))
    process = subprocess.Popen(
        [sys.executable, "-m", "maestro.daemon_main", "--state-dir", str(home), "--port", "0"],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        out, err = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("a second maestro-daemon process started on a directory another daemon owns")
    assert process.returncode == 1 and "already owns" in err, (out, err)
    assert owner.status_a2a("task-20260101-000000-live01")["status"]["state"] == "working"


# ------------------------------------------------------------ HTTP request limits
@pytest.fixture
def http_daemon(home):
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    yield d
    d.stop()


def _raw_post(daemon: MaestroDaemon, content_length: str, body: bytes = b"") -> bytes:
    """Send a POST with a hand-written Content-Length and return the raw reply."""
    with socket.create_connection(("127.0.0.1", daemon.port), timeout=5) as sock:
        head = (
            f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{daemon.port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {content_length}\r\n\r\n"
        ).encode()
        sock.sendall(head + body)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def test_negative_content_length_is_refused_without_reading(http_daemon):
    reply = _raw_post(http_daemon, "-1")
    assert reply.startswith(b"HTTP/1.0 400") and b"Content-Length" in reply


def test_non_integer_content_length_is_refused(http_daemon):
    reply = _raw_post(http_daemon, "ten")
    assert reply.startswith(b"HTTP/1.0 400")


def test_oversized_body_is_refused_before_reading(http_daemon):
    reply = _raw_post(http_daemon, str(8 * 1024 * 1024 + 1))  # one byte over the 8 MiB limit
    assert reply.startswith(b"HTTP/1.0 413")


def test_normal_body_still_reaches_the_dispatcher(http_daemon):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "task-20250101-000000-abcdef"}}).encode()
    reply = _raw_post(http_daemon, str(len(body)), body)
    assert reply.startswith(b"HTTP/1.0 200") and b'"result"' in reply  # the dispatcher answered


def test_dispatcher_exception_becomes_a_jsonrpc_error(http_daemon):
    """params that is not an object used to raise inside the handler and drop the connection."""
    conn = http.client.HTTPConnection("127.0.0.1", http_daemon.port, timeout=10)
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tasks/get", "params": ["not", "an", "object"]})
        conn.request("POST", "/", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        reply = json.loads(resp.read())
    finally:
        conn.close()
    assert resp.status == 400 and reply["id"] == 7 and reply["error"]["code"] == -32602


# ----------------------------------------------------- bounded SSE subscriber queues
def test_event_bus_bounded_subscription_marks_overflow():
    bus = EventBus()
    unbounded = bus.subscribe()
    bounded = bus.subscribe(maxsize=2)
    for i in range(3):
        bus.publish(TaskEvent(task_id="t", type="output", data={"i": i}))
    assert bounded.overflowed is True and bounded._q.qsize() == 2
    assert unbounded.overflowed is False and unbounded._q.qsize() == 3
    # A late bounded subscriber catches up on the newest events only.
    late = bus.subscribe(maxsize=2)
    assert [late.get(timeout=1).data["i"] for _ in range(2)] == [1, 2]
    assert late.overflowed is False


def test_sse_subscriber_that_stops_reading_is_disconnected(http_daemon):
    """A client that stops reading must not make the daemon queue events forever."""
    http_daemon.sse_queue_max = 5
    http_daemon.sse_write_timeout_s = 1.0
    sock = socket.create_connection(("127.0.0.1", http_daemon.port), timeout=5)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    try:
        sock.sendall(f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{http_daemon.port}\r\n\r\n".encode())
        deadline = time.monotonic() + 5
        while not http_daemon.bus._subs and time.monotonic() < deadline:
            time.sleep(0.02)
        assert http_daemon.bus._subs, "the SSE handler never subscribed"
        queue_obj = http_daemon.bus._subs[0][0]
        payload = "x" * 65536
        largest = 0
        for i in range(300):  # about 20 MB: far more than the socket buffers hold
            http_daemon.bus.publish(TaskEvent(task_id="t", type="output", data={"i": i, "chunk": payload}))
            largest = max(largest, queue_obj.qsize())
        assert largest <= 5
        deadline = time.monotonic() + 15
        while http_daemon.bus._subs and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not http_daemon.bus._subs, "the stalled SSE subscriber was never disconnected"
    finally:
        sock.close()


def test_sse_stream_ends_when_the_subscriber_fell_behind(http_daemon, monkeypatch):
    """A subscriber whose queue overflowed is told why (an overflow event) and disconnected."""
    real_subscribe = http_daemon.bus.subscribe

    def _already_behind(*types, maxsize=0):
        sub = real_subscribe(*types, maxsize=maxsize)
        sub._q.overflowed = True
        return sub

    monkeypatch.setattr(http_daemon.bus, "subscribe", _already_behind)
    conn = http.client.HTTPConnection("127.0.0.1", http_daemon.port, timeout=10)
    try:
        conn.request("GET", "/events")
        resp = conn.getresponse()
        text = resp.read().decode()
    finally:
        conn.close()
    assert resp.status == 200 and "fell too far behind" in text and text.startswith("event: overflow\n")
    assert not http_daemon.bus._subs


# ------------------------------------------------ review follow-ups (PR #31)
class _BannerServer:
    """A TCP server that answers every connection with one non-HTTP line, the way an SSH server does."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(2)
                conn.recv(4096)
                conn.sendall(b"SSH-2.0-OpenSSH_9.0\r\n")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self.sock.close()


def _users_lock_is_free(home: Path) -> bool:
    """True when no daemon holds the users lock (a fresh descriptor can take it exclusively)."""
    from maestro import daemonctl

    fd = daemonctl.open_lock(home / daemonctl.USERS_LOCK_NAME)
    try:
        return daemonctl.try_exclusive(fd)
    finally:
        os.close(fd)


def test_startup_failure_releases_the_users_lock(home, monkeypatch):
    """A daemon whose startup raises must not leave the users lock held exclusively."""
    from maestro import daemonctl

    def boom(state_dir):
        raise RuntimeError("owner check failed")

    monkeypatch.setattr(daemonctl, "live_owner", boom)
    with pytest.raises(RuntimeError, match="owner check failed"):
        MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    assert _users_lock_is_free(home), "the failed daemon kept the users lock"


def test_a_non_http_reply_on_the_marker_port_does_not_block_later_daemons(home):
    """Reviewer's r1_leak.py: an old-format marker whose port answers with a non-HTTP line."""
    from maestro import daemonctl

    banner = _BannerServer()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / "daemon.json").write_text(json.dumps({"pid": sleeper.pid, "port": banner.port, "host": "127.0.0.1"}), encoding="utf-8")
        info = daemonctl.status(home)
        assert info.running is False and info.stale_marker is True
        d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
        try:
            assert _marker(home)["pid"] == os.getpid()
        finally:
            d.stop()
        assert _users_lock_is_free(home)
    finally:
        banner.close()
        sleeper.kill()
        sleeper.wait()


def _seed_task_with_runner(daemon: MaestroDaemon, tid: str, ws: Path, runner: dict) -> None:
    """Durable claims of a working task whose runtime record names the process that runs it."""
    _seed_running_task(daemon, tid, ws)
    runtime = {"state": "working", "workspace": str(ws), "title": "Running task", "runner": runner}
    daemon.maestro._write_claim(tid, "task_runtime", json.dumps(runtime))


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_task_runtime_records_the_process_that_runs_it(home, tmp_path):
    from maestro import daemonctl
    from maestro.handoff import HandoffDoc

    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        doc = HandoffDoc(title="t", request="r", verification="none", commit_policy="no-commit", target_agent="codex", explicit_target=True)
        task_id, _ = d._make_record(doc, str(tmp_path))
        d._persist(task_id)
        runtime = json.loads(d.maestro._claims(task_id)["task_runtime"])
        assert runtime["runner"] == {"pid": os.getpid(), "started": daemonctl.process_start_token(os.getpid())}
        assert runtime["runner"]["started"]  # ps reports a start time for this process
    finally:
        d.stop()


def test_reconciliation_fails_a_task_whose_runner_died_while_another_daemon_uses_the_directory(home, tmp_path):
    """The users lock is shared by a live embedded daemon, yet the dead runner's task is failed."""
    ws = tmp_path / "ws"
    ws.mkdir()
    guest = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    tid = "task-20260101-000000-dead01"
    try:
        _seed_task_with_runner(guest, tid, ws, {"pid": _dead_pid(), "started": "Thu Jan  1 00:00:00 1970"})
        d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
        try:
            state = d.status_a2a(tid)
            assert state["status"]["state"] == "failed"
            assert "interrupted" in state["metadata"]["error"]
        finally:
            d.stop()
    finally:
        guest.stop()


def test_reconciliation_keeps_a_task_whose_runner_is_still_alive(home, tmp_path):
    """Even a daemon that is alone in the directory leaves a live runner's task alone."""
    from maestro import daemonctl

    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _sleeper()
    tid = "task-20260101-000000-live04"
    try:
        seed = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
        _seed_task_with_runner(seed, tid, ws, {"pid": runner.pid, "started": daemonctl.process_start_token(runner.pid)})
        seed.stop()
        d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
        try:
            assert d.status_a2a(tid)["status"]["state"] == "working"
        finally:
            d.stop()
    finally:
        runner.kill()
        runner.wait()


def test_reconciliation_fails_a_task_whose_runner_pid_was_reused(home, tmp_path):
    """A live pid with a different start time is another process: the runner is gone."""
    ws = tmp_path / "ws"
    ws.mkdir()
    reused = _sleeper()
    guest = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    tid = "task-20260101-000000-reus01"
    try:
        _seed_task_with_runner(guest, tid, ws, {"pid": reused.pid, "started": "Thu Jan  1 00:00:00 1970"})
        d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
        try:
            assert d.status_a2a(tid)["status"]["state"] == "failed"
        finally:
            d.stop()
    finally:
        guest.stop()
        reused.kill()
        reused.wait()


def test_reconciliation_trusts_a_live_runner_pid_without_a_start_time(home, tmp_path):
    """When the start time could not be read, a live pid still counts as the runner."""
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _sleeper()
    guest = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    tid = "task-20260101-000000-nost01"
    try:
        _seed_task_with_runner(guest, tid, ws, {"pid": runner.pid, "started": None})
        d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
        try:
            assert d.status_a2a(tid)["status"]["state"] == "working"
        finally:
            d.stop()
    finally:
        guest.stop()
        runner.kill()
        runner.wait()


@pytest.mark.parametrize("runner", [{"pid": "12"}, {"pid": True}, {"pid": 0}, "not-a-dict"])
def test_reconciliation_treats_a_malformed_runner_like_an_older_record(home, tmp_path, runner):
    """A runner field that names no usable pid falls back to the older rule: only a daemon alone may fail it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    guest = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    tid = "task-20260101-000000-malf01"
    try:
        _seed_task_with_runner(guest, tid, ws, runner)
        d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
        try:
            assert d.status_a2a(tid)["status"]["state"] == "working"  # not alone: left alone
        finally:
            d.stop()
    finally:
        guest.stop()
    alone = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        assert alone.status_a2a(tid)["status"]["state"] == "failed"
    finally:
        alone.stop()


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _fake_agent(bin_dir: Path, name: str, body: str) -> None:
    import stat as _stat

    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)


def _git_workspace(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(ws), "init", "-q"], env=env, check=True)
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "."], env=env, check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "initial"], env=env, check=True)
    return ws


_OWNER_RUNS_A_TASK = r"""
import sys
import time
from maestro.agents import AgentSpec
from maestro.daemon import MaestroDaemon
from maestro.handoff import HandoffDoc

d = MaestroDaemon(state_dir=sys.argv[1], start_http=True, port=0, max_retries=0, backoff_s=0)
d.registry.save(AgentSpec(name="sleepy", kind="generic", command="sleepy --go"))
doc = HandoffDoc(title="t", request="r", verification="none", commit_policy="no-commit", target_agent="sleepy", explicit_target=True)
started = d.delegate(doc, sys.argv[2])
d.wait(started["task_id"], timeout=30, stop_states=("working",))
print(started["task_id"], flush=True)
time.sleep(120)
"""


def test_task_of_a_killed_owner_is_failed_on_restart_while_an_embedded_daemon_is_alive(home, tmp_path):
    """Reviewer's r4_reconcile.py: the owner dies by SIGKILL while an MCP server's daemon still uses the directory."""
    import signal

    bin_dir = tmp_path / "bin"
    _fake_agent(bin_dir, "sleepy", "cat > /dev/null\nsleep 60")
    ws = _git_workspace(tmp_path)
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), MAESTRO_HOME=str(home), MAESTRO_DISCOVERY="0", PATH=f"{bin_dir}:{os.environ['PATH']}")
    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER_RUNS_A_TASK, str(home), str(ws)],
        cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    guest = None
    try:
        tid = owner.stdout.readline().strip()
        assert tid.startswith("task-"), owner.stderr.read() if owner.poll() is not None else tid
        guest = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
        assert guest.status_a2a(tid)["status"]["state"] == "working"  # the owner is alive: left alone
        os.killpg(owner.pid, signal.SIGKILL)
        owner.wait(timeout=10)
        restarted = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
        try:
            assert restarted.status_a2a(tid)["status"]["state"] == "failed"
        finally:
            restarted.stop()
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait()
        if guest is not None:
            guest.stop()


_MCP_SERVER_WITH_DAEMON = r"""
import sys
import time
from maestro import daemon, mcp_server

mcp_server._stop_daemon_on_exit()
d = daemon.get_daemon()
print(d.port, flush=True)
if sys.argv[1] == "exit":
    sys.exit(0)
time.sleep(120)
"""


@pytest.mark.parametrize("how", ["sigterm", "exit"])
def test_mcp_server_stops_its_daemon_on_exit(home, tmp_path, how):
    """The MCP server's daemon removes its marker and releases its locks when the server exits."""
    import signal

    from maestro import daemonctl

    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), MAESTRO_HOME=str(home), MAESTRO_DISCOVERY="0")
    process = subprocess.Popen(
        [sys.executable, "-c", _MCP_SERVER_WITH_DAEMON, how],
        cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        line = process.stdout.readline().strip()
        assert line.isdigit(), process.stderr.read() if process.poll() is not None else line
        if how == "sigterm":
            assert daemonctl.owner_lock_holder(home) == process.pid
            process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert not (home / "daemon.json").exists()
    assert daemonctl.owner_lock_holder(home) is None
    assert _users_lock_is_free(home)
    if how == "sigterm":
        assert process.returncode == -signal.SIGTERM  # the signal still ends the process as before


def test_mcp_server_main_stops_the_daemon_when_the_client_disconnects(home, monkeypatch):
    import atexit
    import signal

    import maestro.daemon as dm
    from maestro import mcp_server

    registered: list = []
    handlers: dict = {}
    monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    monkeypatch.setattr(dm, "_instance", None)
    started: list = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: started.append(dm.get_daemon()))
    mcp_server.main()
    assert started and started[0]._stopped is True and dm._instance is None
    assert not (home / "daemon.json").exists()
    assert registered == [dm.shutdown_daemon] and signal.SIGTERM in handlers


def test_mcp_server_sigterm_handler_stops_the_daemon_then_ends_the_process(home, monkeypatch):
    import signal

    import maestro.daemon as dm
    from maestro import mcp_server

    handlers: dict = {}
    monkeypatch.setattr(mcp_server.atexit, "register", lambda fn: None)
    monkeypatch.setattr(mcp_server.signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    kills: list = []
    monkeypatch.setattr(mcp_server.os, "kill", lambda pid, signum: kills.append((pid, signum)))
    monkeypatch.setattr(dm, "_instance", None)
    mcp_server._stop_daemon_on_exit()
    d = dm.get_daemon()
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert d._stopped is True and dm._instance is None
    assert handlers[signal.SIGTERM] == signal.SIG_DFL and kills == [(os.getpid(), signal.SIGTERM)]


def test_shutdown_daemon_does_not_wait_forever_for_a_held_lock(home, monkeypatch):
    """A signal can arrive while get_daemon holds the lock on the same thread; shutdown still runs."""
    import maestro.daemon as dm

    monkeypatch.setattr(dm, "_instance", None)
    monkeypatch.setattr(dm, "SHUTDOWN_LOCK_WAIT_S", 0.05)
    d = dm.get_daemon()
    with dm._instance_lock:
        dm.shutdown_daemon()
    assert d._stopped is True and dm._instance is None
    dm.shutdown_daemon()  # nothing left to stop: a no-op


# ------------------------------------------------ 413 is delivered, not reset
def test_oversized_body_sent_by_urllib_gets_a_413(http_daemon):
    """Reviewer's r2_413.py: a normal client that sends the whole body must read the 413."""
    import urllib.error
    import urllib.request

    body = b"x" * (9 * 1024 * 1024)
    for _ in range(3):
        request = urllib.request.Request(f"http://127.0.0.1:{http_daemon.port}/", data=body, headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=30)
        assert caught.value.code == 413
        assert b"larger than" in caught.value.read()


def test_oversized_body_sent_on_a_raw_socket_gets_a_413(http_daemon):
    size = 9 * 1024 * 1024
    with socket.create_connection(("127.0.0.1", http_daemon.port), timeout=30) as sock:
        sock.sendall(f"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {size}\r\n\r\n".encode())
        sock.sendall(b"x" * size)
        reply = sock.recv(200)
    assert reply.startswith(b"HTTP/1.0 413")


def test_bad_content_length_with_a_body_still_gets_its_400(http_daemon):
    with socket.create_connection(("127.0.0.1", http_daemon.port), timeout=30) as sock:
        sock.sendall(b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: -5\r\n\r\n" + b"x" * 200_000)
        reply = sock.recv(200)
    assert reply.startswith(b"HTTP/1.0 400")


def test_body_larger_than_the_drain_limit_is_refused_without_waiting(http_daemon, monkeypatch):
    """Past the drain limit the daemon reads only that much, so a huge declared size costs a bounded read."""
    import maestro.daemon as dm

    monkeypatch.setattr(dm, "MAX_DRAIN_BYTES", 1024)
    size = 9 * 1024 * 1024
    with socket.create_connection(("127.0.0.1", http_daemon.port), timeout=30) as sock:
        sock.sendall(f"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {size}\r\n\r\n".encode() + b"x" * 4096)
        assert sock.recv(200).startswith(b"HTTP/1.0 413")


def test_drain_stops_when_the_client_goes_quiet(http_daemon, monkeypatch):
    """A client that declares a body and never sends it holds the handler only for the drain timeout."""
    import maestro.daemon as dm

    monkeypatch.setattr(dm, "DRAIN_TIMEOUT_S", 0.2)
    size = 9 * 1024 * 1024
    with socket.create_connection(("127.0.0.1", http_daemon.port), timeout=30) as sock:
        sock.sendall(f"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {size}\r\n\r\n".encode())
        started = time.monotonic()
        reply = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
        assert reply.startswith(b"HTTP/1.0 413") and time.monotonic() - started < 10
        time.sleep(0.5)  # keep the connection open and silent past the drain timeout
    # The handler gave up on the silent client and the daemon still serves requests.
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "task-20250101-000000-abcdef"}}).encode()
    assert _raw_post(http_daemon, str(len(body)), body).startswith(b"HTTP/1.0 200")


# ------------------------------------------------ SSE overflow is announced and survived
def _overflowing_subscribe(daemon: MaestroDaemon, monkeypatch) -> None:
    """Make every new SSE subscription start out as having fallen behind."""
    real_subscribe = daemon.bus.subscribe

    def _already_behind(*types, maxsize=0):
        sub = real_subscribe(*types, maxsize=maxsize)
        sub._q.overflowed = True
        return sub

    monkeypatch.setattr(daemon.bus, "subscribe", _already_behind)


def _read_stream(daemon: MaestroDaemon, path: str) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        assert resp.status == 200
        return resp.read().decode()
    finally:
        conn.close()


def test_task_stream_announces_overflow_for_a_running_task(owner, monkeypatch):
    """Reviewer's r6_sse.py: a subscriber that fell behind is told so with an event it can act on."""
    _overflowing_subscribe(owner, monkeypatch)
    text = _read_stream(owner, "/tasks/task-20260101-000000-live01/events")
    assert text.startswith("event: overflow\n")
    envelope = json.loads(text.split("data: ", 1)[1].split("\n", 1)[0])
    assert envelope["task_id"] == "task-20260101-000000-live01" and envelope["data"]["state"] == "working"
    assert "fell too far behind" in envelope["data"]["reason"]
    assert not owner.bus._subs


def test_task_stream_sends_the_final_state_when_the_task_ended_meanwhile(owner, monkeypatch):
    """A task that finished while the subscriber was behind gets its final state, not an overflow."""
    from maestro.events import TaskEvent as _TaskEvent

    _overflowing_subscribe(owner, monkeypatch)
    answers = iter([None, _TaskEvent(task_id="task-20260101-000000-live01", type="state", data={"state": "completed"})])
    monkeypatch.setattr(owner, "final_state_event", lambda task_id: next(answers))
    text = _read_stream(owner, "/tasks/task-20260101-000000-live01/events")
    assert text.startswith("event: state\n") and '"completed"' in text and "overflow" not in text


def test_global_stream_announces_overflow(http_daemon, monkeypatch):
    _overflowing_subscribe(http_daemon, monkeypatch)
    text = _read_stream(http_daemon, "/events")
    assert text.startswith("event: overflow\n") and "fell too far behind" in text


def _frame(event: str, seq: int, task_id: str = "t1", **data) -> bytes:
    from maestro.a2a import sse_encode

    return sse_encode(event, {"task_id": task_id, "type": event, "data": data, "seq": seq}).encode()


class _ScriptedStreams:
    """An A2A-shaped server: each event-stream request gets the next scripted body,
    and ``tasks/get`` reports ``task_state``."""

    def __init__(self, streams: list[bytes], task_state: str | None = "completed", task_error: str | None = None) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.streams = list(streams)
        self.paths: list[str] = []
        self.methods: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                outer.paths.append(self.path)
                body = outer.streams.pop(0) if outer.streams else b""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                outer.methods.append(payload["method"])
                if payload["method"] == "message/send":
                    result = {"task": {"kind": "task", "id": "t1", "status": {"state": "submitted"}}}
                elif task_state is None:
                    self.send_response(500)
                    self.end_headers()
                    return
                else:
                    result = {"task": {"kind": "task", "id": "t1", "status": {"state": task_state}, "metadata": {"error": task_error}}}
                out = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


_OVERFLOW = b'event: overflow\ndata: {"task_id": "t1", "type": "overflow", "data": {"reason": "subscriber fell too far behind"}, "seq": 0}\n\n'


def test_follow_task_events_reconnects_after_overflow_without_repeating_events():
    from maestro.a2a_client import follow_task_events

    server = _ScriptedStreams([
        _frame("output", 1, line="one") + _OVERFLOW,
        _frame("output", 1, line="one") + _frame("output", 2, line="two") + _frame("state", 3, state="completed"),
    ])
    try:
        events = [(name, env["data"]) for name, env in follow_task_events(server.url, "t1")]
    finally:
        server.close()
    assert events == [
        ("output", {"line": "one"}),
        ("overflow", {"reason": "subscriber fell too far behind"}),
        ("output", {"line": "two"}),
        ("state", {"state": "completed"}),
    ]
    assert server.paths == ["/tasks/t1/events", "/tasks/t1/events"] and server.methods == []


def test_follow_task_events_asks_for_the_state_when_the_stream_just_ends():
    from maestro.a2a_client import follow_task_events

    server = _ScriptedStreams([_frame("output", 1, line="one")], task_state="failed", task_error="agent crashed")
    try:
        events = [(name, env["data"]) for name, env in follow_task_events(server.url, "t1")]
    finally:
        server.close()
    assert events == [("output", {"line": "one"}), ("state", {"state": "failed", "error": "agent crashed"})]
    assert server.methods == ["tasks/get"]


@pytest.mark.parametrize("task_state", ["working", None], ids=["still-running", "tasks-get-fails"])
def test_follow_task_events_ends_without_a_state_when_the_task_has_not_finished(task_state):
    from maestro.a2a_client import follow_task_events

    server = _ScriptedStreams([_frame("output", 1, line="one")], task_state=task_state)
    try:
        events = [name for name, _ in follow_task_events(server.url, "t1")]
    finally:
        server.close()
    assert events == ["output"]


def test_follow_task_events_reports_a_completed_state_without_an_error():
    from maestro.a2a_client import follow_task_events

    server = _ScriptedStreams([b""], task_state="completed")
    try:
        events = [(name, env["data"]) for name, env in follow_task_events(server.url, "t1")]
    finally:
        server.close()
    assert events == [("state", {"state": "completed"})]


def test_task_tail_reconnects_after_overflow_and_exits_with_the_final_state(capsys):
    from maestro import cli

    server = _ScriptedStreams([
        _frame("output", 1, line="one") + _OVERFLOW,
        _frame("output", 1, line="one") + _frame("output", 2, line="two") + _frame("state", 3, state="completed"),
    ])
    try:
        code = cli._stream_task(server.url, "t1")
    finally:
        server.close()
    out = capsys.readouterr().out
    assert code == 0
    assert out.count("one\n") == 1 and "two\n" in out and "[state] completed" in out
    assert "fell behind" in out  # the user is told that some output may be missing


def test_task_tail_all_reconnects_after_overflow(capsys):
    from maestro import cli

    server = _ScriptedStreams([
        _frame("output", 1, task_id="a", line="one") + _OVERFLOW,
        _frame("output", 1, task_id="a", line="one") + _frame("output", 2, task_id="b", line="two"),
    ])
    try:
        code = cli._stream_task(server.url, None)
    finally:
        server.close()
    out = capsys.readouterr().out
    assert code == 0 and out.count("one\n") == 1 and "two\n" in out
    assert server.paths == ["/events", "/events"]


def test_a2a_remote_adapter_keeps_following_the_task_after_overflow(tmp_path):
    from maestro.adapters.a2a_remote import A2ARemoteAdapter
    from maestro.agents import AgentSpec

    server = _ScriptedStreams([
        _frame("output", 1, line="one") + _OVERFLOW,
        _frame("output", 2, line="two") + _frame("state", 3, state="completed"),
    ])
    lines: list[str] = []
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="remote", kind="a2a_remote", command=server.url))
        result = adapter.run("prompt", tmp_path, "task-local", timeout=20, on_line=lines.append)
    finally:
        server.close()
    assert result.ok is True and lines == ["one", "two"]


def test_a2a_remote_adapter_uses_the_state_the_remote_reports_when_the_stream_ends(tmp_path):
    from maestro.adapters.a2a_remote import A2ARemoteAdapter
    from maestro.agents import AgentSpec

    server = _ScriptedStreams([_frame("output", 1, line="one")], task_state="failed", task_error="remote agent crashed")
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="remote", kind="a2a_remote", command=server.url))
        result = adapter.run("prompt", tmp_path, "task-local", timeout=20)
    finally:
        server.close()
    assert result.ok is False and result.error == "remote agent crashed"


# ------------------------------------------------ identity of old-format markers
def test_old_format_marker_pointing_at_another_daemon_is_stale_and_not_signalled(home, tmp_path):
    """Reviewer's r8_oldmarker.py: the port answers as a Maestro daemon, but for another state directory and pid."""
    from maestro import daemonctl

    other = MaestroDaemon(state_dir=tmp_path / "b", start_http=True, port=0, max_retries=0, backoff_s=0)
    unrelated = _sleeper()
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / "daemon.json").write_text(json.dumps({"pid": unrelated.pid, "port": other.port, "host": "127.0.0.1"}), encoding="utf-8")
        info = daemonctl.status(home)
        assert info.running is False and info.stale_marker is True
        stopped = daemonctl.stop(home, grace_s=1)
        time.sleep(0.2)
        assert unrelated.poll() is None, "stop() signalled a process that is not this directory's daemon"
        assert "not signalled" in stopped.detail
    finally:
        unrelated.kill()
        unrelated.wait()
        other.stop()


def test_old_format_marker_of_the_real_daemon_is_still_confirmed(home):
    """A marker without owner_lock is confirmed when the agent card names the same pid and state directory."""
    from maestro import daemonctl

    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        marker = _marker(home)
        marker.pop("owner_lock")
        (home / "daemon.json").write_text(json.dumps(marker), encoding="utf-8")
        card = json.loads(_read_card(d))
        assert card["maestro"] == {"pid": os.getpid(), "state_dir": str(home)}
        info = daemonctl.status(home)
        assert info.running is True and info.pid == os.getpid()
    finally:
        d.stop()


def _read_card(daemon: MaestroDaemon) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
    try:
        conn.request("GET", "/.well-known/agent.json")
        return conn.getresponse().read().decode()
    finally:
        conn.close()


# ------------------------------------------------ the CLI confirms the daemon too
def test_cli_endpoint_rejects_a_marker_whose_pid_is_not_the_daemon(home, monkeypatch):
    """Reviewer's r5_cli.py: a live pid alone does not make a daemon reachable."""
    from maestro import cli

    monkeypatch.delenv("MAESTRO_DAEMON_URL", raising=False)
    sleeper = _sleeper()
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / "daemon.json").write_text(json.dumps({"pid": sleeper.pid, "port": 1, "host": "127.0.0.1", "owner_lock": True}), encoding="utf-8")
        with pytest.raises(ValueError, match="no daemon reachable"):
            cli._daemon_endpoint()
    finally:
        sleeper.kill()
        sleeper.wait()


def test_cli_endpoint_returns_the_confirmed_daemon(home, monkeypatch):
    from maestro import cli

    monkeypatch.delenv("MAESTRO_DAEMON_URL", raising=False)
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        assert cli._daemon_endpoint() == (f"http://127.0.0.1:{d.port}", None)
    finally:
        d.stop()
