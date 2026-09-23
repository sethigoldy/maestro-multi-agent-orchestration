"""Regression tests for bugs in the CLI, the terminal dashboard and ``maestro doctor``.

Each test here reproduces one reported bug. No test talks to a real daemon on
the machine: ``MAESTRO_HOME`` always points into ``tmp_path``, and the daemon
URL points either at a test daemon started on 127.0.0.1 or at a port nobody
listens on.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from maestro import cli, tui
from maestro.daemon import MaestroDaemon
from maestro.doctor import format_doctor, run_doctor
from maestro.handoff import HandoffDoc


# ---------------------------------------------------------------- helpers

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


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


def _fake_codex(bin_dir: Path) -> None:
    """Write a fake ``codex`` that only does work on a real task run.

    Probes such as ``codex --version`` and ``codex exec --help`` exit at once.
    A real run always ends with ``-`` (the prompt arrives on stdin), so only
    that invocation prints a line. The script writes nothing to disk.
    """
    path = bin_dir / "codex"
    path.write_text(
        "#!/bin/sh\n"
        "for last; do :; done\n"
        '[ "$last" = "-" ] || exit 0\n'
        "cat > /dev/null\n"
        "echo tail-test-line\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _doc() -> HandoffDoc:
    return HandoffDoc(
        title="Tail me", request="Implement it", verification="none", commit_policy="no-commit",
        target_agent="codex", explicit_target=True,
    )


@pytest.fixture
def live_daemon(tmp_path, monkeypatch):
    """A test daemon with a real HTTP server on an ephemeral loopback port."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    daemon = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    monkeypatch.setenv("MAESTRO_DAEMON_URL", f"http://127.0.0.1:{daemon.port}")
    yield daemon
    daemon.stop()


@pytest.fixture
def finished_task(live_daemon, tmp_path, monkeypatch):
    """Run one task to completion on the test daemon and return its id."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_codex(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    ws = _git_repo(tmp_path)
    started = live_daemon.delegate(_doc(), ws)
    live_daemon.wait(started["task_id"], timeout=60)
    return started["task_id"]


def _run_main_with_timeout(argv: list[str], timeout: float = 20.0) -> int:
    """Run ``cli.main`` in a thread so a hanging command fails the test instead of the run."""
    result: dict[str, object] = {}

    def target() -> None:
        try:
            result["rc"] = cli.main(argv)
        except SystemExit as exc:  # argparse errors exit instead of returning
            result["rc"] = exc.code
        except BaseException as exc:  # pragma: no cover - only reached when a test is about to fail
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"maestro {' '.join(argv)} did not finish within {timeout}s"
    assert "error" not in result, result.get("error")
    return int(result["rc"])  # type: ignore[arg-type]


class _PipeStdin:
    """A stdin stand-in backed by a real pipe; the script is written after a delay."""

    def __init__(self, script: bytes, delay_s: float) -> None:
        self._read_fd, self._write_fd = os.pipe()
        timer = threading.Timer(delay_s, lambda: (os.write(self._write_fd, script), os.close(self._write_fd)))
        timer.daemon = True
        timer.start()

    def fileno(self) -> int:
        return self._read_fd


def _sse_server(tasks_payload: list[dict], frames: bytes):
    """A stub daemon that serves ``GET /tasks`` and a ``GET /events`` stream."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/tasks":
                body = json.dumps({"tasks": tasks_payload}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(frames)
                self.wfile.flush()
                while True:  # a real SSE endpoint holds the connection open
                    time.sleep(0.2)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _frame(task_id: str, type_: str, data: dict) -> bytes:
    envelope = {"task_id": task_id, "type": type_, "data": data}
    return f"event: {type_}\ndata: {json.dumps(envelope)}\n\n".encode()


def _last_frame_lines(output: str) -> list[str]:
    last = output.split("\x1b[2J\x1b[H")[-1]
    return [_ANSI.sub("", line) for line in last.splitlines()]


# ------------------------------------------- bug 1: dashboard duplicates rows

def test_dashboard_updates_loaded_task_row_in_place():
    """An event for a task loaded from GET /tasks updates that row instead of adding a new one."""
    frames = _frame("task-1", "output", {"line": "live-line"}) + _frame("task-1", "state", {"state": "completed"})
    server, url = _sse_server(
        [{"id": "task-1", "status": {"state": "working"}, "metadata": {"title": "Seeded title"}}],
        frames,
    )
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b"q", delay_s=0.5), stdout=out, is_tty=lambda: True)
    finally:
        server.shutdown()
        server.server_close()
    assert rc == 0
    rows = [line for line in _last_frame_lines(out.getvalue()) if "task-1" in line]
    assert len(rows) == 1, rows  # one row per task, no untitled duplicate
    assert "Seeded title" in rows[0] and "completed" in rows[0]


