"""Daemon lifecycle tests (maestro daemon start/stop/status/restart).

Unit tests cover marker parsing, liveness distinctions, and signal handling with
real subprocesses; integration tests drive the real ``maestro.daemon_main`` entry
point through :mod:`maestro.daemonctl` in isolated state directories.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maestro import daemonctl


def _dead_pid() -> int:
    """A pid that is (almost certainly) not running right now."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _reparented_pid(command: list[str]) -> int:
    """Spawn a process whose parent exits immediately.

    The child is reparented to init/launchd, which reaps it on death — so the
    pid genuinely disappears (no zombie keeping ``os.kill(pid, 0)`` alive).
    """
    import shlex

    script = "nohup " + " ".join(shlex.quote(arg) for arg in command) + " >/dev/null 2>&1 & echo $!"
    launcher = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True)
    return int(launcher.stdout.strip())


def _write_marker(state_dir: Path, **overrides) -> None:
    marker = {"pid": os.getpid(), "port": 8790, "host": "127.0.0.1",
              "started_at": datetime.now(timezone.utc).isoformat()}
    marker.update(overrides)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "daemon.json").write_text(json.dumps(marker), encoding="utf-8")


def _hold_owner_lock(state_dir: Path) -> int:
    """Hold the owner lock in this process, the way a running daemon does."""
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = daemonctl.acquire_owner_lock(state_dir)
    assert fd is not None
    return fd


_LOCK_HOLDER_SCRIPT = """
import fcntl, os, signal, sys, time
if sys.argv[2] == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.pwrite(fd, f"{os.getpid()}\\n".encode(), 0)
while True:
    time.sleep(0.1)
"""


def _wait_for_lock_holder(state_dir: Path, pid: int) -> None:
    deadline = time.monotonic() + 10
    while daemonctl.owner_lock_holder(state_dir) != pid:
        assert time.monotonic() < deadline, "the child never took the owner lock"
        time.sleep(0.05)


def _unrelated_sleeper() -> subprocess.Popen:
    """A process that is alive but is not a Maestro daemon (it stands in for a reused pid)."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _ProbeServer:
    """A tiny HTTP server standing in for the daemon's public route."""

    def __init__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence
        pass


# ------------------------------------------------------------------ status unit

def test_read_marker_absent_or_valid(tmp_path):
    assert daemonctl._read_marker(tmp_path) is None  # no file at all
    _write_marker(tmp_path, pid=1234)
    assert daemonctl._read_marker(tmp_path) == {
        "pid": 1234, "port": 8790, "host": "127.0.0.1",
        "started_at": daemonctl._read_marker(tmp_path)["started_at"],
    }


def test_status_no_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    info = daemonctl.status()
    assert info.running is False and info.pid is None and info.url is None
    assert info.detail == "no daemon marker"


def test_status_malformed_marker_json(tmp_path):
    (tmp_path / "daemon.json").write_text("not json", encoding="utf-8")
    info = daemonctl.status(state_dir=tmp_path)
    assert info.running is False and info.stale_marker is True
    assert "malformed" in info.detail


def test_status_marker_missing_keys(tmp_path):
    (tmp_path / "daemon.json").write_text(json.dumps({"port": 8790}), encoding="utf-8")
    info = daemonctl.status(state_dir=tmp_path)
    assert info.running is False and info.stale_marker is True


def test_status_marker_not_a_dict(tmp_path):
    (tmp_path / "daemon.json").write_text("[1, 2]", encoding="utf-8")
    info = daemonctl.status(state_dir=tmp_path)
    assert info.running is False and info.stale_marker is True
    assert "malformed" in info.detail


