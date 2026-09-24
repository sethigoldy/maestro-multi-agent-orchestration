"""v2-M2: terminal dashboard — pure rendering plus event-driven loop behavior."""

from __future__ import annotations

import io
import json
import os
import re
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


def test_state_event_for_new_task_uses_fetched_record_or_falls_back():
    state = tui._State()
    state.fetch = lambda task_id: {"task_id": task_id, "title": "Fetched", "state": "submitted", "origin_agent": "human", "target_agent": "codex"}
    state.apply_event("task-a", "state", {"state": "working"})
    assert state.by_id["task-a"]["title"] == "Fetched"
    assert state.by_id["task-a"]["target_agent"] == "codex"
    assert state.by_id["task-a"]["state"] == "working"  # the event still applies

    def unreachable(task_id):
        raise ValueError("cannot reach A2A endpoint")

    state.fetch = unreachable
    state.apply_event("task-b", "state", {"state": "working"})
    assert state.by_id["task-b"] == {"task_id": "task-b", "state": "working"}


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


def _sse_server(tmp_path, tasks_payload: list[dict], frames: bytes, close_after_connect: bool = False, rst_after_connect: bool = False, events_status: int = 200, rpc_tasks: dict | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            # JSON-RPC tasks/get, answered from rpc_tasks (task id -> A2A task).
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            task = (rpc_tasks or {}).get(request["params"]["id"])
            if task is None:
                reply = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32001, "message": "Unknown task"}}
            else:
                reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"task": task}}
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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


def test_run_draws_the_task_list_before_any_event_or_key(tmp_path):
    # When no task is running, the daemon sends no events. The dashboard must
    # still show the tasks it loaded at start, not an empty screen that waits
    # for an event or a key press.
    server, url = _sse_server(
        tmp_path,
        [{"id": "task-1", "status": {"state": "completed"}, "metadata": {"title": "Seeded"}}],
        b"",
    )
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b"q", delay_s=0.3), stdout=out, is_tty=lambda: True)
        assert rc == 0
        text = out.getvalue()
        assert "Seeded" in text
        # The first frame comes after switching to the alternate screen, and
        # before leaving it.
        assert text.index("\x1b[?1049h") < text.index("Seeded") < text.index("\x1b[?1049l")
    finally:
        server.shutdown()
        server.server_close()


def test_run_shows_route_and_title_of_a_task_that_starts_after_it_opens(tmp_path):
    # The task list is loaded once, when the dashboard opens. A task delegated
    # later reaches it only through events, which carry no title or agents.
    # The dashboard must ask the daemon for that task's record, or its row
    # shows the task id and its route shows "? -> ?".
    late = {
        "id": "task-late",
        "status": {"state": "working"},
        "metadata": {"title": "Late task", "origin_agent": "human", "target_agent": "codex", "workspace": "/repo"},
    }
    server, url = _sse_server(tmp_path, [], _frame("task-late", "state", {"state": "working"}), rpc_tasks={"task-late": late})
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b"j" + b"q", delay_s=0.5), stdout=out, is_tty=lambda: True)
        text = _ANSI_RE.sub("", out.getvalue())
        assert rc == 0
        assert "Late task" in text
        assert "route: human → codex" in text
        assert "workspace: /repo" in text
    finally:
        server.shutdown()
        server.server_close()


def test_task_from_an_event_is_kept_when_its_record_cannot_be_fetched(tmp_path):
    # If the daemon does not know the task (or cannot be reached), the event
    # still adds a row, as before.
    server, url = _sse_server(tmp_path, [], _frame("task-gone", "state", {"state": "working"}), rpc_tasks={})
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b"j" + b"q", delay_s=0.5), stdout=out, is_tty=lambda: True)
        text = _ANSI_RE.sub("", out.getvalue())
        assert rc == 0
        assert "task-gone" in text and "working" in text
    finally:
        server.shutdown()
        server.server_close()


def test_run_ends_every_line_with_carriage_return(tmp_path):
    # The dashboard puts the terminal in raw mode, which turns off the
    # terminal's own newline translation. A bare "\n" then moves the cursor
    # down without returning it to column 0, so each row starts where the
    # previous one ended. Every line break the dashboard writes must be "\r\n".
    server, url = _sse_server(
        tmp_path,
        [
            {"id": "task-1", "status": {"state": "completed"}, "metadata": {"title": "First"}},
            {"id": "task-2", "status": {"state": "failed"}, "metadata": {"title": "Second"}},
        ],
        _frame("task-1", "output", {"line": "live-line"}),
    )
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b"j" + b"q", delay_s=0.3), stdout=out, is_tty=lambda: True)
        text = out.getvalue()
        assert rc == 0
        assert "Second" in text
        assert "\n" in text
        assert "\n" not in text.replace("\r\n", "")
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