def test_dashboard_state_indexes_replaced_tasks():
    """Replacing the task table also replaces the id index, so events find the loaded rows."""
    state = tui._State()
    state.apply_event("task-old", "state", {"state": "working"})
    loaded = [{"task_id": "task-1", "title": "One", "state": "working"}, {"task_id": None, "title": "No id"}]
    state.set_tasks(loaded)
    assert state.tasks is loaded
    assert state.by_id == {"task-1": loaded[0]}  # the old entry and the id-less record are not indexed
    state.apply_event("task-1", "state", {"state": "failed", "error": "boom"})
    assert len(state.tasks) == 2
    assert loaded[0]["state"] == "failed" and loaded[0]["error"] == "boom"


# ---------------------------------------- bug 2: dashboard leaves cursor hidden

def test_dashboard_shows_cursor_again_on_exit():
    """On exit the dashboard shows the cursor (ESC[?25h) before it leaves the alternate screen."""
    server, url = _sse_server([], b"")
    try:
        out = io.StringIO()
        rc = tui.run(url, stdin=_PipeStdin(b"q", delay_s=0.2), stdout=out, is_tty=lambda: True)
    finally:
        server.shutdown()
        server.server_close()
    assert rc == 0
    text = out.getvalue()
    assert text.startswith("\x1b[?1049h\x1b[?25l")  # enter: alternate screen, hide cursor
    assert text.endswith("\x1b[?25h\x1b[?1049l")  # exit: show cursor, leave alternate screen


# ------------------------------------------------- bug 3: task tail resolution