def test_status_marker_unreadable(tmp_path, monkeypatch):
    _write_marker(tmp_path)

    def _boom(*a, **k):
        raise OSError("read denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    info = daemonctl.status(state_dir=tmp_path)
    assert info.running is False and info.stale_marker is True
    assert "malformed" in info.detail


def test_status_dead_pid_is_stale(tmp_path):
    dead = _dead_pid()
    _write_marker(tmp_path, pid=dead)
    info = daemonctl.status(state_dir=tmp_path)
    assert info.running is False and info.stale_marker is True
    assert info.pid == dead
    assert "process is dead" in info.detail


def test_status_alive_but_unreachable(tmp_path, monkeypatch):
    # The marker's pid (this process) holds the owner lock, so it is the daemon.
    _write_marker(tmp_path, owner_lock=True)
    fd = _hold_owner_lock(tmp_path)
    try:
        monkeypatch.setattr(daemonctl, "probe", lambda url, timeout=3.0: False)
        info = daemonctl.status(state_dir=tmp_path)
    finally:
        os.close(fd)
    assert info.running is False and info.stale_marker is False
    assert "does not answer" in info.detail


def test_status_running(tmp_path, monkeypatch):
    _write_marker(tmp_path, started_at="2026-01-01T00:00:00+00:00", owner_lock=True)
    fd = _hold_owner_lock(tmp_path)
    try:
        monkeypatch.setattr(daemonctl, "probe", lambda url, timeout=3.0: True)
        info = daemonctl.status(state_dir=tmp_path)
    finally:
        os.close(fd)
    assert info.running is True and info.pid == os.getpid()
    assert info.url == "http://127.0.0.1:8790"
    assert info.uptime_s is not None and info.uptime_s > 0
    data = info.to_dict()
    assert data["running"] is True and data["url"] == "http://127.0.0.1:8790"


def test_status_running_with_token_and_custom_host(tmp_path, monkeypatch):
    # A marker from an older daemon has no owner_lock field; its agent card confirms it.
    _write_marker(tmp_path, host="10.0.0.5", token="sekrit")
    seen = []
    monkeypatch.setattr(daemonctl, "_answers_as_maestro", lambda url, token: seen.append((url, token)) or True)
    monkeypatch.setattr(daemonctl, "probe", lambda url, timeout=3.0: True)
    info = daemonctl.status(state_dir=tmp_path)
    assert info.running is True and seen == [("http://10.0.0.5:8790", "sekrit")]
    assert info.url == "http://10.0.0.5:8790" and info.token == "sekrit"


def test_status_zero_port_has_no_url(tmp_path, monkeypatch):
    _write_marker(tmp_path, port=0)
    monkeypatch.setattr(daemonctl, "probe", lambda url, timeout=3.0: True)
    info = daemonctl.status(state_dir=tmp_path)
    assert info.url is None and info.running is False  # probe(None-url) fails -> not answering


def test_probe_real_server():
    server = _ProbeServer()
    try:
        assert daemonctl.probe(server.url()) is True
    finally:
        server.close()


def test_probe_closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert daemonctl.probe(f"http://127.0.0.1:{port}") is False


def test_probe_bad_url():
    assert daemonctl.probe("http://[bad") is False


def test_uptime_s_variants():
    assert daemonctl._uptime_s(None) is None
    assert daemonctl._uptime_s("not-a-date") is None
    recent = datetime.now(timezone.utc).isoformat()
    assert daemonctl._uptime_s(recent) == pytest.approx(0.0, abs=2.0)
    naive = "2026-01-01T00:00:00"
    assert daemonctl._uptime_s(naive) > 0


def test_pid_alive_variants():
    assert daemonctl._pid_alive(os.getpid()) is True
    assert daemonctl._pid_alive(_dead_pid()) is False


def test_pid_alive_permission_and_oserror(monkeypatch):
    def _permission(pid, sig):
        raise PermissionError()

    monkeypatch.setattr(daemonctl.os, "kill", _permission)
    assert daemonctl._pid_alive(4194000) is True  # exists but owned by someone else

    def _oserror(pid, sig):
        raise OSError("boom")

    monkeypatch.setattr(daemonctl.os, "kill", _oserror)
    assert daemonctl._pid_alive(4194000) is False


# --------------------------------------------------------------------- stop unit

def test_stop_no_marker_is_idempotent(tmp_path):
    info = daemonctl.stop(state_dir=tmp_path)
    assert info.running is False and info.detail == "no daemon running"


def test_stop_malformed_marker_removed(tmp_path):
    (tmp_path / "daemon.json").write_text("oops", encoding="utf-8")
    info = daemonctl.stop(state_dir=tmp_path)
    assert info.stale_marker is True
    assert not (tmp_path / "daemon.json").exists()


def test_stop_dead_pid_cleans_stale_marker(tmp_path):
    _write_marker(tmp_path, pid=_dead_pid())
    info = daemonctl.stop(state_dir=tmp_path)
    assert info.running is False and info.stale_marker is True
    assert not (tmp_path / "daemon.json").exists()


def test_stop_sigterm_real_process(tmp_path):
    # Reparented target: init/launchd reaps it, so the pid really disappears.
    # It holds the owner lock the way a daemon does, so stop() knows it is the daemon.
    pid = _reparented_pid([sys.executable, "-c", _LOCK_HOLDER_SCRIPT, str(tmp_path / "daemon.owner.lock"), "default"])
    _wait_for_lock_holder(tmp_path, pid)
    _write_marker(tmp_path, pid=pid, owner_lock=True)
    info = daemonctl.stop(state_dir=tmp_path, grace_s=10)
    assert info.running is False and info.detail == "daemon stopped"
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # must be gone (SIGTERM default: terminate)
    assert not (tmp_path / "daemon.json").exists()


def test_stop_escalates_to_sigkill(tmp_path):
    # A daemon that ignores SIGTERM must be escalated to SIGKILL after the grace.
    process = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER_SCRIPT, str(tmp_path / "daemon.owner.lock"), "ignore-term"], start_new_session=True
    )
    _wait_for_lock_holder(tmp_path, process.pid)  # the handler is installed before the lock is taken
    _write_marker(tmp_path, pid=process.pid, owner_lock=True)
    info = daemonctl.stop(state_dir=tmp_path, grace_s=1)
    assert info.running is False and info.detail == "daemon stopped"
    assert process.wait(timeout=5) != 0
    assert not (tmp_path / "daemon.json").exists()