def test_run_arrow_keys_move_the_selection_instead_of_quitting(tmp_path):
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
        # Down arrow, then up arrow, then q. Before the fix the first arrow's
        # ESC byte quit the dashboard, and q was never read.
        stdin = _PipeStdin(b"\x1b[B\x1b[Aq", delay_s=0.3)
        rc = tui.run(url, stdin=stdin, stdout=out, is_tty=lambda: True)
        assert rc == 0
        assert os.read(stdin.fileno(), 16) == b""  # every key, including q, was consumed
    finally:
        server.shutdown()
        server.server_close()


def test_read_key_escape_sequences():
    def read(data: bytes, close: bool = True) -> bytes:
        r, w = os.pipe()
        os.write(w, data)
        if close:
            os.close(w)
        try:
            return tui._KeyReader(r).read()
        finally:
            os.close(r)
            if not close:
                os.close(w)

    assert read(b"j") == b"j"
    assert read(b"\x1b[A") == tui._ARROW_UP
    assert read(b"\x1b[B") == tui._ARROW_DOWN
    assert read(b"\x1bOA") == tui._ARROW_UP  # application cursor mode
    assert read(b"\x1bOB") == tui._ARROW_DOWN
    assert read(b"\x1bOP") == b"\x1bOP"  # F1 in application mode: not an arrow, ignored by the loop
    assert read(b"\x1b[C") == b"\x1b[C"  # right arrow: not used, ignored by the loop
    assert read(b"\x1b", close=False) == b"\x1b"  # a lone Esc: nothing follows
    assert read(b"\x1b") == b"\x1b"  # Esc, then end of input
    assert read(b"\x1b[") == b"\x1b["  # ESC [ and then end of input


def _timed_reads(first: bytes, later: bytes, count: int) -> list[bytes]:
    """Write ``first`` now and ``later`` after a delay, then read ``count`` keys.

    The delay is much longer than the escape wait, so ``later`` arrives as a
    separate key press. The reads run in a thread so that a reader which
    blocks shows up as a test failure instead of a hang.
    """
    r, w = os.pipe()
    os.write(w, first)
    timer = threading.Timer(0.5, lambda: os.write(w, later))
    timer.start()
    keys: list[bytes] = []
    reader = tui._KeyReader(r)
    thread = threading.Thread(target=lambda: keys.extend(reader.read() for _ in range(count)), daemon=True)
    thread.start()
    thread.join(5)
    timer.join()
    os.close(w)
    os.close(r)
    return keys


def test_read_key_incomplete_escape_bracket_does_not_block():
    """ESC [ with no third byte is returned at once, and the next key is read on its own.

    Before the fix the third byte was read without a timeout, so the next q
    was read as ESC [ q and ignored instead of quitting.
    """
    assert _timed_reads(b"\x1b[", b"q", 2) == [b"\x1b[", b"q"]


def test_read_key_incomplete_escape_o_does_not_block():
    assert _timed_reads(b"\x1bO", b"q", 2) == [b"\x1bO", b"q"]


def test_read_key_esc_followed_by_another_key_is_a_lone_esc():
    """Esc Esc gives two Esc keys, and Esc x gives Esc and then x.

    Before the fix ESC and the next byte came back together as one unknown
    key, so Esc did not quit and the following key was lost.
    """
    r, w = os.pipe()
    os.write(w, b"\x1b\x1b\x1bxj")
    os.close(w)
    reader = tui._KeyReader(r)
    try:
        assert [reader.read() for _ in range(5)] == [b"\x1b", b"\x1b", b"\x1b", b"x", b"j"]
        assert not reader.pending
        assert reader.read() == b""
    finally:
        os.close(r)


def test_key_reader_reports_a_kept_byte_as_pending():
    r, w = os.pipe()
    os.write(w, b"\x1bx")
    os.close(w)
    reader = tui._KeyReader(r)
    try:
        assert not reader.pending
        assert reader.read() == b"\x1b"
        assert reader.pending
        assert reader.read() == b"x"
        assert not reader.pending
    finally:
        os.close(r)


