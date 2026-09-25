"""Reviewing a task's work from the console: its diff and its verification report."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import urllib.error
import urllib.request
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


def _repo(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], env=_ENV, check=True)
    (ws / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "."], env=_ENV, check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "initial"], env=_ENV, check=True)
    return ws


# The agent commits one change, leaves another uncommitted, and adds a new file.
_AGENT = (
    'cat > /dev/null\n'
    'echo "y = 2" >> app.py && git -c user.name=a -c user.email=a@a commit -qam "agent commit"\n'
    'echo "z = 3" >> app.py\n'
    'echo "new" > notes.txt\n'
    'exit 0'
)


def _doc(**kw) -> HandoffDoc:
    base = dict(title="T", request="R", verification="none", commit_policy="branch", target_agent="codex", explicit_target=True)
    base.update(kw)
    return HandoffDoc(**base)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    yield d
    d.stop()


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    _fake_bin(bp, "codex", _AGENT)
    return bp


def _get(daemon: MaestroDaemon, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{daemon.port}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_diff_covers_commits_uncommitted_changes_and_new_files(daemon, tmp_path, binpath):
    ws = _repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    daemon.wait(started["task_id"], timeout=60)
    status, diff = _get(daemon, f"/tasks/{started['task_id']}/diff")
    assert status == 200 and diff["available"] is True
    assert diff["run_dir"] == str(ws)
    assert diff["files"] == [{"path": "app.py", "added": 2, "removed": 0}]
    assert diff["untracked"] == ["notes.txt"]
    assert "+y = 2" in diff["diff"] and "+z = 3" in diff["diff"]  # the agent's commit and the uncommitted line
    assert diff["truncated"] is False


def test_a_follow_up_keeps_the_start_commit(daemon, tmp_path, binpath):
    ws = _repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    daemon.wait(started["task_id"], timeout=60)
    first = daemon.task_diff(started["task_id"])["base"]
    daemon.followup(started["task_id"], "again")
    daemon.wait(started["task_id"], timeout=60)
    assert daemon.task_diff(started["task_id"])["base"] == first


def test_a_large_diff_is_cut_and_marked(daemon, tmp_path, binpath, monkeypatch):
    import maestro.daemon as dm

    monkeypatch.setattr(dm, "DIFF_LIMIT_BYTES", 40)
    ws = _repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    daemon.wait(started["task_id"], timeout=60)
    diff = daemon.task_diff(started["task_id"])
    assert diff["truncated"] is True and len(diff["diff"].encode()) <= 40


def test_the_diff_of_a_missing_run_dir_says_why(daemon, tmp_path, binpath):
    import shutil

    ws = _repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    daemon.wait(started["task_id"], timeout=60)
    shutil.rmtree(ws)
    diff = daemon.task_diff(started["task_id"])
    assert diff["available"] is False and "does not exist" in diff["reason"]


def test_the_verification_report_and_unknown_tasks(daemon, tmp_path, binpath):
    ws = _repo(tmp_path)
    started = daemon.delegate(_doc(verification="command", verification_command="true"), ws)
    daemon.wait(started["task_id"], timeout=60)
    status, body = _get(daemon, f"/tasks/{started['task_id']}/verification")
    assert status == 200 and "verification command: true" in body["report"]
    (tmp_path / "other").mkdir()
    quiet = daemon.delegate(_doc(), _repo(tmp_path / "other"))
    daemon.wait(quiet["task_id"], timeout=60)
    assert _get(daemon, f"/tasks/{quiet['task_id']}/verification") == (200, {"report": None})
    assert _get(daemon, "/tasks/task-19700101-000000-nothing/diff")[0] == 404
    assert _get(daemon, "/tasks/task-19700101-000000-nothing/verification")[0] == 404


def test_earlier_output_is_served_from_the_agent_logs(daemon, tmp_path, binpath):
    import maestro.daemon as dm

    ws = _repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    daemon.wait(started["task_id"], timeout=60)
    log = daemon.state_dir / "tasks" / started["task_id"] / "codex-extra.log"
    log.write_text("".join(f"line {i}\n" for i in range(3000)), encoding="utf-8")
    status, body = _get(daemon, f"/tasks/{started['task_id']}/output")
    assert status == 200
    assert len(body["lines"]) == dm.OUTPUT_TAIL_LINES and body["lines"][-1] == "line 2999"
    assert _get(daemon, "/tasks/task-19700101-000000-nothing/output")[0] == 404


def test_unknown_tasks_are_refused_by_every_review_read(daemon):
    for read in (daemon.task_diff, daemon.task_output, daemon.verification_report):
        with pytest.raises(KeyError, match="Unknown task reference"):
            read("task-19700101-000000-nothing")


def test_a_run_dir_that_git_cannot_compare_says_why(daemon, tmp_path, binpath):
    import shutil

    ws = _repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    daemon.wait(started["task_id"], timeout=60)
    shutil.rmtree(ws / ".git")  # the directory is still there, but it is no longer a repository
    diff = daemon.task_diff(started["task_id"])
    assert diff["available"] is False and "git could not compare" in diff["reason"]
