"""Recursion-protection tests: agents launched by Maestro must carry the
MAESTRO_AGENT_CONTEXT marker so their global skill never re-delegates."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from maestro.adapters.base import (
    MAESTRO_CONTEXT_ENV,
    MAESTRO_ROLE_ENV,
    MAESTRO_TASK_ID_ENV,
    worker_environment,
)
from maestro.agents import AgentSpec
from maestro.adapters.generic import GenericAdapter
from maestro.adapters.pi import PiAdapter


def _fake_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_worker_environment_sets_markers(monkeypatch):
    monkeypatch.setenv(MAESTRO_CONTEXT_ENV, "bogus-preexisting")
    env = worker_environment("task-42")
    assert env[MAESTRO_CONTEXT_ENV] == "1"  # overwrites any inherited value
    assert env[MAESTRO_TASK_ID_ENV] == "task-42"
    assert env[MAESTRO_ROLE_ENV] == "implementation"


def test_spawn_agent_receives_context(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(
        bindir / "probe-agent",
        'echo "CTX=$MAESTRO_AGENT_CONTEXT TASK=$MAESTRO_TASK_ID ROLE=$MAESTRO_ROLE"',
    )
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    spec = AgentSpec(name="probe", kind="generic", command="probe-agent {prompt}")
    adapter = GenericAdapter(spec)
    result = adapter.run("do the thing", tmp_path, "task-xyz", log_dir=tmp_path / "logs")
    assert result.ok is True
    log = Path(result.output_path).read_text(encoding="utf-8")
    assert "CTX=1 TASK=task-xyz ROLE=implementation" in log


def test_spawn_agent_env_overrides_inherited(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(bindir / "probe-agent", 'echo "CTX=$MAESTRO_AGENT_CONTEXT"')
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv(MAESTRO_CONTEXT_ENV, "0")
    spec = AgentSpec(name="probe", kind="generic", command="probe-agent {prompt}")
    adapter = GenericAdapter(spec)
    result = adapter.run("x", tmp_path, "task-1", log_dir=tmp_path / "logs2")
    assert result.ok is True
    log = Path(result.output_path).read_text(encoding="utf-8")
    assert "CTX=1" in log  # inherited 0 was replaced by the worker marker


def test_rpc_agent_receives_context(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_executable(
        bindir / "pi",
        'echo "CTX=$MAESTRO_AGENT_CONTEXT TASK=$MAESTRO_TASK_ID ROLE=$MAESTRO_ROLE"\n'
        "read -r line\n"
        "echo '{\"type\": \"agent_settled\"}'",
    )
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    spec = AgentSpec(name="pi", kind="pi")
    adapter = PiAdapter(spec)
    log_dir = tmp_path / "logs"
    result = adapter.run("x", tmp_path, "task-rpc", log_dir=log_dir)
    assert result.ok is True
    log_files = list(log_dir.glob("pi-task-rpc.log"))
    assert log_files, "rpc run must write its transcript log"
    text = log_files[0].read_text(encoding="utf-8")
    assert "CTX=1 TASK=task-rpc ROLE=implementation" in text
