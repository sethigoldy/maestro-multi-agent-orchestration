"""What the web console reads from the daemon (docs/design-console.md)."""

from __future__ import annotations

import os
import stat
import subprocess
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


def _repo(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], env=_ENV, check=True)
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "."], env=_ENV, check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "initial"], env=_ENV, check=True)
    return ws


def _doc(**kw) -> HandoffDoc:
    base = dict(title="T", request="R", verification="none", commit_policy="no-commit", target_agent="codex", explicit_target=True)
    base.update(kw)
    return HandoffDoc(**base)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    yield d
    for tid, rec in list(d._tasks.items()):
        if rec.get("state") not in ("completed", "failed", "canceled"):
            try:
                d.cancel(tid)
            except (KeyError, ValueError):
                pass
    d.stop()


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    return bp


def test_a_parked_task_carries_its_question_and_what_it_waits_for(daemon, tmp_path, binpath):
    parked = daemon.delegate(_doc(target_agent="codex", explicit_target=False), _repo(tmp_path))
    meta = daemon.status_a2a(parked["task_id"])["metadata"]
    assert meta["awaiting"] == "routing"
    assert "Which agent (and model) should run this task?" in meta["question"]
    assert meta["started_at"]


def test_a_parked_task_read_back_after_a_restart_keeps_its_question(daemon, tmp_path, binpath):
    parked = daemon.delegate(_doc(sensitive=True), _repo(tmp_path))
    daemon._tasks.clear()  # as after a restart: only the durable record is left
    meta = daemon.status_a2a(parked["task_id"])["metadata"]
    assert meta["awaiting"] == "approval" and "sensitive workspace" in meta["question"]


def test_a_queued_task_carries_its_reason_and_a_running_one_its_run_dir_kind(daemon, tmp_path, binpath):
    gate = tmp_path / "go"
    _fake_bin(binpath, "codex", f'cat > /dev/null\nwhile [ ! -f "{gate}" ]; do sleep 0.05; done\nexit 0')
    ws = _repo(tmp_path)
    try:
        running = daemon.delegate(_doc(), ws)
        waiting = daemon.delegate(_doc(), ws)
        first = daemon.status_a2a(running["task_id"])["metadata"]
        second = daemon.status_a2a(waiting["task_id"])["metadata"]
        assert first["run_dir_kind"] == "workspace" and first["queued"] is False and "queue_reason" not in first
        assert second["queued"] is True and "works in place" in second["queue_reason"]
        assert "question" not in first and "awaiting" not in first
    finally:
        gate.touch()


def test_agents_list_also_returns_installed_agents_that_are_not_registered(daemon, binpath):
    from maestro.a2a import A2ADispatcher
    from maestro.agents import AgentSpec

    _fake_bin(binpath, "codex", "exit 0")
    _fake_bin(binpath, "claude", "exit 0")
    daemon.registry.save(AgentSpec(name="cc", kind="claude_code"))
    result = A2ADispatcher(daemon).handle({"jsonrpc": "2.0", "id": 1, "method": "agents/list", "params": {}})["result"]
    assert [a["name"] for a in result["agents"]] == ["cc"]
    discovered = {a["name"]: a for a in result["discovered"]}
    assert "codex" in discovered and discovered["codex"]["status"]["found"] is True
    assert "claude_code" not in discovered  # already registered, as cc


def test_a_new_task_branch_is_announced_so_the_console_shows_it(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", "cat > /dev/null\nexit 0")
    sub = daemon.bus.subscribe("branch")
    try:
        started = daemon.delegate(_doc(commit_policy="branch"), _repo(tmp_path))
        event = sub.wait(predicate=lambda e: e.task_id == started["task_id"], timeout=30)
    finally:
        sub.close()
    assert event is not None and event.data["branch"] == f"maestro/{started['task_id']}"
    assert event.data.get("old_branch") is None