def test_stop_signal_delivery_failures(tmp_path, monkeypatch):
    # The target dies between the liveness check and each signal: both
    # ProcessLookupError paths must be tolerated.
    _write_marker(tmp_path, pid=4194000)

    def _fake_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(daemonctl.os, "kill", _fake_kill)
    calls = {"n": 0}

    def _fake_alive(pid):
        calls["n"] += 1
        return calls["n"] <= 2  # pre-check and post-grace checks see it alive

    monkeypatch.setattr(daemonctl, "_pid_alive", _fake_alive)
    monkeypatch.setattr(daemonctl, "_identity_confirmed", lambda base, marker, pid, url: True)
    info = daemonctl.stop(state_dir=tmp_path, grace_s=0)
    assert info.running is False and info.detail == "daemon stopped"
    assert not (tmp_path / "daemon.json").exists()


def test_stop_marker_missing_pid_key(tmp_path):
    (tmp_path / "daemon.json").write_text(json.dumps({"port": 8790}), encoding="utf-8")
    info = daemonctl.stop(state_dir=tmp_path)
    assert info.running is False and info.stale_marker is True
    assert not (tmp_path / "daemon.json").exists()


def test_stop_grace_from_env(tmp_path, monkeypatch):
    _write_marker(tmp_path, pid=_dead_pid())
    monkeypatch.setenv("MAESTRO_DAEMON_STOP_GRACE_S", "2.5")
    info = daemonctl.stop(state_dir=tmp_path)
    assert info.stale_marker is True


def test_stop_grace_env_invalid_falls_back(tmp_path, monkeypatch):
    _write_marker(tmp_path, pid=_dead_pid())
    monkeypatch.setenv("MAESTRO_DAEMON_STOP_GRACE_S", "not-a-number")
    info = daemonctl.stop(state_dir=tmp_path)
    assert info.stale_marker is True


# ---------------------------------------------------------------- start (real daemon)

@pytest.fixture()
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    return home


