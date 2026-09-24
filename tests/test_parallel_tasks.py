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
    # Before a task, the adapter runs probes such as "<agent> --version",
    # "<agent> exec --help" and "<agent> login status" from the test's working
    # directory. Answer them at once, so the agent body (which may write files
    # into its current directory) runs only as a real task.
    probes = 'case " $* " in *" --version "*|*" --help "*|" login "*) echo "' + name + ' 0.0.0"; exit 0;; esac'
    path.write_text(f"#!/bin/sh\n{probes}\n{body}\n", encoding="utf-8")
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


def test_worktree_task_changes_stay_in_its_worktree(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        first = d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        gate.touch()
        d.wait(first["task_id"], timeout=60)
        final = d.wait(second["task_id"], timeout=60)
        wt = Path(second["run_dir"])
        assert (ws / "ran-here.txt").read_text().strip() == str(ws)
        assert (wt / "ran-here.txt").read_text().strip() == str(wt)
        assert final["metadata"]["run_dir"] == str(wt)
        claims = d.maestro._claims(second["task_id"])
        assert claims["task_run_dir"] == str(wt) and claims["task_run_dir_kind"] == "worktree"
        branch = subprocess.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], text=True, capture_output=True).stdout.strip()
        assert branch == f"maestro/{second['task_id']}"
    finally:
        _stop(d, gate)


def test_followup_in_a_deleted_worktree_recreates_it(tmp_path, monkeypatch, binpath):
    import shutil

    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        gate.touch()
        d.wait(second["task_id"], timeout=60)
        shutil.rmtree(second["run_dir"])
        sub = d.bus.subscribe("output")
        try:
            d.followup(second["task_id"], "again")
            note = sub.wait(
                predicate=lambda e: e.task_id == second["task_id"] and "was created again" in (e.data.get("line") or ""),
                timeout=30,
            )
        finally:
            sub.close()
        final = d.wait(second["task_id"], timeout=60)
        assert note is not None
        assert final["status"]["state"] == "completed"
        assert Path(second["run_dir"]).is_dir()
    finally:
        _stop(d, gate)


def test_rename_branch_of_a_worktree_task_keeps_its_worktree(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        gate.touch()
        d.wait(second["task_id"], timeout=60)
        d.rename_branch(second["task_id"], "feat/renamed")
        d.followup(second["task_id"], "again")
        d.wait(second["task_id"], timeout=60)
        head = subprocess.run(["git", "-C", second["run_dir"], "rev-parse", "--abbrev-ref", "HEAD"], text=True, capture_output=True).stdout.strip()
        assert head == "feat/renamed"
    finally:
        _stop(d, gate)


def test_workspace_without_commit_fails_the_worktree_task(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = tmp_path / "empty"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True)
    d = _daemon(tmp_path, monkeypatch)
    try:
        d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        final = d.wait(second["task_id"], timeout=60)
        assert final["status"]["state"] == "failed"
        assert "has no commit yet" in final["metadata"]["error"]
    finally:
        _stop(d, gate)


def test_followup_in_a_broken_worktree_fails_with_the_reason(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        gate.touch()
        d.wait(second["task_id"], timeout=60)
        # The worktree directory exists, but git can no longer use it.
        (Path(second["run_dir"]) / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
        d.followup(second["task_id"], "again")
        final = d.wait(second["task_id"], timeout=60)
        assert final["status"]["state"] == "failed"
        assert "could not check out the task branch" in final["metadata"]["error"]
    finally:
        _stop(d, gate)

def test_cleanup_rules(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        first = d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        with pytest.raises(ValueError, match="is running"):
            d.cleanup_worktree(second["task_id"])
        gate.touch()
        d.wait(first["task_id"], timeout=60)
        d.wait(second["task_id"], timeout=60)
        # The fake agent left ran-here.txt uncommitted in the worktree.
        with pytest.raises(ValueError, match="uncommitted changes: ran-here.txt"):
            d.cleanup_worktree(second["task_id"])
        result = d.cleanup_worktree(second["task_id"], force=True)
        assert result["removed"] is True and not Path(second["run_dir"]).exists()
        again = d.cleanup_worktree(second["task_id"])
        assert again["removed"] is False and again["reason"] == "the worktree is already gone"
        assert d.maestro._claims(second["task_id"])["task_run_dir_removed"] == "true"
        in_place = d.cleanup_worktree(first["task_id"])
        assert in_place["removed"] is False and "ran in the workspace" in in_place["reason"]
        assert (ws / "ran-here.txt").exists()  # the user's checkout is never touched
    finally:
        _stop(d, gate)


def test_three_tasks_run_at_once_each_in_its_own_directory(tmp_path, monkeypatch, binpath):
    marker_dir = tmp_path / "running"
    marker_dir.mkdir()
    gate = tmp_path / "go"
    # Each agent records that it is running, then waits for the gate. If the
    # tasks ran one after another, the test would time out waiting for 3 markers.
    _fake_bin(binpath, "codex", f'cat > /dev/null\ntouch "{marker_dir}/$$"\nwhile [ ! -f "{gate}" ]; do sleep 0.05; done\necho "$PWD" > ran-here.txt\nexit 0')
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        started = [d.delegate(_doc(title=f"t{i}"), ws) for i in range(3)]
        deadline = time.monotonic() + 30
        while len(list(marker_dir.iterdir())) < 3:
            assert time.monotonic() < deadline, "the three tasks did not run at the same time"
            time.sleep(0.05)
        gate.touch()
        finals = [d.wait(s["task_id"], timeout=60) for s in started]
        assert [f["status"]["state"] for f in finals] == ["completed"] * 3
        dirs = [Path(s["run_dir"]) for s in started]
        assert dirs[0] == ws and len(set(dirs)) == 3
        for path in dirs:
            assert (path / "ran-here.txt").read_text().strip() == str(path)
        branches = {subprocess.run(["git", "-C", str(p), "rev-parse", "--abbrev-ref", "HEAD"], text=True, capture_output=True).stdout.strip() for p in dirs}
        assert len(branches) == 3
    finally:
        _stop(d, gate)


def test_max_parallel_one_never_creates_a_worktree(tmp_path, monkeypatch, binpath):
    from maestro.agents import AgentSpec

    # With max_parallel = 1 a task parked in the workspace holds it, and the
    # next task waits, exactly as before worktrees existed.
    _fake_bin(binpath, "asker", 'cat > /dev/null\necho \'{"question": "which db?"}\'\nexit 0')
    _fake_bin(binpath, "codex", "cat > /dev/null\nexit 0")
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch, "[defaults]\nmax_parallel = 1\n")
    d.registry.save(AgentSpec(name="asker", kind="generic", command="asker --go", output_format="jsonl"))
    try:
        parked = d.delegate(_doc(target_agent="asker"), ws)
        assert d.wait(parked["task_id"], timeout=30)["status"]["state"] == "input-required"
        second = d.delegate(_doc(), ws)
        assert second["queued"] is True
        assert "max_parallel is 1" in second["reason"]
        assert not (d.state_dir / "worktrees").exists()
        d.cancel(parked["task_id"])  # frees the workspace
        assert d.wait(second["task_id"], timeout=30)["status"]["state"] == "completed"
    finally:
        _stop(d, tmp_path / "go")
