"""v2-M2: terminal dashboard — pure rendering plus event-driven loop behavior."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from maestro import tui


# ---------------------------------------------------------------- pure parts

def test_normalize_maps_a2a_shape():
    record = {
        "kind": "task",
        "id": "task-1",
        "status": {"state": "completed"},
        "metadata": {"title": "T", "usage": {"cost_usd": 0.5}, "attempts": [{"agent": "codex"}]},
    }
    flat = tui.normalize(record)
    assert flat["task_id"] == "task-1" and flat["state"] == "completed"
    assert flat["title"] == "T" and flat["usage"]["cost_usd"] == 0.5
    bare = tui.normalize({"id": "x"})
    assert bare["state"] == "unknown" and bare["attempts"] == []


def test_render_frame_empty():
    frame = tui.render_frame([], None, live=True)
    assert "\x1b[2J\x1b[H" in frame  # full-screen clear prefix
    assert "no tasks yet" in frame.replace("\x1b[2m", "").replace("\x1b[0m", "")


def test_render_frame_tasks_selection_cost():
    tasks = [
        {"task_id": "task-0001", "title": "First task", "state": "working", "target_agent": "codex",
         "usage": {"cost_usd": 0.25}, "transcript": ["a", "b"]},
        {"task_id": "task-0002", "title": "Second task", "state": "completed", "target_agent": "hermes"},
    ]
    frame = tui.render_frame(tasks, 1, live=True)
    plain = frame.replace("\x1b[0m", "").replace("\x1b[2m", "")
    assert "First task" in plain and "Second task" in plain
    assert "working" in plain and "completed" in plain
    assert "$0.250" in plain
    # selection marker sits on the second row
    rows = [r for r in frame.split("\n") if "task-000" in r]
    assert "\x1b[1m▸\x1b[0m" in rows[1] and "\x1b[1m▸\x1b[0m" not in rows[0]


def test_render_frame_detail_error_attempts_and_tail():
    task = {
        "task_id": "task-9", "title": "Broken", "state": "failed",
        "origin_agent": "human", "target_agent": "codex",
        "workspace": "/w", "branch": "maestro/task-9",
        "error": "boom\nsecond line",
        "attempts": [{"agent": "codex", "error": "exit 1"}, {"agent": "hermes", "error": None}],
        "transcript": [f"line{i}" for i in range(30)],
    }
    plain = tui.render_frame([task], 0, live=False).replace("\x1b[0m", "").replace("\x1b[2m", "")
    assert "human → codex" in plain
    assert "boom" in plain and "second line" not in plain.split("boom")[1][:40]  # first line only
    assert "line29" in plain and "line5" not in plain  # transcript tail cap


def test_render_frame_more_than_max_shown():
    tasks = [{"task_id": f"task-{i}", "title": f"T{i}", "state": "completed"} for i in range(60)]
    plain = tui.render_frame(tasks, None, live=True).replace("\x1b[0m", "").replace("\x1b[2m", "")
    assert "… and 10 more" in plain


def test_state_apply_event_lifecycle():
    state = tui._State()
    state.apply_event("task-1", "state", {"state": "working"})
    state.apply_event("task-1", "output", {"line": "hello"})
    state.apply_event("task-1", "usage", {"cost_usd": 0.1})
    state.apply_event("task-1", "usage", {"tokens": 5})
    task = state.by_id["task-1"]
    assert task["state"] == "working"
    assert task["transcript"] == ["hello"]
    assert task["usage"] == {"cost_usd": 0.1, "tokens": 5}
    # newest-first ordering for a second task
    state.apply_event("task-2", "state", {"state": "submitted"})
    assert [t["task_id"] for t in state.tasks] == ["task-2", "task-1"]


def test_state_output_cap_and_clamp():
    state = tui._State()
    for i in range(2500):
        state.apply_event("task-1", "output", {"line": f"l{i}"})
    assert len(state.by_id["task-1"]["transcript"]) == 2000
    state.selected = 99
    state.clamp_selection()
    assert state.selected == 0


# ---------------------------------------------------------------- the loop

class _PipeStdin:
    """A stdin stand-in backed by a real pipe fd, fed by a script thread."""

    def __init__(self, script: bytes, delay_s: float = 0.0) -> None:
        self._read_fd, self._write_fd = os.pipe()
        if delay_s > 0:
            timer = threading.Timer(delay_s, lambda: (os.write(self._write_fd, script), os.close(self._write_fd)))
            timer.daemon = True
            timer.start()
        else:
            os.write(self._write_fd, script)
            os.close(self._write_fd)

    def fileno(self) -> int:
        return self._read_fd


def _sse_server(tmp_path, tasks_payload: list[dict], frames: bytes, close_after_connect: bool = False, rst_after_connect: bool = False, events_status: int = 200):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/tasks":
                body = json.dumps({"tasks": tasks_payload}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                if events_status != 200:
                    self.send_response(events_status)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if rst_after_connect:
                    import socket as _socket
                    import struct as _struct

                    self.connection.setsockopt(
                        _socket.SOL_SOCKET, _socket.SO_LINGER, _struct.pack("ii", 1, 0)
                    )
                    self.connection.close()  # linger-0 close sends RST
                    return
                if close_after_connect:
                    return  # handler returns -> connection closes (daemon stopped)
                try:
                    self.wfile.write(frames)
                    self.wfile.flush()
                    # A real SSE endpoint holds the connection open.
                    while True:
                        time.sleep(0.2)
                except OSError:
                    pass  # client disconnected
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _frame(task_id: str, type_: str, data: dict) -> bytes:
    envelope = {"task_id": task_id, "type": type_, "data": data}
    return f"event: {type_}\ndata: {json.dumps(envelope)}\n\n".encode()


def test_run_renders_events_and_quits(tmp_path):
    frames = (
        b": keepalive\n\n"  # comment lines are ignored
        + _frame("task-1", "output", {"line": "live-line"})
        + b"event: output\ndata: {not json}\n\n"  # malformed payload is skipped
        + _frame("task-1", "state", {"state": "completed"})
    )
    server, url = _sse_server(
        tmp_path,
        [{"id": "task-1", "status": {"state": "submitted"}, "metadata": {"title": "Seeded"}}],
        frames,
    )
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b"j" + b"q", delay_s=0.3), stdout=out, is_tty=lambda: True)
        text = out.getvalue().replace("\x1b[0m", "").replace("\x1b[2m", "")
        assert rc == 0
        assert "Seeded" in text and "live-line" in text and "completed" in text
    finally:
        server.shutdown()
        server.server_close()


def test_run_exits_when_stream_closes(tmp_path):
    server, url = _sse_server(tmp_path, [], b"", close_after_connect=True)
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b""), stdout=out, is_tty=lambda: True)
        assert rc == 1  # daemon stopped mid-session
    finally:
        server.shutdown()
        server.server_close()


def test_run_rejects_non_tty(tmp_path):
    import contextlib

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = tui.run("http://127.0.0.1:1", is_tty=lambda: False)
    assert rc == 2 and "interactive terminal" in err.getvalue()


def test_run_unreachable_daemon(tmp_path):
    import contextlib

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = tui.run("http://127.0.0.1:1", is_tty=lambda: True)
    assert rc == 1 and "cannot reach" in err.getvalue()


def test_cli_dashboard_command(tmp_path, monkeypatch):
    from maestro import cli as clic

    server, url = _sse_server(tmp_path, [], b"")
    try:
        monkeypatch.setenv("MAESTRO_DAEMON_URL", url)
        rc = clic.main(["dashboard"])  # not a tty under pytest
        assert rc == 2
    finally:
        server.shutdown()
        server.server_close()


def test_cli_dashboard_without_daemon(tmp_path, monkeypatch):
    from maestro import cli as clic

    monkeypatch.delenv("MAESTRO_DAEMON_URL", raising=False)
    marker = tmp_path / "home" / "daemon.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"pid": 999999, "port": 1}), encoding="utf-8")
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    rc = clic.main(["dashboard"])
    assert rc == 1


def test_run_exits_on_connection_reset(tmp_path):
    server, url = _sse_server(tmp_path, [], b"", rst_after_connect=True)
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b""), stdout=out, is_tty=lambda: True)
        assert rc == 1  # RST from the server counts as stream end
    finally:
        server.shutdown()
        server.server_close()


def test_run_exits_on_ctrl_c(tmp_path, monkeypatch):
    import select as _select

    server, url = _sse_server(tmp_path, [], b"")
    real_select = _select.select
    state = {"raised": False}

    def fake_select(r, w, x):
        if not state["raised"]:
            state["raised"] = True
            raise KeyboardInterrupt()  # what the default SIGINT handler does
        return real_select(r, w, x)

    monkeypatch.setattr(tui.select, "select", fake_select)
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b""), stdout=out, is_tty=lambda: True)
        assert rc == 0
    finally:
        server.shutdown()
        server.server_close()


def test_run_exits_on_stdin_eof(tmp_path):
    server, url = _sse_server(tmp_path, [], b"")
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b""), stdout=out, is_tty=lambda: True)
        assert rc == 1  # terminal closed while the stream was still alive
    finally:
        server.shutdown()
        server.server_close()


def test_run_uses_raw_mode_on_real_tty(tmp_path):
    import termios as _termios

    server, url = _sse_server(tmp_path, [], b"")
    master, slave = os.openpty()
    try:
        class Stdin:
            def fileno(self):
                return slave

        out = io.StringIO()
        threading.Timer(0.3, lambda: (os.write(master, b"q"), )).start()
        rc = tui.run(url, stdin=Stdin(), stdout=out, is_tty=lambda: True)
        assert rc == 0
    finally:
        os.close(master)
        os.close(slave)
        server.shutdown()
        server.server_close()


def test_run_survives_termios_failure(tmp_path, monkeypatch):
    import termios as _termios

    server, url = _sse_server(tmp_path, [], b"")
    master, slave = os.openpty()
    try:
        class Stdin:
            def fileno(self):
                return slave

        def boom(fd):
            raise _termios.error("no termios for you")

        monkeypatch.setattr(tui.termios, "tcgetattr", boom)
        out = io.StringIO()
        # No raw mode -> the pty stays canonical (line-buffered), so the key
        # needs a newline to be delivered.
        threading.Timer(0.3, lambda: os.write(master, b"q\n")).start()
        rc = tui.run(url, stdin=Stdin(), stdout=out, is_tty=lambda: True)
        assert rc == 0  # raw mode skipped, still runs and quits cleanly
    finally:
        os.close(master)
        os.close(slave)
        server.shutdown()
        server.server_close()


# ------------------------------------------------- branch-completeness tests

def test_state_apply_event_edge_cases():
    state = tui._State()
    state.apply_event("", "state", {"state": "working"})  # empty id ignored
    assert state.tasks == []
    state.apply_event("task-1", "state", {"state": "failed", "error": "boom"})
    task = state.by_id["task-1"]
    assert task["state"] == "failed" and task["error"] == "boom"
    state.apply_event("task-1", "output", {"line": 42})  # non-str line ignored
    assert task.get("transcript") in (None, [])
    state.apply_event("task-1", "ping", {})  # unknown event type is a no-op
    assert task["state"] == "failed"


def test_read_sse_frames_done_before_start():
    import threading as _threading

    state = tui._State()
    done = _threading.Event()
    done.set()  # main loop already exited; the reader must not touch the stream

    class NoRead:
        def read1(self, n):
            raise AssertionError("must not read")

    r, w = os.pipe()
    try:
        tui._read_sse_frames(NoRead(), state, w, done)
        assert os.read(r, 8) == b"q"  # stream-end marker still signaled
    finally:
        os.close(r)
        os.close(w)


def test_read_sse_frames_pipe_closed():
    import threading as _threading

    state = tui._State()
    done = _threading.Event()
    r, w = os.pipe()
    os.close(w)  # main loop is tearing down while an event is in flight

    class OneFrame:
        def read1(self, n):
            return (
                b'event: output\n'
                b'data: {"task_id": "t", "type": "output", "data": {"line": "x"}}\n'
                b"\n"
            )

    tui._read_sse_frames(OneFrame(), state, w, done)  # write raises -> return
    assert state.by_id["t"]["transcript"] == ["x"]  # event applied before the signal
    os.close(r)


def test_run_events_endpoint_refused(tmp_path, monkeypatch):
    import contextlib

    server, url = _sse_server(tmp_path, [], b"")
    real_conn = tui.http.client.HTTPConnection

    class BoomConn(real_conn):
        def request(self, method, url_, *a, **k):
            self._url_ = url_
            super().request(method, url_, *a, **k)

        def getresponse(self):
            if getattr(self, "_url_", "").endswith("/events"):
                raise ConnectionResetError("connection reset")
            return super().getresponse()

    monkeypatch.setattr(tui.http.client, "HTTPConnection", BoomConn)
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = tui.run(url, stdin=_PipeStdin(b""), stdout=io.StringIO(), is_tty=lambda: True)
        assert rc == 1 and "cannot reach" in err.getvalue()
    finally:
        server.shutdown()
        server.server_close()


def test_run_events_endpoint_error_status(tmp_path):
    import contextlib

    server, url = _sse_server(tmp_path, [], b"", events_status=503)
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = tui.run(url, stdin=_PipeStdin(b""), stdout=io.StringIO(), is_tty=lambda: True)
        assert rc == 1 and "answered 503" in err.getvalue()
    finally:
        server.shutdown()
        server.server_close()


def test_run_key_navigation(tmp_path):
    server, url = _sse_server(
        tmp_path,
        [
            {"id": "task-2", "status": {"state": "completed"}, "metadata": {"title": "Two"}},
            {"id": "task-1", "status": {"state": "working"}, "metadata": {"title": "One"}},
        ],
        b"",
    )
    try:
        out = io.StringIO()
        # k and \x7f move up (clamped at 0), j moves down, x is ignored, q quits
        rc = tui.run(url, stdin=_PipeStdin(b"k\x7fjxq", delay_s=0.3), stdout=out, is_tty=lambda: True)
        assert rc == 0
    finally:
        server.shutdown()
        server.server_close()


def test_run_stream_close_with_stdin_open(tmp_path):
    server, url = _sse_server(tmp_path, [], b"", close_after_connect=True)
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b"", delay_s=30), stdout=out, is_tty=lambda: True)
        assert rc == 1  # exits via the stream-end path, not stdin EOF
    finally:
        server.shutdown()
        server.server_close()


def test_run_termios_restore_failure(tmp_path, monkeypatch):
    import termios as _termios

    server, url = _sse_server(tmp_path, [], b"")
    master, slave = os.openpty()
    try:
        class Stdin:
            def fileno(self):
                return slave

        def boom(fd, how, attrs):
            raise _termios.error("restore failed")

        monkeypatch.setattr(tui.termios, "tcsetattr", boom)
        out = io.StringIO()
        threading.Timer(0.3, lambda: os.write(master, b"q")).start()
        rc = tui.run(url, stdin=Stdin(), stdout=out, is_tty=lambda: True)
        assert rc == 0  # restore failure is swallowed; quit still works
    finally:
        os.close(master)
        os.close(slave)
        server.shutdown()
        server.server_close()


def test_run_fd_close_failures(tmp_path, monkeypatch):
    import contextlib

    server, url = _sse_server(tmp_path, [], b"")
    real_os = tui.os

    class FlakyOs:
        def close(self, fd):
            raise OSError("close denied")

        def __getattr__(self, name):
            return getattr(real_os, name)

    monkeypatch.setattr(tui, "os", FlakyOs())
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = tui.run(url, stdin=_PipeStdin(b"q", delay_s=0.3), stdout=io.StringIO(), is_tty=lambda: True)
        assert rc == 0
    finally:
        server.shutdown()
        server.server_close()


def test_tui_main_dispatch(tmp_path, monkeypatch):
    import contextlib

    from maestro import tui as t

    server, url = _sse_server(tmp_path, [], b"")
    try:
        monkeypatch.setenv("MAESTRO_DAEMON_URL", url)
        assert t.main([]) == 2  # resolves the URL, then run() rejects non-tty
    finally:
        server.shutdown()
        server.server_close()

    marker = tmp_path / "home" / "daemon.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"pid": 999999, "port": 1}), encoding="utf-8")
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MAESTRO_DAEMON_URL", raising=False)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert t.main([]) == 1  # stale marker -> ValueError from _daemon_url


# ------------------------------------------------------------ bearer-token support

def _authed_sse_server(tmp_path, token: str):
    """Fake daemon that 401s /tasks and /events without the right Bearer token."""
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self) -> bool:
            seen.append(self.headers.get("Authorization"))
            return self.headers.get("Authorization") == f"Bearer {token}"

        def do_GET(self):
            if not self._ok():
                body = json.dumps({"error": "unauthorized"}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/tasks":
                body = json.dumps({"tasks": [{"id": "task-1", "status": {"state": "submitted"}, "metadata": {"title": "T"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    while True:
                        time.sleep(0.2)
                except OSError:
                    pass
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", seen


def test_load_tasks_sends_bearer_token(tmp_path):
    server, url, seen = _authed_sse_server(tmp_path, "sekrit")
    try:
        with pytest.raises(Exception):  # no token -> 401
            tui.load_tasks(url)
        tasks = tui.load_tasks(url, token="sekrit")
        assert tasks[0]["task_id"] == "task-1"
        assert seen[-1] == "Bearer sekrit"
    finally:
        server.shutdown()
        server.server_close()


def test_run_with_token_streams_events(tmp_path):
    server, url, seen = _authed_sse_server(tmp_path, "sekrit")
    try:
        out = io.StringIO()
        rc = tui.run(url, token="sekrit", stdin=_PipeStdin(b"j" + b"q", delay_s=0.3), stdout=out, is_tty=lambda: True)
        assert rc == 0 and "T" in out.getvalue()
        assert any(h == "Bearer sekrit" for h in seen)
    finally:
        server.shutdown()
        server.server_close()


def test_run_without_token_against_authed_daemon_fails(tmp_path):
    server, url, _ = _authed_sse_server(tmp_path, "sekrit")
    try:
        import contextlib

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = tui.run(url, stdin=_PipeStdin(b""), stdout=io.StringIO(), is_tty=lambda: True)
        assert rc == 1 and "cannot reach" in err.getvalue()
    finally:
        server.shutdown()
        server.server_close()


def test_cli_main_uses_marker_token(tmp_path, monkeypatch):
    # tui.main resolves the token from the daemon marker (or env) and passes it on
    import maestro.tui as tui_mod

    calls: dict = {}

    def fake_run(url, *, token=None, **kw):
        calls["url"], calls["token"] = url, token
        return 0

    monkeypatch.setattr(tui_mod, "run", fake_run)
    home = tmp_path / "home"
    home.mkdir()
    (home / "daemon.json").write_text(
        json.dumps({"pid": os.getpid(), "port": 8790, "host": "127.0.0.1", "token": "mk-token"}), encoding="utf-8"
    )
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.delenv("MAESTRO_DAEMON_URL", raising=False)
    assert tui_mod.main([]) == 0
    assert calls == {"url": "http://127.0.0.1:8790", "token": "mk-token"}
