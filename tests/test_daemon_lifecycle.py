"""One daemon per state directory, and resource limits on the local HTTP API.

The first half covers what happens when a second daemon starts on a state
directory that a live daemon already owns: a foreground ``maestro-daemon``
refuses to start, the MCP server's embedded daemon runs without an HTTP
endpoint, and neither of them marks the live daemon's running tasks as failed.

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


def test_embedded_daemon_runs_without_http_when_another_daemon_owns_the_directory(owner, home, monkeypatch, capsys):
    import maestro.daemon as dm

    monkeypatch.setattr(dm, "_instance", None)
    before = _marker(home)
    guest = dm.get_daemon()
    try:
        assert guest is not owner and guest._httpd is None and guest.port is None
        assert _marker(home) == before  # the owner's marker was not overwritten
        assert guest.status_a2a("task-20260101-000000-live01")["status"]["state"] == "working"
        assert "without an HTTP endpoint" in capsys.readouterr().err
        assert dm.get_daemon() is guest  # the singleton is reused
    finally:
        guest.stop()
    # Stopping the embedded daemon must not remove the owner's marker either.
    assert _marker(home) == before


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
    """A subscriber whose queue overflowed is told why and disconnected, so it can reconnect."""
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
    assert resp.status == 200 and "fell too far behind" in text
    assert not http_daemon.bus._subs
