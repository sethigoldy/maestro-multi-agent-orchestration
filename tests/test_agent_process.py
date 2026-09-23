"""Agent processes: odd output bytes, leftover children, large prompts and kills.

Each test reproduces a defect found in review: a run killed by one byte that
is not valid UTF-8, a run that hung (or reported a timeout) because a child
the agent left running held its output open, a large prompt that deadlocked
the pipes, and a group kill that was skipped once the agent itself had exited.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

from maestro.adapters.base import BaseAdapter, _kill_group, _next_line
from maestro.adapters.generic import GenericAdapter
from maestro.agents import AgentSpec


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    # The guard keeps version probes (run in the test's current directory)
    # from doing anything; only a real run passes "--go".
    path = dirpath / name
    path.write_text(f'#!/bin/sh\n[ "$1" = "--go" ] || exit 0\n{body}\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    monkeypatch.setenv("MAESTRO_LOGIN_ENV", "0")
    return bp


def _agent(name: str, input_mode: str = "stdin") -> GenericAdapter:
    return GenericAdapter(AgentSpec(name=name, kind="generic", command=f"{name} --go", input_mode=input_mode))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - pid reused by another user's process
        return True
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def test_output_that_is_not_utf8_does_not_kill_the_run(binpath, tmp_path):
    _fake_bin(binpath, "latin", "cat > /dev/null\nprintf 'caf\\351\\n'\nsleep 1\necho done\nexit 0")
    result = _agent("latin").run("p", tmp_path, "t", timeout=30, log_dir=tmp_path / "logs")
    assert result.ok is True, result.error
    log = Path(result.output_path).read_text(encoding="utf-8")
    assert "caf�" in log and "done" in log


def test_a_background_child_holding_the_output_does_not_hang_the_run(binpath, tmp_path):
    pidfile = tmp_path / "child.pid"
    _fake_bin(binpath, "leaver", f"cat > /dev/null\nsleep 30 &\necho $! > {pidfile}\necho finished\nexit 0")
    started = time.monotonic()
    result = _agent("leaver").run("p", tmp_path, "t", timeout=20)
    assert result.ok is True and result.exit_code == 0, result.error
    assert time.monotonic() - started < 10  # the exit grace, not the child's 30 s
    assert _wait_dead(int(pidfile.read_text().strip()))  # the leftover child was stopped


def test_a_detached_child_is_stopped_after_a_successful_run(binpath, tmp_path):
    pidfile = tmp_path / "child.pid"
    _fake_bin(binpath, "detacher", f"cat > /dev/null\nsleep 30 > /dev/null 2>&1 &\necho $! > {pidfile}\nexit 0")
    result = _agent("detacher").run("p", tmp_path, "t", timeout=20)
    assert result.ok is True
    assert _wait_dead(int(pidfile.read_text().strip()))


def test_a_large_prompt_does_not_deadlock_a_chatty_agent(binpath, tmp_path):
    # The agent prints 200 KB before reading its 300 KB prompt: both pipes fill
    # unless the prompt is written while the output is being read.
    _fake_bin(binpath, "chatty", "head -c 200000 /dev/zero | tr '\\0' 'x'\necho\ncat > /dev/null\necho read-it\nexit 0")
    started = time.monotonic()
    result = _agent("chatty").run("y" * 300_000, tmp_path, "t", timeout=20, log_dir=tmp_path / "logs")
    assert result.ok is True, result.error
    assert time.monotonic() - started < 15
    assert "read-it" in Path(result.output_path).read_text(encoding="utf-8")


def test_kill_group_reaches_children_after_the_agent_exited(tmp_path):
    # The group leader exits (and is not reaped yet) while its child runs on.
    # os.getpgid(leader) fails in that state, which used to skip the kill.
    process = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 & echo $!; exit 0"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    child = int(process.stdout.readline().strip())
    time.sleep(0.3)
    _kill_group(process)
    assert _wait_dead(child)
    process.stdout.close()


def test_kill_group_escalates_to_sigkill(tmp_path):
    process = subprocess.Popen(
        ["/bin/sh", "-c", "trap '' TERM; echo ready; while :; do sleep 0.1; done"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    assert process.stdout.readline().strip() == "ready"
    started = time.monotonic()
    _kill_group(process)
    assert process.poll() == -signal.SIGKILL
    assert time.monotonic() - started < 15
    process.stdout.close()


def test_next_line_reports_idle():
    import queue

    q: queue.Queue = queue.Queue()
    assert _next_line(q, None, None, lambda: True) == ("idle", None)
    calls = []
    assert _next_line(q, time.monotonic() + 0.8, None, lambda: calls.append(1) is not None) == ("timeout", None)
    assert calls  # idle was polled while waiting


class _SlowRpc(BaseAdapter):
    """An rpc agent that never reads its stdin, so a large start command blocks."""

    kind = "slowrpc"
    mode = "rpc"

    def binary(self):
        return "slowrpc"

    def build_command(self, prompt, workspace, task_id, settings):
        return ["slowrpc", "--go"]

    def rpc_start_command(self, prompt, task_id):
        return {"type": "prompt", "message": prompt}

    def rpc_abort_command(self):
        return {"type": "abort"}

    def rpc_event(self, event):
        return {}


def test_rpc_cancel_does_not_interleave_the_abort_with_a_blocked_start(binpath, tmp_path):
    _fake_bin(binpath, "slowrpc", "sleep 30")
    started = time.monotonic()
    result = _SlowRpc(AgentSpec(name="slowrpc", kind="generic")).run(
        "z" * 1_000_000, tmp_path, "t", timeout=60, should_cancel=lambda: True,
    )
    assert result.ok is False and "canceled" in (result.error or "")
    assert time.monotonic() - started < 20


def test_rpc_agent_that_exits_while_a_child_holds_the_output(binpath, tmp_path):
    _fake_bin(binpath, "slowrpc", "sleep 30 &\nexit 0")
    started = time.monotonic()
    result = _SlowRpc(AgentSpec(name="slowrpc", kind="generic")).run("p", tmp_path, "t", timeout=60)
    assert result.ok is False and "closed its stream before settling" in (result.error or "")
    assert time.monotonic() - started < 15
