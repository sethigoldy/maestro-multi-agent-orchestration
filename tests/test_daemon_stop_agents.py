"""Stopping the daemon also stops the agents it started for running tasks."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from maestro.daemon import MaestroDaemon
from maestro.handoff import HandoffDoc

_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    path = dirpath / name
    probes = 'case " $* " in *" --version "*|*" --help "*|" login "*) echo "' + name + ' 0.0.0"; exit 0;; esac'
    path.write_text(f"#!/bin/sh\n{probes}\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _repo(tmp_path: Path, name: str) -> Path:
    ws = tmp_path / name
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], env=_ENV, check=True)
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "."], env=_ENV, check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "initial"], env=_ENV, check=True)
    return ws


def _doc() -> HandoffDoc:
    return HandoffDoc(title="T", request="R", verification="none", commit_policy="no-commit", target_agent="codex", explicit_target=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A process that exited but was not reaped yet is a zombie: it is gone too.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], text=True, capture_output=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_for_file(path: Path, timeout: float = 20) -> int:
    deadline = time.monotonic() + timeout
    while not (path.is_file() and path.read_text().strip()):
        assert time.monotonic() < deadline, f"{path} was never written"
        time.sleep(0.05)
    return int(path.read_text().strip())


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    # The agent records its own pid and a background child's, then waits.
    _fake_bin(bp, "codex", 'cat > /dev/null\necho $$ > agent.pid\nsleep 120 &\necho $! > child.pid\nwait')
    return bp


def _daemon(tmp_path: Path, name: str, monkeypatch) -> MaestroDaemon:
    home = tmp_path / name
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    return MaestroDaemon(state_dir=home, start_http=False, max_retries=2, backoff_s=0)


def test_stopping_the_daemon_stops_its_running_agents_and_their_children(tmp_path, binpath, monkeypatch):
    daemon = _daemon(tmp_path, "home", monkeypatch)
    ws = _repo(tmp_path, "ws")
    started = daemon.delegate(_doc(), ws)
    agent = _wait_for_file(ws / "agent.pid")
    child = _wait_for_file(ws / "child.pid")
    daemon.stop()
    assert not _alive(agent) and not _alive(child)
    assert daemon._tasks[started["task_id"]]["state"] == "failed"
    time.sleep(1)
    # No retry started a new agent after the stop: the pid file is still the first agent's.
    assert int((ws / "agent.pid").read_text().strip()) == agent


def test_stopping_one_daemon_leaves_other_processes_alone(tmp_path, binpath, monkeypatch):
    unrelated = subprocess.Popen(["sleep", "120"], start_new_session=True)
    first = _daemon(tmp_path, "home1", monkeypatch)
    second = _daemon(tmp_path, "home2", monkeypatch)
    ws1, ws2 = _repo(tmp_path, "ws1"), _repo(tmp_path, "ws2")
    try:
        first.delegate(_doc(), ws1)
        second.delegate(_doc(), ws2)
        mine = _wait_for_file(ws1 / "agent.pid")
        theirs = _wait_for_file(ws2 / "agent.pid")
        first.stop()
        assert not _alive(mine)
        assert _alive(theirs) and unrelated.poll() is None
    finally:
        second.stop()
        unrelated.kill()
        unrelated.wait()
    assert not _alive(theirs)
