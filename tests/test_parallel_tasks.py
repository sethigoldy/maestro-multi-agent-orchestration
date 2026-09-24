"""Parallel tasks in one workspace: where each turn runs (design-parallel-tasks.md)."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from maestro.daemon import MaestroDaemon
from maestro.handoff import HandoffDoc

_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _repo(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for args in (("init", "-q"),):
        subprocess.run(["git", "-C", str(ws), *args], env=_ENV, check=True)
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "."], env=_ENV, check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "initial"], env=_ENV, check=True)
    return ws


def _doc(**kw) -> HandoffDoc:
    base = dict(title="T", request="R", verification="none", commit_policy="branch", target_agent="codex", explicit_target=True)
    base.update(kw)
    return HandoffDoc(**base)


def _daemon(tmp_path, monkeypatch, config: str = "") -> MaestroDaemon:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if config:
        (home / "config.toml").write_text(config, encoding="utf-8")
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    return MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    return bp


def _slow_agent(binpath: Path, gate: Path) -> None:
    # Waits until the test creates the gate file, so tasks overlap in time.
    _fake_bin(binpath, "codex", f'cat > /dev/null\nwhile [ ! -f "{gate}" ]; do sleep 0.05; done\necho "$PWD" > ran-here.txt\nexit 0')


def _stop(d: MaestroDaemon, gate: Path) -> None:
    gate.touch()
    for tid in list(d._tasks):
        try:
            d.wait(tid, timeout=30)
        except Exception:
            pass
    d.stop()


def test_second_task_runs_in_a_worktree_at_once(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        first = d.delegate(_doc(title="first"), ws)
        second = d.delegate(_doc(title="second"), ws)
        assert first["queued"] is False and first["run_dir"] == str(ws)
        assert second["queued"] is False
        assert second["run_dir"] == str(d.state_dir / "worktrees" / second["task_id"])
    finally:
        _stop(d, gate)


def test_limit_queues_with_a_reason_and_releases_on_finish(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch, "[defaults]\nmax_parallel = 2\n")
    try:
        d.delegate(_doc(), ws)
        d.delegate(_doc(), ws)
        third = d.delegate(_doc(), ws)
        assert third["queued"] is True
        assert "limit of 2 running tasks" in third["reason"]
        gate.touch()
        final = d.wait(third["task_id"], timeout=60)
        assert final["status"]["state"] == "completed"
    finally:
        _stop(d, gate)


def test_max_parallel_one_keeps_todays_queue(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch, "[defaults]\nmax_parallel = 1\n")
    try:
        d.delegate(_doc(), ws)
        assert d.delegate(_doc(), ws)["queued"] is True
    finally:
        _stop(d, gate)


def test_no_commit_task_waits_for_the_workspace(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        d.delegate(_doc(), ws)
        waiting = d.delegate(_doc(commit_policy="no-commit"), ws)
        assert waiting["queued"] is True
        assert "works in place" in waiting["reason"]
    finally:
        _stop(d, gate)


def test_followup_of_a_workspace_task_queues_while_another_task_uses_the_workspace(tmp_path, monkeypatch, binpath):
    _fake_bin(binpath, "codex", "cat > /dev/null\nexit 0")
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        done = d.delegate(_doc(), ws)
        d.wait(done["task_id"], timeout=60)
        gate = tmp_path / "go"
        _slow_agent(binpath, gate)
        d.delegate(_doc(), ws)  # takes the workspace, which is free again
        result = d.followup(done["task_id"], "more")
        assert result["queued"] is True  # stays in the workspace, so it waits
        assert "this task's work is in the workspace" in result["reason"]
        assert d._tasks[done["task_id"]]["run_dir_kind"] == "workspace"
    finally:
        _stop(d, tmp_path / "go")


def test_parked_worktree_task_frees_its_place_under_the_limit(tmp_path, monkeypatch, binpath):
    from maestro.agents import AgentSpec

    gate = tmp_path / "go"
    ws = _repo(tmp_path)
    _slow_agent(binpath, gate)  # "codex": keeps running until the gate exists
    _fake_bin(binpath, "asker", 'cat > /dev/null\necho \'{"question": "which db?"}\'\nexit 0')
    d = _daemon(tmp_path, monkeypatch, "[defaults]\nmax_parallel = 2\n")
    d.registry.save(AgentSpec(name="asker", kind="generic", command="asker --go", output_format="jsonl"))
    try:
        d.delegate(_doc(), ws)                                      # runs in the workspace until the gate
        parked = d.delegate(_doc(target_agent="asker"), ws)         # worktree; parks on its question
        assert d.wait(parked["task_id"], timeout=30)["status"]["state"] == "input-required"
        third = d.delegate(_doc(), ws)
        assert third["queued"] is False                             # the parked task does not count
    finally:
        _stop(d, gate)


def test_queued_task_starts_when_a_running_task_parks(tmp_path, monkeypatch, binpath):
    from maestro.agents import AgentSpec

    gate = tmp_path / "go"
    ask_gate = tmp_path / "ask"
    ws = _repo(tmp_path)
    _slow_agent(binpath, gate)
    # Asks its question only once the test creates ask_gate, so it is running
    # (and counted) when the third task is delegated.
    _fake_bin(binpath, "asker", f'cat > /dev/null\nwhile [ ! -f "{ask_gate}" ]; do sleep 0.05; done\necho \'{{"question": "which db?"}}\'\nexit 0')
    d = _daemon(tmp_path, monkeypatch, "[defaults]\nmax_parallel = 2\n")
    d.registry.save(AgentSpec(name="asker", kind="generic", command="asker --go", output_format="jsonl"))
    try:
        d.delegate(_doc(), ws)
        d.delegate(_doc(target_agent="asker"), ws)
        third = d.delegate(_doc(), ws)
        assert third["queued"] is True
        sub = d.bus.subscribe("state")
        try:
            ask_gate.touch()
            started = sub.wait(predicate=lambda e: e.task_id == third["task_id"] and e.data.get("state") == "working", timeout=30)
        finally:
            sub.close()
        assert started is not None  # it started when the other task parked, not when the first finished
        assert not gate.exists()
    finally:
        _stop(d, gate)
