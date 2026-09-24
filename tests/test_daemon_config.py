"""The daemon reads the config that applies to each task's workspace, as it is
now: the user file, the project's .maestro/config.toml and the worktree's."""

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


def _repo(tmp_path: Path, name: str, config: str = "") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], env=_ENV, check=True)
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    if config:
        (ws / ".maestro").mkdir()
        (ws / ".maestro" / "config.toml").write_text(config, encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "."], env=_ENV, check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "initial"], env=_ENV, check=True)
    return ws


def _unrouted(**kw) -> HandoffDoc:
    """A handoff that names no agent, so routing comes from [defaults]."""
    base = dict(title="T", request="R", verification="none", commit_policy="no-commit")
    base.update(kw)
    return HandoffDoc(**base)


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(path))
    return path


@pytest.fixture
def daemon(home):
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
    _fake_bin(bp, "codex", "cat > /dev/null\nexit 0")
    return bp


def test_user_config_changed_after_the_daemon_started_applies(daemon, home, tmp_path, binpath):
    parked = daemon.delegate(_unrouted(), _repo(tmp_path, "first"))
    assert parked["state"] == "input-required"  # no default agent yet
    (home / "config.toml").write_text('[defaults]\nagent = "codex"\n', encoding="utf-8")
    # A second workspace, because the parked task keeps the first one.
    started = daemon.delegate(_unrouted(), _repo(tmp_path, "second"))
    assert started["state"] != "input-required"
    assert daemon.wait(started["task_id"], timeout=60)["status"]["state"] == "completed"


def test_project_config_defaults_apply_to_that_workspace_only(daemon, tmp_path, binpath):
    configured = _repo(tmp_path, "configured", '[defaults]\nagent = "codex"\n')
    plain = _repo(tmp_path, "plain")
    started = daemon.delegate(_unrouted(), configured)
    assert started["state"] != "input-required"
    assert daemon.wait(started["task_id"], timeout=60)["status"]["state"] == "completed"
    assert daemon.delegate(_unrouted(), plain)["state"] == "input-required"


def test_project_config_max_parallel_applies(daemon, tmp_path, binpath):
    gate = tmp_path / "go"
    _fake_bin(binpath, "codex", f'cat > /dev/null\nwhile [ ! -f "{gate}" ]; do sleep 0.05; done\nexit 0')
    ws = _repo(tmp_path, "ws", '[defaults]\nagent = "codex"\nmax_parallel = 1\n')
    try:
        daemon.delegate(_unrouted(commit_policy="branch"), ws)
        second = daemon.delegate(_unrouted(commit_policy="branch"), ws)
        assert second["queued"] is True
        assert "max_parallel is 1" in second["reason"] or "limit of 1" in second["reason"]
    finally:
        gate.touch()


def test_project_config_verification_timeout_applies(daemon, tmp_path):
    import time

    ws = _repo(tmp_path, "ws", "[verification]\ntimeout_s = 1\n")
    task_id = "task-t"
    doc = _unrouted(verification="command", verification_command="sleep 30")
    daemon._tasks[task_id] = {"state": "working", "workspace": str(ws)}
    daemon._record_turn_baseline(task_id, ws, doc)
    started = time.monotonic()
    assert daemon._verify(ws, task_id, doc) is False
    assert time.monotonic() - started < 20
    report = (daemon.state_dir / "tasks" / task_id / "verification.txt").read_text(encoding="utf-8")
    assert "did not finish within 1 second" in report


def test_an_invalid_project_config_refuses_the_delegation_with_its_reason(daemon, tmp_path, binpath):
    ws = _repo(tmp_path, "ws", "[defaults]\nmax_parallel = 0\n")
    with pytest.raises(ValueError, match="max_parallel must be a whole number of at least 1"):
        daemon.delegate(_unrouted(target_agent="codex", explicit_target=True), ws)


def test_an_invalid_config_edited_while_tasks_run_falls_back_to_the_startup_config(daemon, home, tmp_path):
    ws = _repo(tmp_path, "ws")
    (home / "config.toml").write_text("[defaults]\nmax_parallel = 0\n", encoding="utf-8")
    # Scheduling and verification keep working with the config the daemon started with ...
    assert daemon._config(ws) is daemon.maestro.config
    assert daemon._max_parallel(ws) == 4
    # ... while a new delegation is refused with the reason.
    with pytest.raises(ValueError, match="max_parallel"):
        daemon._config(ws, strict=True)
