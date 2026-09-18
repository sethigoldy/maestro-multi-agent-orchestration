"""v2-M3: api adapter mode and the a2a_remote (daemon-to-daemon) adapter."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from maestro.adapters import make_adapter
from maestro.adapters.a2a_remote import A2ARemoteAdapter
from maestro.adapters.generic import GenericAdapter
from maestro.a2a_client import fetch_agent_card, post_jsonrpc, sse_events
from maestro.agents import AgentSpec
from maestro.daemon import MaestroDaemon
from maestro.handoff import HandoffDoc


# ------------------------------------------------------------ a2a_client

class _JsonRpcServer:
    """Minimal A2A-shaped server for client unit tests."""

    def __init__(self, tmp_path, card: dict | None = None, no_task_id: bool = False, sse_error: int | None = None, cancel_fails: bool = False, hold_sse: bool = False):
        self.requests: list[tuple[str, bytes]] = []
        self.sse_body = b""
        self.hold_sse = hold_sse

        class Handler(BaseHTTPRequestHandler):
            outer = self

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/.well-known/agent.json":
                    if card is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = json.dumps(card).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.startswith("/tasks/") and self.path.endswith("/events"):
                    if self.outer.hold_sse:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        try:
                            while True:
                                time.sleep(0.5)
                        except OSError:
                            pass
                    elif sse_error is not None:
                        self.send_response(sse_error)
                        self.end_headers()
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        self.wfile.write(self.outer.sse_body)
                        self.wfile.flush()
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                self.outer.requests.append((self.path, body))
                payload = json.loads(body.decode())
                if cancel_fails and payload.get("method") == "tasks/cancel":
                    self.send_response(500)
                    self.end_headers()
                    return
                if payload.get("method") == "message/send":
                    if no_task_id:
                        result = {"task": {"kind": "task", "status": {"state": "submitted"}}}
                    else:
                        result = {"task": {"kind": "task", "id": "remote-1", "status": {"state": "submitted"}}}
                elif payload.get("method") == "tasks/cancel":
                    result = {"task": {"kind": "task", "id": "remote-1", "status": {"state": "canceled"}}}
                else:
                    self.send_response(400)
                    err = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601, "message": "Method not found"}}).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return
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

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_post_jsonrpc_ok_and_error(tmp_path):
    srv = _JsonRpcServer(tmp_path)
    try:
        result = post_jsonrpc(srv.url, "message/send", {"message": {}})
        assert result["task"]["id"] == "remote-1"
        with pytest.raises(ValueError, match="Method not found"):
            post_jsonrpc(srv.url, "bogus/method", {})
    finally:
        srv.close()


def test_post_jsonrpc_unreachable():
    with pytest.raises(ValueError, match="cannot reach"):
        post_jsonrpc("http://127.0.0.1:1", "message/send", {})


def test_sse_events_parses_frames(tmp_path):
    srv = _JsonRpcServer(tmp_path)
    srv.sse_body = (
        b": keepalive\n\n"
        + b'event: output\ndata: {"task_id": "t", "type": "output", "data": {"line": "hello"}}\n\n'
        + b'event: state\ndata: {"task_id": "t", "type": "state", "data": {"state": "completed"}}\n\n'
    )
    try:
        events = list(sse_events(srv.url, "/tasks/t/events"))
        assert [e for e, _ in events] == ["output", "state"]
        assert events[0][1]["data"]["line"] == "hello"
    finally:
        srv.close()


def test_fetch_agent_card_variants(tmp_path):
    card = {"name": "node-a", "version": "1.0"}
    srv = _JsonRpcServer(tmp_path, card=card)
    try:
        got = fetch_agent_card(srv.url)
        assert got["name"] == "node-a"
    finally:
        srv.close()
    no_card = _JsonRpcServer(tmp_path, card=None)
    try:
        with pytest.raises(ValueError, match="HTTP 404"):
            fetch_agent_card(no_card.url)
    finally:
        no_card.close()
    with pytest.raises(ValueError, match="cannot fetch agent card"):
        fetch_agent_card("http://127.0.0.1:1")


# ------------------------------------------------------------ api mode

class _RestTaskServer:
    """Implements the generic REST task contract with a scripted state machine."""

    def __init__(self, states: list[dict], hold: bool = False):
        self.cancel_hits: list[str] = []
        self.submit_fail = False

        class Handler(BaseHTTPRequestHandler):
            outer = self

            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode() or "{}")
                if self.path == "/tasks":
                    if self.outer.submit_fail:
                        self.send_response(500)
                        self.end_headers()
                        return
                    self.send_response(202)
                    out = json.dumps({"id": "r-1"}).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                elif self.path == "/tasks/r-1/cancel":
                    self.outer.cancel_hits.append(self.path)
                    out = json.dumps({"ok": True}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

            def do_GET(self):
                if self.path == "/":
                    out = json.dumps({"ok": True}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                elif self.path == "/tasks/r-1":
                    state = states[min(self.outer.polls, len(states) - 1)]
                    self.outer.polls += 1
                    out = json.dumps(state).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                else:
                    self.send_response(404)
                    self.end_headers()

        self.polls = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _api_adapter(url: str) -> GenericAdapter:
    return GenericAdapter(AgentSpec(name="remote", kind="generic", command=url))


def test_api_mode_success_with_usage_and_lines(tmp_path):
    srv = _RestTaskServer([
        {"state": "working", "output": "line one\n"},
        {"state": "working", "output": "line one\nline two\n"},
        {"state": "completed", "output": "line one\nline two\ndone\n", "usage": {"cost_usd": 0.42}},
    ])
    try:
        lines: list[str] = []
        result = _api_adapter(srv.url).run(
            "do it", Path(tmp_path), "task-x", timeout=30, on_line=lines.append
        )
        assert result.ok and result.usage == {"cost_usd": 0.42}
        assert lines == ["line one", "line two", "done"]
    finally:
        srv.close()


def test_api_mode_failure_state(tmp_path):
    srv = _RestTaskServer([{"state": "failed", "error": "remote exploded", "usage": {"cost_usd": 0.1}}])
    try:
        result = _api_adapter(srv.url).run("do it", Path(tmp_path), "task-x", timeout=30)
        assert not result.ok and "remote exploded" in (result.error or "")
        assert result.usage == {"cost_usd": 0.1}
    finally:
        srv.close()


def test_api_mode_timeout(tmp_path):
    srv = _RestTaskServer([{"state": "working", "output": ""}])
    try:
        result = _api_adapter(srv.url).run("do it", Path(tmp_path), "task-x", timeout=2)
        assert not result.ok and "timed out" in (result.error or "")
    finally:
        srv.close()


def test_api_mode_cancel(tmp_path):
    states = [{"state": "working", "output": ""}, {"state": "working", "output": ""}, {"state": "canceled", "error": "canceled by orchestrator"}]
    srv = _RestTaskServer(states)

    class Adapter(GenericAdapter):
        def api_poll_interval_s(self):
            return 0.2

    flag = threading.Event()
    import time as _time

    def cancel_later():
        _time.sleep(0.15)
        flag.set()

    threading.Thread(target=cancel_later, daemon=True).start()
    try:
        result = Adapter(AgentSpec(name="remote", kind="generic", command=srv.url)).run(
            "do it", Path(tmp_path), "task-x", timeout=30, should_cancel=lambda: flag.is_set()
        )
        assert not result.ok and srv.cancel_hits == ["/tasks/r-1/cancel"]
    finally:
        srv.close()


def test_api_mode_submit_failure(tmp_path):
    srv = _RestTaskServer([{"state": "working"}])
    srv.submit_fail = True
    try:
        result = _api_adapter(srv.url).run("do it", Path(tmp_path), "task-x", timeout=10)
        assert not result.ok and "Failed to submit" in (result.error or "")
    finally:
        srv.close()


def test_api_mode_unreachable_submit():
    result = _api_adapter("http://127.0.0.1:1").run("do it", Path("/tmp"), "task-x", timeout=10)
    assert not result.ok and "Failed to submit" in (result.error or "")


def test_generic_api_preflight(tmp_path):
    srv = _RestTaskServer([{"state": "working"}])
    try:
        preflight = _api_adapter(srv.url).preflight()
        assert preflight.ok and preflight.binary == srv.url
    finally:
        srv.close()
    bad = _api_adapter("http://127.0.0.1:1")
    assert not bad.preflight().ok


def test_generic_non_url_stays_spawn():
    adapter = GenericAdapter(AgentSpec(name="cli", kind="generic", command="/bin/echo hi"))
    assert adapter.mode == "spawn"


# ------------------------------------------------------------ a2a_remote

def test_a2a_remote_requires_url_spec():
    with pytest.raises(ValueError):
        A2ARemoteAdapter(AgentSpec(name="x", kind="a2a_remote", command="not-a-url"))
    assert isinstance(make_adapter(AgentSpec(name="x", kind="a2a_remote", command="http://127.0.0.1:9")), A2ARemoteAdapter)


def test_a2a_remote_preflight(tmp_path):
    srv = _JsonRpcServer(tmp_path, card={"name": "node-a", "version": "1.0"})
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="remote", kind="a2a_remote", command=srv.url))
        preflight = adapter.preflight()
        assert preflight.ok and "node-a" in (preflight.version or "")
    finally:
        srv.close()
    bad = A2ARemoteAdapter(AgentSpec(name="remote", kind="a2a_remote", command="http://127.0.0.1:1"))
    assert not bad.preflight().ok


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


def test_a2a_remote_daemon_to_daemon(tmp_path, monkeypatch):
    """The flagship path: an a2a_remote adapter delegates to a live remote daemon."""
    home = tmp_path / "remote-home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_bin(bin_dir, "codex", 'cat > /dev/null\necho remote-work-done\necho \'{"total_cost_usd": 0.31}\'\nexit 0')
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    ws = _git_repo(tmp_path, "remote-ws")
    remote = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        remote.registry.save(AgentSpec(name="codex", kind="codex"))
        adapter = A2ARemoteAdapter(AgentSpec(name="remote-node", kind="a2a_remote", command=f"http://127.0.0.1:{remote.port}"))
        doc = HandoffDoc(title="Remote work", request="Implement it", verification="none", commit_policy="no-commit")
        lines: list[str] = []
        result = adapter.run(
            "prompt text", ws, "local-task-1",
            settings={"maestro_handoff": doc.to_dict()},
            timeout=60, on_line=lines.append,
        )
        assert result.ok, result.error
        assert "remote-work-done" in lines
        assert (result.usage or {}).get("cost_usd") == 0.31
    finally:
        remote.stop()


def test_a2a_remote_queued_remote(tmp_path):
    class QueuedHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            out = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "result": {"task": {"kind": "task", "id": None, "status": {"state": "submitted"}, "metadata": {"queued": True}}},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    server = ThreadingHTTPServer(("127.0.0.1", 0), QueuedHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="remote", kind="a2a_remote", command=f"http://127.0.0.1:{server.server_address[1]}"))
        result = adapter.run("prompt", Path(tmp_path), "task-q", timeout=10)
        assert not result.ok and "queued" in (result.error or "")
    finally:
        server.shutdown()
        server.server_close()


def test_a2a_remote_stream_closes_without_terminal_state(tmp_path):
    srv = _JsonRpcServer(tmp_path)  # /tasks/*/events returns immediately, no frames
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="remote", kind="a2a_remote", command=srv.url))
        result = adapter.run("prompt", Path(tmp_path), "task-n", timeout=10)
        assert not result.ok and "closed before a terminal state" in (result.error or "")
    finally:
        srv.close()


def test_a2a_remote_cancel_sent(tmp_path):
    class CancelHandler(BaseHTTPRequestHandler):
        cancel_hits: list[str] = []

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/.well-known/"):
                out = json.dumps({"name": "n", "version": "1"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            elif self.path.endswith("/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                frame = (
                    b'event: output\ndata: {"task_id": "remote-1", "type": "output", "data": {"line": "working..."}}\n\n'
                    b'event: state\ndata: {"task_id": "remote-1", "type": "state", "data": {"state": "canceled", "error": "user canceled"}}\n\n'
                )
                self.wfile.write(frame)
                self.wfile.flush()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode())
            if body.get("method") == "message/send":
                out = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"task": {"kind": "task", "id": "remote-1", "status": {"state": "submitted"}}}}).encode()
            else:
                CancelHandler.cancel_hits.append(body.get("method"))
                out = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"task": {"kind": "task", "id": "remote-1", "status": {"state": "canceled"}}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    server = ThreadingHTTPServer(("127.0.0.1", 0), CancelHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="remote", kind="a2a_remote", command=f"http://127.0.0.1:{server.server_address[1]}"))
        result = adapter.run("prompt", Path(tmp_path), "task-c", timeout=10, should_cancel=lambda: True)
        assert not result.ok and "canceled" in (result.error or "")
        assert CancelHandler.cancel_hits == ["tasks/cancel"]
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------- branch-completeness tests

def test_post_jsonrpc_non_jsonrpc_error_body(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"foo": 1}).encode()  # JSON but not a JSON-RPC error
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ValueError, match="non JSON-RPC body"):
            post_jsonrpc(f"http://127.0.0.1:{server.server_address[1]}", "message/send", {})
    finally:
        server.shutdown()
        server.server_close()


def test_post_jsonrpc_non_object_payload(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b'[1, 2, 3]'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ValueError, match="non-object"):
            post_jsonrpc(f"http://127.0.0.1:{server.server_address[1]}", "message/send", {})
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_agent_card_non_object(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'[1]'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ValueError, match="not a JSON object"):
            fetch_agent_card(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()


def test_a2a_remote_binary_and_missing_url(tmp_path):
    adapter = A2ARemoteAdapter(AgentSpec(name="x", kind="a2a_remote", command="http://127.0.0.1:9"))
    assert adapter.binary() == "http://127.0.0.1:9"
    adapter.spec = None
    assert adapter.binary() is None
    adapter.spec = AgentSpec(name="x", kind="a2a_remote")  # no command at all
    preflight = adapter.preflight()
    assert not preflight.ok and "base URL" in (preflight.error or "")
    result = adapter.run("p", Path(tmp_path), "t1")
    assert not result.ok and "base URL" in (result.error or "")


def test_a2a_remote_send_failure_and_missing_id(tmp_path):
    unreachable = A2ARemoteAdapter(AgentSpec(name="x", kind="a2a_remote", command="http://127.0.0.1:1"))
    result = unreachable.run("p", Path(tmp_path), "t2", timeout=10)
    assert not result.ok and "message/send failed" in (result.error or "")

    srv = _JsonRpcServer(tmp_path, no_task_id=True)
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="x", kind="a2a_remote", command=srv.url))
        result = adapter.run("p", Path(tmp_path), "t3", timeout=10)
        assert not result.ok and "no task id" in (result.error or "")
    finally:
        srv.close()


def test_a2a_remote_stream_error_and_timeout(tmp_path):
    err_srv = _JsonRpcServer(tmp_path, sse_error=503)
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="x", kind="a2a_remote", command=err_srv.url))
        result = adapter.run("p", Path(tmp_path), "t4", timeout=10)
        assert not result.ok and "event stream failed" in (result.error or "")
    finally:
        err_srv.close()

    hold_srv = _JsonRpcServer(tmp_path, hold_sse=True)
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="x", kind="a2a_remote", command=hold_srv.url))
        result = adapter.run("p", Path(tmp_path), "t5", timeout=1)
        assert not result.ok and "timed out after 1s" in (result.error or "")
    finally:
        hold_srv.close()


def test_a2a_remote_cancel_failure_swallowed_and_odd_events(tmp_path):
    srv = _JsonRpcServer(tmp_path, cancel_fails=True)
    srv.sse_body = (
        b'event: ping\ndata: {"task_id": "remote-1", "type": "ping", "data": {}}\n\n'  # unknown event type
        + b'event: state\ndata: {"task_id": "remote-1", "type": "state", "data": {}}\n\n'  # no state key
        + b'event: state\ndata: {"task_id": "remote-1", "type": "state", "data": {"state": "failed", "error": "boom"}}\n\n'
    )
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="x", kind="a2a_remote", command=srv.url))
        result = adapter.run("p", Path(tmp_path), "t6", timeout=10, should_cancel=lambda: True)
        assert not result.ok and "boom" in (result.error or "")  # cancel failed but stream reported the outcome
    finally:
        srv.close()


def test_api_mode_settings_base_url_and_odd_payloads(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        mode = "ok"  # ok | non_object_submit | status_error | non_object_status | bad_json_status

        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if Handler.mode == "non_object_submit":
                body = b'[1]'
            else:
                body = json.dumps({"id": "r-1"}).encode()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if Handler.mode == "status_error":
                self.send_response(500)
                self.end_headers()
                return
            if Handler.mode == "non_object_status":
                body = b'[1]'
            elif Handler.mode == "bad_json_status":
                body = b"not json at all"
            else:
                body = json.dumps({"state": "completed", "usage": "not-a-dict"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(out if False else body)

    def run_mode(mode: str, **kw):
        Handler.mode = mode
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            # http command selects api mode; the settings URL (trailing slash) wins over it
            adapter = GenericAdapter(AgentSpec(name="r", kind="generic", command="http://127.0.0.1:9"))
            return adapter.run("p", Path(tmp_path), "t", settings={"api_base_url": f"http://127.0.0.1:{server.server_address[1]}/"}, **kw)
        finally:
            server.shutdown()
            server.server_close()

    # settings api_base_url (with trailing slash) + completed with non-dict usage, no on_line
    result = run_mode("ok")
    assert result.ok and result.usage is None
    Handler.mode = "non_object_submit"
    result = run_mode("non_object_submit")
    assert not result.ok and "non-object payload on submit" in (result.error or "")
    result = run_mode("status_error")
    assert not result.ok and "status lookup failed" in (result.error or "")
    result = run_mode("non_object_status")
    assert not result.ok and "non-object payload on status" in (result.error or "")
    result = run_mode("bad_json_status")
    assert not result.ok and "status lookup failed" in (result.error or "")  # JSON parse fails inside the status call

    # outer polling handler: an exception escaping the per-call try (here from on_line)
    class Handler2(Handler):
        def do_GET(self):
            body = json.dumps({"state": "working", "output": "x\n"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler2)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        def boom(line):
            raise ValueError("observer died")

        adapter = GenericAdapter(AgentSpec(name="r", kind="generic", command=f"http://127.0.0.1:{server.server_address[1]}"))
        result = adapter.run("p", Path(tmp_path), "t", timeout=10, on_line=boom)
        assert not result.ok and "failed while polling status" in (result.error or "")
    finally:
        server.shutdown()
        server.server_close()


def test_api_mode_cancel_post_failure(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.endswith("/cancel"):
                self.send_response(500)
                self.end_headers()
                return
            body = json.dumps({"id": "r-1"}).encode()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            body = json.dumps({"state": "canceled", "error": "gone"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        adapter = GenericAdapter(AgentSpec(name="r", kind="generic", command=f"http://127.0.0.1:{server.server_address[1]}"))
        result = adapter.run("p", Path(tmp_path), "t", timeout=10, should_cancel=lambda: True)
        assert not result.ok and "gone" in (result.error or "")  # cancel failed silently; stream decided
    finally:
        server.shutdown()
        server.server_close()


def test_generic_api_preflight_success_and_missing_url(tmp_path):
    srv = _RestTaskServer([{"state": "working"}])
    try:
        preflight = _api_adapter(srv.url).preflight()
        assert preflight.ok and preflight.binary == srv.url  # 200 on "/" now
    finally:
        srv.close()
    # any HTTP answer (even 404) proves reachability
    no_route = _JsonRpcServer(tmp_path, card=None)
    try:
        preflight = _api_adapter(no_route.url).preflight()
        assert preflight.ok and preflight.binary == no_route.url
    finally:
        no_route.close()
    adapter = GenericAdapter(AgentSpec(name="r", kind="generic", command="http://127.0.0.1:9"))
    adapter.spec.command = None
    preflight = adapter.preflight()
    assert not preflight.ok and "base URL" in (preflight.error or "")


def test_a2a_remote_observer_exception_and_postloop_timeout(tmp_path):
    srv = _JsonRpcServer(tmp_path)
    srv.sse_body = b'event: output\ndata: {"task_id": "remote-1", "type": "output", "data": {"line": "x"}}\n\n'
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="x", kind="a2a_remote", command=srv.url))

        def boom(line):
            raise ValueError("observer died")

        result = adapter.run("p", Path(tmp_path), "t7", timeout=10, on_line=boom)
        assert not result.ok and "event stream failed" in (result.error or "")
    finally:
        srv.close()

    # Hold the stream open (no events, no close) so the only way out before
    # the tiny deadline is the timeout path itself — deterministic on any OS:
    # either the pre-get check fires or queue.get() times out, both report
    # "timed out after 0.001s". An immediately-closing stream would race the
    # deadline and sometimes win with "stream closed before a terminal state".
    quick = _JsonRpcServer(tmp_path, hold_sse=True)
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="x", kind="a2a_remote", command=quick.url))
        result = adapter.run("p", Path(tmp_path), "t8", timeout=0.001)
        assert not result.ok and "timed out after 0.001s" in (result.error or "")
    finally:
        quick.close()


def test_api_mode_output_without_observer(tmp_path):
    srv = _RestTaskServer([
        {"state": "working", "output": "silent line\n"},
        {"state": "completed", "output": "silent line\ndone\n"},
    ])
    try:
        result = _api_adapter(srv.url).run("do it", Path(tmp_path), "task-x", timeout=30)  # no on_line
        assert result.ok
    finally:
        srv.close()