def test_run_reads_a_kept_key_without_waiting_for_more_input(tmp_path, monkeypatch):
    """A byte kept back by the key reader is handled even though stdin has nothing more to read.

    The dashboard quits on Esc, so a real kept byte never reaches the loop.
    This test uses a reader that keeps q back after j instead. The loop must
    take q from the reader rather than wait on stdin, where nothing else
    will arrive, because the write end of the pipe stays open.
    """
    server, url = _sse_server(
        tmp_path,
        [{"id": "task-1", "status": {"state": "working"}, "metadata": {"title": "One"}}],
        b"",
    )
    read_fd, write_fd = os.pipe()

    class _Stdin:
        def fileno(self) -> int:
            return read_fd

    class _KeepQ(tui._KeyReader):
        def read(self) -> bytes:
            key = super().read()
            if key == b"j":
                self._kept = b"q"
            return key

    monkeypatch.setattr(tui, "_KeyReader", _KeepQ)
    result: dict[str, int] = {}
    try:
        thread = threading.Thread(target=lambda: result.update(rc=tui.run(url, stdin=_Stdin(), stdout=io.StringIO(), is_tty=lambda: True)), daemon=True)
        thread.start()
        time.sleep(0.3)
        os.write(write_fd, b"j")
        thread.join(5)
        assert result.get("rc") == 0
    finally:
        os.close(write_fd)
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
    from maestro import daemonctl

    home = tmp_path / "home"
    home.mkdir()
    # A live pid alone no longer confirms a daemon: this process holds the
    # owner lock, as a running daemon does, and the marker says so.
    lock_fd = daemonctl.acquire_owner_lock(home)
    try:
        (home / "daemon.json").write_text(
            json.dumps({"pid": os.getpid(), "port": 8790, "host": "127.0.0.1", "token": "mk-token", "owner_lock": True}), encoding="utf-8"
        )
        monkeypatch.setenv("MAESTRO_HOME", str(home))
        monkeypatch.delenv("MAESTRO_DAEMON_URL", raising=False)
        # Nothing listens on the marker's port; this test is about where the
        # token comes from, so the "does it answer HTTP" probe is stubbed.
        monkeypatch.setattr(daemonctl, "probe", lambda url, *a, **k: True)
        assert tui_mod.main([]) == 0
        assert calls == {"url": "http://127.0.0.1:8790", "token": "mk-token"}
    finally:
        daemonctl.release_owner_lock(lock_fd)


# ---------------------------------------------------------------- terminal width

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain_lines(frame: str) -> list[str]:
    return [_ANSI_RE.sub("", ln) for ln in frame.split("\n")]


def test_terminal_width_prefers_columns_env(monkeypatch):
    monkeypatch.setenv("COLUMNS", "64")
    assert tui._terminal_width(io.StringIO()) == 64


@pytest.mark.parametrize("bad", ["not-a-number", "5", "9999"])
def test_terminal_width_ignores_bad_columns_env(monkeypatch, bad):
    monkeypatch.setenv("COLUMNS", bad)
    assert tui._terminal_width(io.StringIO()) == 80  # falls through to the default


def test_terminal_width_from_stdout_fd(monkeypatch):
    monkeypatch.delenv("COLUMNS", raising=False)  # force the fd path

    class _Fd:
        def fileno(self):
            return 7

    monkeypatch.setattr(tui.os, "get_terminal_size", lambda fd: os.terminal_size((72, 30)))
    assert tui._terminal_width(_Fd()) == 72
    # Out-of-range terminal sizes fall through to the default.
    monkeypatch.setattr(tui.os, "get_terminal_size", lambda fd: os.terminal_size((10, 30)))
    assert tui._terminal_width(_Fd()) == 80


def test_terminal_width_fallback_without_usable_fd(monkeypatch):
    monkeypatch.delenv("COLUMNS", raising=False)
    assert tui._terminal_width(io.StringIO()) == 80  # StringIO.fileno() raises OSError

    class _NoFileno:
        pass

    assert tui._terminal_width(_NoFileno()) == 80  # AttributeError path
    rfd, wfd = os.pipe()
    try:

        class _PipeFd:
            def fileno(self):
                return rfd

        assert tui._terminal_width(_PipeFd()) == 80  # a pipe is not a terminal
    finally:
        os.close(rfd)
        os.close(wfd)


def test_render_frame_clamps_tiny_width():
    frame = tui.render_frame([{"task_id": "task-1", "title": "T", "state": "working"}], 0, live=True, width=5)
    assert "task-1" in _plain_lines(frame)[2]  # clamped to 40: still a sane frame