def test_task_tail_resolves_task_number(finished_task, capsys):
    """``maestro task tail 1`` follows the task numbered 1 instead of waiting forever."""
    rc = _run_main_with_timeout(["task", "tail", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tail-test-line" in out and "[state] completed" in out


def test_task_tail_accepts_full_task_id(finished_task, capsys):
    rc = _run_main_with_timeout(["task", "tail", finished_task])
    assert rc == 0
    assert "[state] completed" in capsys.readouterr().out


def test_task_tail_unknown_number_exits_2(live_daemon, capsys):
    rc = _run_main_with_timeout(["task", "tail", "99"])
    assert rc == 2
    assert "Unknown task number 99" in capsys.readouterr().err


def test_task_tail_unknown_task_id_exits_2(live_daemon, capsys):
    """A well-formed task id that the daemon has never seen is refused, not followed forever."""
    rc = _run_main_with_timeout(["task", "tail", "task-20260101-000000-abcdef"])
    assert rc == 2
    assert "Unknown task reference 'task-20260101-000000-abcdef'" in capsys.readouterr().err


def test_task_tail_follows_a_migrated_task_without_a_workspace_claim(live_daemon, monkeypatch):
    """A task migrated from the legacy journal is followed, not refused as unknown.

    The legacy migration writes a ``task_workspace`` claim only when the old
    journal had one, but it always puts the workspace in the registry record.
    ``Maestro.status`` falls back to that record, and so must the daemon's
    ``tasks/get`` answer that ``task tail`` checks.
    """
    tid = "task-20250101-000000-1e9ac1"
    live_daemon.maestro._register_task(tid, "Legacy task", 1)
    live_daemon.maestro._write_claim(tid, "task_status", "REVIEWING")
    assert "task_workspace" not in live_daemon.maestro._claims(tid)
    calls: list[str | None] = []
    monkeypatch.setattr(cli, "_stream_task", lambda url, task_id, token=None: calls.append(task_id) or 0)
    assert _run_main_with_timeout(["task", "tail", "1"]) == 0
    assert calls == [tid]
    record_workspace = live_daemon.maestro.status(tid)["workspace"]
    assert live_daemon.status_a2a(tid)["metadata"]["workspace"] == record_workspace


def _durable_task(daemon: MaestroDaemon, tid: str, number: int, runtime: dict) -> None:
    """Record a task the way an earlier daemon run leaves it: claims only, no live record."""
    daemon.maestro._register_task(tid, "Earlier run", number)
    daemon.maestro._write_claim(tid, "task_workspace", "/old/ws")
    daemon.maestro._write_claim(tid, "task_status", "REVIEWING")
    daemon.maestro._write_claim(tid, "task_runtime", json.dumps(runtime))
    assert tid not in daemon._tasks


def test_task_tail_prints_the_result_of_a_task_from_an_earlier_daemon_run(live_daemon, capsys):
    """Tail of a task that finished before this daemon started prints its final state and exits.

    Before the fix the stream only carried events published by this daemon
    process, so no final state ever arrived and tail waited forever.
    """
    _durable_task(live_daemon, "task-20250101-000000-0ab1e5", 1, {"state": "completed"})
    rc = _run_main_with_timeout(["task", "tail", "1"], timeout=10)
    assert rc == 0
    assert "[state] completed" in capsys.readouterr().out


def test_task_tail_of_an_earlier_failed_task_prints_its_error(live_daemon, capsys):
    _durable_task(live_daemon, "task-20250101-000000-fa11ed", 1, {"state": "failed", "error": "agent crashed"})
    rc = _run_main_with_timeout(["task", "tail", "1"], timeout=10)
    assert rc == 1
    assert "[state] failed — agent crashed" in capsys.readouterr().out


def test_task_tail_of_a_finished_task_whose_events_left_the_buffer(finished_task, live_daemon, capsys):
    """A finished task whose events are no longer in the replay buffer still ends the tail.

    The buffer keeps only recent events, so on a busy daemon a finished
    task's final state event can be gone while its live record remains.
    """
    with live_daemon.bus._lock:
        live_daemon.bus._ring.clear()
    rc = _run_main_with_timeout(["task", "tail", finished_task], timeout=10)
    assert rc == 0
    assert "[state] completed" in capsys.readouterr().out


def test_final_state_event_only_for_finished_tasks(live_daemon):
    assert live_daemon.final_state_event("task-20250101-000000-0000aa") is None  # no such task
    _durable_task(live_daemon, "task-20250101-000000-9a4ced", 1, {"state": "input-required"})
    assert live_daemon.final_state_event("task-20250101-000000-9a4ced") is None  # parked, not finished
    _durable_task(live_daemon, "task-20250101-000000-d0e5ed", 2, {"state": "canceled"})
    event = live_daemon.final_state_event("task-20250101-000000-d0e5ed")
    assert event is not None and event.type == "state" and event.data == {"state": "canceled"}


def test_task_tail_all_needs_no_reference(monkeypatch):
    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://127.0.0.1:9")
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(cli, "_stream_task", lambda url, task_id, token=None: calls.append((url, task_id)) or 0)
    assert _run_main_with_timeout(["task", "tail", "--all"]) == 0
    assert calls == [("http://127.0.0.1:9", None)]


def test_task_tail_without_reference_or_all_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://127.0.0.1:9")
    assert _run_main_with_timeout(["task", "tail"]) == 2
    assert "give a task number or id, or --all" in capsys.readouterr().err


def test_task_tail_reference_and_all_together_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://127.0.0.1:9")
    assert _run_main_with_timeout(["task", "tail", "1", "--all"]) == 2
    assert "not both" in capsys.readouterr().err


def test_resolve_tail_task_rejects_empty_daemon_answer(monkeypatch):
    """A daemon answer without a task id is treated as an unknown reference."""
    monkeypatch.setattr(cli, "_post_jsonrpc", lambda url, method, params, token=None: None)
    with pytest.raises(ValueError, match="Unknown task reference '7'"):
        cli._resolve_task_on_daemon("http://127.0.0.1:9", "7", None)


# ------------------------------------ bug 4: doctor crashes without memvara

def test_doctor_reports_missing_memvara_as_blocking(tmp_path, monkeypatch, capsys):
    """A memvara storage backend without the memvara package is a blocking problem, not a traceback."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://127.0.0.1:9")
    monkeypatch.setitem(sys.modules, "memvara", None)  # makes "import memvara" fail
    (home / "config.toml").write_text('[storage]\nbackend = "memvara"\n', encoding="utf-8")
    ws = _git_repo(tmp_path)

    report = run_doctor(ws)
    assert report["ok"] is False
    assert any(p.startswith("storage backend unavailable:") and "memvara" in p for p in report["problems"])
    assert report["state"]["error"].startswith("storage backend unavailable:")
    assert "✗ storage backend unavailable" in format_doctor(report)

    assert cli.main(["doctor", "--workspace", str(ws)]) == 1
    assert "✗ storage backend unavailable" in capsys.readouterr().out


# ------------------------------------- bug 5: task shorthand after options

@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["task", "1"], ["task", "status", "1"]),
        (["--workspace", "/repo", "task", "1"], ["--workspace", "/repo", "task", "status", "1"]),
        (["--project", "/repo", "task", "1"], ["--project", "/repo", "task", "status", "1"]),
        (["--workspace=/repo", "task", "1"], ["--workspace=/repo", "task", "status", "1"]),
        (["--work", "/repo", "task", "1"], ["--work", "/repo", "task", "status", "1"]),  # argparse accepts prefixes
        (["--workspace", "task", "task", "1"], ["--workspace", "task", "task", "status", "1"]),  # a directory named "task"
        (["--workspace", "task", "list"], ["--workspace", "task", "list"]),
        (["--workspace", "/repo", "task", "tail", "1"], ["--workspace", "/repo", "task", "tail", "1"]),
        (["--workspace", "/repo", "task", "--help"], ["--workspace", "/repo", "task", "--help"]),
        (["--workspace", "/repo", "task"], ["--workspace", "/repo", "task"]),
        (["status", "task", "1"], ["status", "task", "1"]),  # "task" is an argument here, not the command
        (["--workspace", "/repo"], ["--workspace", "/repo"]),
        ([], []),
    ],
)
def test_normalize_argv_finds_the_task_command(argv, expected):
    assert cli._normalize_argv(argv) == expected


def test_task_shorthand_after_workspace_option_reaches_status(tmp_path, monkeypatch, capsys):
    """``maestro --workspace DIR task 1`` runs ``task status 1`` instead of failing to parse."""
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _run_main_with_timeout(["--workspace", str(ws), "task", "1"]) == 2
    assert "Unknown task number 1" in capsys.readouterr().err