def test_daemon_start_stop_restart_cycle(state_home):
    info = daemonctl.start()
    try:
        assert info.running is True and info.already_running is False
        assert info.pid is not None and info.port is not None
        assert info.url.startswith("http://127.0.0.1:")
        marker = json.loads((state_home / "daemon.json").read_text(encoding="utf-8"))
        assert marker["pid"] == info.pid and marker["port"] == info.port
        # The detached daemon answers HTTP from a fresh client process view.
        assert daemonctl.probe(info.url) is True
        # Duplicate start reuses the running daemon.
        again = daemonctl.start()
        assert again.already_running is True and again.pid == info.pid
    finally:
        stopped = daemonctl.stop()
    assert stopped.running is False
    assert not (state_home / "daemon.json").exists()
    # Stop again: idempotent.
    assert daemonctl.stop().running is False
    # Restart from stopped state starts a fresh daemon.
    restarted = daemonctl.restart()
    try:
        assert restarted.running is True and restarted.already_running is False
    finally:
        daemonctl.stop()


def test_daemon_start_replaces_stale_marker(state_home):
    _write_marker(state_home, pid=_dead_pid(), port=1)
    info = daemonctl.start()
    try:
        assert info.running is True and info.already_running is False
        marker = json.loads((state_home / "daemon.json").read_text(encoding="utf-8"))
        assert marker["pid"] == info.pid
    finally:
        daemonctl.stop()


def test_daemon_start_child_crash_reports_log(state_home, monkeypatch):
    monkeypatch.setattr(
        daemonctl, "_spawn_command",
        lambda base: [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); raise SystemExit(3)"],
    )
    with pytest.raises(RuntimeError, match="exited while starting"):
        daemonctl.start(ready_timeout_s=10)