def test_render_frame_detail_values_truncated_to_width():
    task = {
        "task_id": "task-9", "title": "T" * 60, "state": "failed",
        "workspace": "/very/long/" + "x" * 120,
        "error": "E" * 200,
    }
    frame = tui.render_frame([task], 0, live=False, width=60)
    lines = _plain_lines(frame)
    assert all(len(ln) <= 60 for ln in lines)
    text = "".join(lines)
    assert not re.search(r"T{49,}", text)  # the 60-char title is truncated everywhere
    assert "T" * 14 in text  # list-row title column at width 60


def test_render_frame_summary_truncated_on_narrow_width():
    states = ["submitted", "working", "input-required", "completed", "failed", "canceled", "unknown"]
    tasks = [{"task_id": f"t{i}", "title": f"T{i}", "state": s} for i, s in enumerate(states)]
    frame = tui.render_frame(tasks, None, live=True, width=60)
    assert all(len(ln) <= 60 for ln in _plain_lines(frame))


def test_run_uses_detected_terminal_width(tmp_path, monkeypatch):
    frames = _frame("task-1", "state", {"state": "working"})
    server, url = _sse_server(
        tmp_path,
        [{"id": "task-1", "status": {"state": "submitted"}, "metadata": {"title": "X" * 40}}],
        frames,
    )
    monkeypatch.setenv("COLUMNS", "60")
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b"q", delay_s=0.5), stdout=out, is_tty=lambda: True)
        assert rc == 0
        text = out.getvalue()
        # The list row truncates the title to width - 46 = 14 characters. The
        # detail pane below may show all 40, because it allows width - 12.
        rows = [ln for ln in _plain_lines(text) if "task-1" in ln]
        assert rows and all("X" * 14 in ln and "X" * 15 not in ln for ln in rows)
        nonempty = [ln for ln in _plain_lines(text) if ln.strip()]
        assert all(len(ln) <= 60 for ln in nonempty)
    finally:
        server.shutdown()
        server.server_close()


def test_winch_handler_updates_width_and_wakes(monkeypatch):
    import signal as _signal

    holder = {"width": 100}
    rfd, wfd = os.pipe()
    try:
        previous = _signal.getsignal(_signal.SIGWINCH)
        restore = tui._install_winch_handler(io.StringIO(), holder, wfd)
        assert restore is not None and callable(restore)
        monkeypatch.setenv("COLUMNS", "64")
        os.kill(os.getpid(), _signal.SIGWINCH)  # CPython runs the handler before the next bytecode
        assert holder["width"] == 64
        assert os.read(rfd, 1) == b"w"
        # A closed wake fd must not kill the handler.
        os.close(wfd)
        os.kill(os.getpid(), _signal.SIGWINCH)
        assert holder["width"] == 64
        restore()
        assert _signal.getsignal(_signal.SIGWINCH) is previous
    finally:
        try:
            os.close(rfd)
        except OSError:
            pass


def test_install_winch_handler_noop_off_main_thread():
    box: dict = {}

    def worker():
        rfd, wfd = os.pipe()
        try:
            restore = tui._install_winch_handler(io.StringIO(), {"width": 80}, wfd)
            box["restore"] = restore
            box["result"] = restore()
        except BaseException as exc:  # surface worker failures to the main thread
            box["error"] = exc
        finally:
            os.close(rfd)
            os.close(wfd)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive() and "error" not in box
    assert callable(box["restore"]) and box["result"] is None  # no-op restore, nothing installed


def test_run_restores_winch_handler(tmp_path):
    import signal as _signal

    server, url = _sse_server(tmp_path, [], b"", close_after_connect=True)
    previous = _signal.getsignal(_signal.SIGWINCH)
    try:
        rc = tui.run(url, stdin=_PipeStdin(b""), stdout=io.StringIO(), is_tty=lambda: True)
        assert rc == 1
        assert _signal.getsignal(_signal.SIGWINCH) is previous
    finally:
        server.shutdown()
        server.server_close()


def test_detail_shows_run_dir_only_when_it_differs_from_workspace():
    base = {"task_id": "t", "title": "T", "state": "working", "workspace": "/repo"}
    same = _ANSI_RE.sub("", tui.render_frame([dict(base, run_dir="/repo")], 0, live=True))
    other = _ANSI_RE.sub("", tui.render_frame([dict(base, run_dir="/h/worktrees/t")], 0, live=True))
    assert "run dir:" not in same
    assert "run dir: /h/worktrees/t" in other
    assert tui.normalize({"id": "t", "metadata": {"run_dir": "/x"}})["run_dir"] == "/x"