def test_daemon_start_child_never_ready(state_home, monkeypatch):
    monkeypatch.setattr(
        daemonctl, "_spawn_command",
        lambda base: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    with pytest.raises(RuntimeError, match="did not become ready"):
        daemonctl.start(ready_timeout_s=1)


def test_daemon_start_spawn_failure(state_home, monkeypatch):
    def _boom(base):
        raise OSError("no such interpreter")

    monkeypatch.setattr(daemonctl, "_spawn_command", _boom)
    with pytest.raises(RuntimeError, match="failed to launch"):
        daemonctl.start()


def test_daemon_start_live_pid_not_answering(state_home, monkeypatch):
    # A confirmed daemon (it holds the owner lock) that does not answer HTTP.
    _write_marker(state_home, pid=os.getpid(), port=1, owner_lock=True)
    fd = _hold_owner_lock(state_home)
    try:
        monkeypatch.setattr(daemonctl, "probe", lambda url, timeout=3.0: False)
        with pytest.raises(RuntimeError, match="exists but is not answering"):
            daemonctl.start()
    finally:
        os.close(fd)


def test_start_lock_contention_times_out(state_home, monkeypatch):
    import fcntl

    lock_path = state_home / "daemon.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        monkeypatch.setattr(daemonctl, "LOCK_WAIT_S", 1)
        with pytest.raises(TimeoutError, match="start lock"):
            daemonctl.start()
    finally:
        os.close(fd)


def test_terminate_kills_session_group():
    process = subprocess.Popen(["sh", "-c", 'trap "" TERM; while :; do sleep 0.1; done'], start_new_session=True)
    time.sleep(0.3)  # ensure the trap is armed before _terminate sends SIGTERM
    daemonctl._terminate(process, grace_s=1)
    assert process.wait(timeout=5) != 0


def test_terminate_already_dead():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    daemonctl._terminate(process, grace_s=0.1)  # must not raise


def test_terminate_killpg_falls_back_to_process_kill(monkeypatch):
    import signal as _signal

    process = subprocess.Popen(
        ["sh", "-c", 'trap "" TERM; while :; do sleep 0.1; done'], start_new_session=True
    )
    time.sleep(0.3)  # ensure the trap is armed before SIGTERM
    real_killpg = os.killpg

    def _killpg(pgid, sig):
        if sig == _signal.SIGKILL:
            raise ProcessLookupError()
        return real_killpg(pgid, sig)

    monkeypatch.setattr(daemonctl.os, "killpg", _killpg)
    daemonctl._terminate(process, grace_s=1)
    assert process.wait(timeout=5) is not None  # Popen.kill() finished the job


def test_tail_missing_file(tmp_path):
    assert daemonctl._tail(tmp_path / "nope.log") == "(no log)"


def test_tail_returns_last_lines(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("\n".join(f"line-{i}" for i in range(30)), encoding="utf-8")
    assert daemonctl._tail(path, lines=5).splitlines() == [f"line-{i}" for i in range(25, 30)]


# ------------------------------------------------------------- CLI integration

def test_cli_daemon_status_stopped(state_home, capsys):
    from maestro import cli

    rc = cli.main(["daemon", "status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Maestro daemon stopped" in out and f"State: {state_home}" in out


def test_cli_daemon_status_json(state_home, capsys):
    from maestro import cli

    rc = cli.main(["daemon", "status", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1 and data["running"] is False and data["already_running"] is False


def test_cli_daemon_start_stop_restart(state_home, capsys):
    from maestro import cli

    rc = cli.main(["daemon", "start"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Maestro daemon started" in out and "PID:" in out and "URL:" in out
    first_pid = int(out.split("PID:")[1].split()[0])

    # Duplicate start reports already running with the same PID.
    rc = cli.main(["daemon", "start"])
    out = capsys.readouterr().out
    assert rc == 0 and "Maestro daemon already running" in out
    assert int(out.split("PID:")[1].split()[0]) == first_pid

    rc = cli.main(["daemon", "status"])
    out = capsys.readouterr().out
    assert rc == 0 and "running" in out and "Uptime:" in out

    rc = cli.main(["daemon", "restart"])
    out = capsys.readouterr().out
    assert rc == 0 and "Maestro daemon restarted" in out

    rc = cli.main(["daemon", "stop"])
    out = capsys.readouterr().out
    assert rc == 0 and "stopped" in out

    rc = cli.main(["daemon", "status"])
    assert rc == 1
    capsys.readouterr()


def test_cli_daemon_start_failure_exit_code(state_home, monkeypatch, capsys):
    from maestro import cli

    monkeypatch.setattr(daemonctl, "_spawn_command", lambda base: ["/nonexistent/interpreter"])
    rc = cli.main(["daemon", "start"])
    assert rc == 1
    assert "failed to launch" in capsys.readouterr().err


def test_cli_daemon_status_failure_exit_code(state_home, monkeypatch, capsys):
    from maestro import cli

    def _boom():
        raise RuntimeError("state dir on fire")

    monkeypatch.setattr(daemonctl, "status", _boom)
    rc = cli.main(["daemon", "status"])
    assert rc == 1 and "on fire" in capsys.readouterr().err


def test_cli_daemon_stop_failure_exit_code(state_home, monkeypatch, capsys):
    from maestro import cli

    def _boom():
        raise TimeoutError("lock wait")

    monkeypatch.setattr(daemonctl, "stop", _boom)
    rc = cli.main(["daemon", "stop"])
    assert rc == 1 and "lock wait" in capsys.readouterr().err


def test_cli_daemon_restart_failure_exit_code(state_home, monkeypatch, capsys):
    from maestro import cli

    def _boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(daemonctl, "restart", _boom)
    rc = cli.main(["daemon", "restart"])
    assert rc == 1 and "nope" in capsys.readouterr().err


def test_cli_daemon_status_running_json(state_home, capsys):
    from maestro import cli

    daemonctl.start()
    try:
        rc = cli.main(["daemon", "status", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 0 and data["running"] is True and data["pid"] is not None
    finally:
        daemonctl.stop()


# ------------------------------------------------ identity before signalling (reused pids)

@pytest.mark.parametrize("owner_lock", [True, False], ids=["current-marker", "older-marker"])
def test_status_reports_a_reused_pid_as_stale(tmp_path, owner_lock):
    sleeper = _unrelated_sleeper()
    try:
        extra = {"owner_lock": True} if owner_lock else {}
        _write_marker(tmp_path, pid=sleeper.pid, port=_closed_port(), **extra)
        info = daemonctl.status(state_dir=tmp_path)
        assert info.running is False and info.stale_marker is True
        assert "not the Maestro daemon" in info.detail
        assert (tmp_path / "daemon.json").exists()  # status is read-only
    finally:
        sleeper.kill()
        sleeper.wait()


@pytest.mark.parametrize("owner_lock", [True, False], ids=["current-marker", "older-marker"])
def test_stop_never_signals_a_process_that_reused_the_pid(tmp_path, owner_lock):
    sleeper = _unrelated_sleeper()
    try:
        extra = {"owner_lock": True} if owner_lock else {}
        _write_marker(tmp_path, pid=sleeper.pid, port=_closed_port(), **extra)
        info = daemonctl.stop(state_dir=tmp_path, grace_s=1)
        time.sleep(0.2)
        assert sleeper.poll() is None, "stop() signalled a process that is not the daemon"
        assert info.running is False and info.stale_marker is True
        assert "not signalled" in info.detail and str(sleeper.pid) in info.detail
        assert not (tmp_path / "daemon.json").exists()
    finally:
        sleeper.kill()
        sleeper.wait()


def test_start_replaces_a_marker_whose_pid_was_reused(state_home):
    sleeper = _unrelated_sleeper()
    try:
        _write_marker(state_home, pid=sleeper.pid, port=_closed_port())
        info = daemonctl.start()
        try:
            assert info.running is True and info.pid != sleeper.pid
        finally:
            daemonctl.stop()
        assert sleeper.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait()


def test_owner_lock_holder_and_acquire(tmp_path):
    assert daemonctl.owner_lock_holder(tmp_path) is None  # no lock file yet
    fd = _hold_owner_lock(tmp_path)
    try:
        assert daemonctl.owner_lock_holder(tmp_path) == os.getpid()
        assert daemonctl.acquire_owner_lock(tmp_path, wait_s=0.2) is None  # already held
        os.pwrite(fd, b"garbage", 0)
        assert daemonctl.owner_lock_holder(tmp_path) == 0  # held, but the pid is unreadable
    finally:
        daemonctl.release_owner_lock(fd)
    assert daemonctl.owner_lock_holder(tmp_path) is None  # file exists, nobody holds it


class _CardHandler(http.server.BaseHTTPRequestHandler):
    """Answers the agent card only with the right bearer token; other paths return a list."""

    def do_GET(self):  # noqa: N802
        if self.path != "/.well-known/agent.json":
            body = b"[]"
        elif self.headers.get("Authorization") != "Bearer sekrit":
            self.send_response(401)
            self.end_headers()
            return
        else:
            body = json.dumps({"name": "maestro-node", "url": "http://x", "capabilities": {}}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def test_answers_as_maestro_checks_the_agent_card():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    plain = _ProbeServer()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert daemonctl._answers_as_maestro(url, "sekrit") is True
        assert daemonctl._answers_as_maestro(url, None) is False  # 401 without the token
        assert daemonctl._answers_as_maestro(plain.url(), None) is False  # answers, but not JSON
        assert daemonctl._answers_as_maestro(f"http://127.0.0.1:{_closed_port()}", None) is False
    finally:
        server.shutdown()
        server.server_close()
        plain.close()


def test_answers_as_maestro_rejects_json_that_is_not_a_card(monkeypatch):
    class _Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    for body in (b"[1, 2]", b'{"name": "something else"}'):
        monkeypatch.setattr(daemonctl.urllib.request, "urlopen", lambda request, timeout, body=body: _Resp(body))
        assert daemonctl._answers_as_maestro("http://127.0.0.1:1", None) is False


def test_older_marker_without_port_cannot_be_confirmed(tmp_path):
    _write_marker(tmp_path, port=0)
    assert daemonctl._identity_confirmed(tmp_path, daemonctl._read_marker(tmp_path), os.getpid(), None) is False


def test_live_owner_variants(tmp_path, monkeypatch):
    assert daemonctl.live_owner(tmp_path) is None  # no marker at all
    _write_marker(tmp_path, owner_lock=True)
    fd = _hold_owner_lock(tmp_path)
    try:
        monkeypatch.setattr(daemonctl, "probe", lambda url, timeout=3.0: True)
        assert daemonctl.live_owner(tmp_path).running is True
        monkeypatch.setattr(daemonctl, "probe", lambda url, timeout=3.0: False)
        hung = daemonctl.live_owner(tmp_path)  # confirmed but not answering still owns the directory
        assert hung is not None and hung.running is False and hung.pid == os.getpid()
    finally:
        os.close(fd)
    assert daemonctl.live_owner(tmp_path) is None  # nobody holds the lock: the marker is stale
