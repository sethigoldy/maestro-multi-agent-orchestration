"""Custom task branch names at delegation time, and renaming a task's branch later."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from maestro import cli, mcp_server
from maestro.a2a import ERR_INVALID_PARAMS, ERR_TASK_NOT_FOUND, A2ADispatcher
from maestro.branches import branch_exists, rename_task_branch, validate_branch_name
from maestro.core import Maestro
from maestro.daemon import MaestroDaemon
from maestro.handoff import HandoffDoc, from_dict, from_legacy, from_toml, to_toml, validate_handoff


_GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ws), *args], text=True, capture_output=True, env=_GIT_ENV)


def _git_repo(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    _git(ws, "init", "-q")
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(ws, "add", ".")
    _git(ws, "commit", "-qm", "initial")
    return ws


def _current_branch(ws: Path) -> str:
    return _git(ws, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _fake_codex(binpath: Path, body: str = "cat > /dev/null\nexit 0") -> None:
    path = binpath / "codex"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _doc(**kw) -> HandoffDoc:
    base = dict(title="Do the thing", request="Implement it", verification="none", commit_policy="branch",
                target_agent="codex", explicit_target=True)
    base.update(kw)
    return HandoffDoc(**base)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.delenv("MAESTRO_DAEMON_URL", raising=False)
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    yield d
    for tid, rec in list(d._tasks.items()):
        if rec.get("state") not in ("completed", "failed", "canceled"):
            try:
                d.cancel(tid, reason="test teardown")
            except (KeyError, ValueError):
                pass
    d.stop()


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    return bp


def _finished_task(daemon, ws, **kw) -> str:
    started = daemon.delegate(_doc(**kw), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "completed", final
    return started["task_id"]


# ------------------------------------------------------------ name validation
def test_validate_branch_name_accepts_and_strips():
    assert validate_branch_name("feat/login-form") == "feat/login-form"
    assert validate_branch_name("  fix/bug-12  ") == "fix/bug-12"


@pytest.mark.parametrize("name", [
    None, 12, "", "   ", "-x", "@", "HEAD", "has space", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b",
    "a\x01b", "a\x7fb", "a..b", "a@{b", "/a", "a/", "a//b", "a.", ".a", "a/.b", "a.lock", "a/b.lock/c",
])
def test_validate_branch_name_rejects(name):
    with pytest.raises(ValueError):
        validate_branch_name(name)


# ------------------------------------------------------------ handoff document
def test_handoff_branch_round_trips_through_dict_and_toml():
    doc = validate_handoff(_doc(branch=" feat/x "))
    assert doc.branch == "feat/x"
    assert doc.to_dict()["expectations"]["branch"] == "feat/x"
    assert from_dict(doc.to_dict()).branch == "feat/x"
    assert from_toml(to_toml(doc)).branch == "feat/x"


def test_handoff_without_branch_keeps_default_and_omits_it_from_toml():
    doc = validate_handoff(_doc())
    assert doc.branch is None
    assert "branch =" not in to_toml(doc)
    data = doc.to_dict()
    data["expectations"]["branch"] = ""
    assert from_dict(data).branch is None


def test_handoff_rejects_bad_branch_and_no_commit_branch():
    with pytest.raises(ValueError, match="Invalid branch name"):
        validate_handoff(_doc(branch="bad name"))
    with pytest.raises(ValueError, match="no-commit"):
        validate_handoff(_doc(branch="feat/x", commit_policy="no-commit"))


def test_legacy_handoff_accepts_branch():
    assert from_legacy({"title": "T", "request": "R", "branch": "feat/legacy"}).branch == "feat/legacy"
    assert from_legacy({"title": "T", "request": "R"}).branch is None


# ------------------------------------------------------------ delegation
def test_delegate_creates_the_requested_branch(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws, branch="feat/login-form")
    assert _current_branch(ws) == "feat/login-form"
    assert not branch_exists(ws, f"maestro/{task_id}")
    assert daemon.maestro._claims(task_id)["task_branch"] == "feat/login-form"
    assert daemon.status_a2a(task_id)["metadata"]["branch"] == "feat/login-form"
    assert daemon.maestro.status(task_id)["branch"] == "feat/login-form"


def test_followup_stays_on_the_requested_branch(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws, branch="feat/keep")
    _git(ws, "checkout", "-q", "-")  # leave the branch; the follow-up must check it out again
    daemon.followup(task_id, "one more change")
    assert daemon.wait(task_id, timeout=60)["status"]["state"] == "completed"
    assert _current_branch(ws) == "feat/keep"
    assert not branch_exists(ws, f"maestro/{task_id}")
    assert daemon._tasks[task_id]["doc"]["expectations"]["branch"] == "feat/keep"


def test_delegate_refuses_a_branch_that_already_exists(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    _git(ws, "branch", "feat/taken")
    with pytest.raises(ValueError, match="already exists"):
        daemon.delegate(_doc(branch="feat/taken"), ws)


def test_prepare_branch_requested_name_that_cannot_be_created_fails_the_turn(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    _git(ws, "branch", "feat/raced")  # created after delegate's check, before the turn
    with pytest.raises(RuntimeError, match="feat/raced"):
        daemon._prepare_branch(ws, "task-z", "branch", requested="feat/raced")
    # A later turn that already recorded the branch checks it out instead.
    assert daemon._prepare_branch(ws, "task-z", "branch", requested="feat/raced", recorded="feat/raced") == "feat/raced"
    assert _current_branch(ws) == "feat/raced"


def test_turn_fails_when_the_requested_branch_cannot_be_created(daemon, tmp_path, binpath, monkeypatch):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    import maestro.daemon as dm

    monkeypatch.setattr(dm, "branch_exists", lambda workspace, name: False)  # let delegate through
    _git(ws, "branch", "feat/raced")
    started = daemon.delegate(_doc(branch="feat/raced"), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "failed"
    assert "feat/raced" in final["metadata"]["error"]


# ------------------------------------------------------------ renaming
def test_rename_branch_renames_git_branch_and_every_view(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    old = f"maestro/{task_id}"
    assert branch_exists(ws, old)

    result = daemon.rename_branch(task_id, "feat/renamed")
    assert result == {"task_id": task_id, "old_branch": old, "branch": "feat/renamed", "git_renamed": True}
    assert branch_exists(ws, "feat/renamed") and not branch_exists(ws, old)
    assert daemon._tasks[task_id]["branch"] == "feat/renamed"
    assert daemon.status_a2a(task_id)["metadata"]["branch"] == "feat/renamed"
    listed = [t for t in daemon.maestro.list_tasks() if t["task_id"] == task_id]
    assert listed[0]["branch"] == "feat/renamed"
    claims = daemon.maestro._claims(task_id)
    assert json.loads(claims["task_runtime"])["branch"] == "feat/renamed"
    assert "branch feat/renamed" in claims["task_knowledge"]
    events = [e for e in daemon.bus.history(task_id) if e.type == "branch"]
    assert events and events[-1].data == {"old_branch": old, "branch": "feat/renamed"}

    # A follow-up works on the renamed branch and does not recreate the old one.
    daemon.followup(task_id, "more")
    assert daemon.wait(task_id, timeout=60)["status"]["state"] == "completed"
    assert _current_branch(ws) == "feat/renamed"
    assert not branch_exists(ws, old)


def test_rename_branch_records_a_rename_already_done_by_hand(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    _git(ws, "branch", "-m", f"maestro/{task_id}", "feat/by-hand")
    result = daemon.rename_branch(task_id, "feat/by-hand")
    assert result["git_renamed"] is False and result["branch"] == "feat/by-hand"
    assert daemon.maestro.status(task_id)["branch"] == "feat/by-hand"


def test_rename_branch_follows_a_chain_of_hand_renames(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    _git(ws, "branch", "-m", f"maestro/{task_id}", "feat/first")
    _git(ws, "branch", "-m", "feat/first", "feat/second")
    assert daemon.rename_branch(task_id, "feat/second")["git_renamed"] is False
    assert daemon.maestro.status(task_id)["branch"] == "feat/second"


def test_rename_branch_refuses_an_unrelated_existing_branch(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    old = f"maestro/{task_id}"
    _git(ws, "checkout", "-q", "-b", "release/2.0")
    _git(ws, "branch", "-D", old)  # the task branch was merged and deleted
    with pytest.raises(ValueError, match="no record that 'release/2.0' was renamed from it"):
        daemon.rename_branch(task_id, "release/2.0")
    assert daemon.maestro._claims(task_id)["task_branch"] == old
    assert daemon._tasks[task_id]["branch"] == old


def test_followup_fails_instead_of_working_on_the_wrong_branch(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws, branch="feat/login")
    (ws / "README.md").write_text("# changed on the task branch\n", encoding="utf-8")
    _git(ws, "commit", "-qam", "task work")
    _git(ws, "checkout", "-q", "-")
    main = _current_branch(ws)
    (ws / "README.md").write_text("# uncommitted edit that blocks the checkout\n", encoding="utf-8")
    attempts_before = len(daemon._tasks[task_id]["attempts"])

    daemon.followup(task_id, "one more change")
    final = daemon.wait(task_id, timeout=60)
    assert final["status"]["state"] == "failed"
    assert "could not check out the task branch 'feat/login'" in final["metadata"]["error"]
    assert _current_branch(ws) == main
    assert len(daemon._tasks[task_id]["attempts"]) == attempts_before  # no agent ran on the wrong branch


def test_rename_branch_same_name_is_a_no_op(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws, branch="feat/same")
    result = daemon.rename_branch(task_id, "feat/same")
    assert result == {"task_id": task_id, "old_branch": "feat/same", "branch": "feat/same", "git_renamed": False}


def test_rename_branch_refusals(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    old = f"maestro/{task_id}"

    _git(ws, "branch", "feat/taken")
    with pytest.raises(ValueError, match="already exists"):
        daemon.rename_branch(task_id, "feat/taken")

    _git(ws, "branch", "feat/dir/child")
    with pytest.raises(ValueError, match="git branch -m"):  # 'feat/dir' collides with feat/dir/child
        daemon.rename_branch(task_id, "feat/dir")

    with pytest.raises(ValueError, match="Invalid branch name"):
        daemon.rename_branch(task_id, "bad name")

    _git(ws, "checkout", "-q", "feat/taken")
    _git(ws, "branch", "-D", old)
    with pytest.raises(ValueError, match="Neither"):
        daemon.rename_branch(task_id, "feat/nowhere")
    assert daemon.maestro._claims(task_id)["task_branch"] == old  # nothing recorded on refusal

    with pytest.raises(KeyError):
        daemon.rename_branch("task-20250101-000000-abcdef", "feat/x")


def test_rename_branch_refuses_task_without_branch(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws, commit_policy="no-commit")
    with pytest.raises(ValueError, match="no branch yet"):
        daemon.rename_branch(task_id, "feat/x")


def test_rename_branch_refuses_running_task(daemon, tmp_path, binpath):
    _fake_codex(binpath, "cat > /dev/null\nsleep 5")
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    deadline = time.monotonic() + 10
    while daemon._tasks[started["task_id"]]["state"] != "working" and time.monotonic() < deadline:
        time.sleep(0.02)
    with pytest.raises(ValueError, match="running"):
        daemon.rename_branch(started["task_id"], "feat/x")


def test_rename_branch_after_daemon_restart(tmp_path, binpath, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    first = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        task_id = _finished_task(first, ws)
    finally:
        first.stop()
    second = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        second._tasks.pop(task_id, None)  # force the durable path
        assert second.rename_branch(task_id, "feat/after-restart")["git_renamed"] is True
        assert second.status_a2a(task_id)["metadata"]["branch"] == "feat/after-restart"
    finally:
        second.stop()


def test_rename_task_branch_tolerates_missing_or_bad_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    ws = _git_repo(tmp_path)
    _git(ws, "branch", "maestro/task-a")
    (tmp_path / "home").mkdir()
    m = Maestro(tmp_path / "home")
    try:
        for tid, runtime in (("task-a", None), ("task-b", "not json"), ("task-c", "[]")):
            m._write_claim(tid, "task_workspace", str(ws))
            m._write_claim(tid, "task_branch", f"maestro/{tid}")
            if runtime is not None:
                m._write_claim(tid, "task_runtime", runtime)
        assert rename_task_branch(m, "task-a", "feat/a")["git_renamed"] is True
        assert "task_runtime" not in m._claims("task-a")
        for tid in ("task-b", "task-c"):  # renamed by hand, so the reflog records it
            _git(ws, "branch", f"maestro/{tid}")
            _git(ws, "branch", "-m", f"maestro/{tid}", f"feat/{tid[-1]}")
        assert rename_task_branch(m, "task-b", "feat/b")["git_renamed"] is False
        assert m._claims("task-b")["task_runtime"] == "not json"  # left as it was
        assert rename_task_branch(m, "task-c", "feat/c")["git_renamed"] is False
        with pytest.raises(KeyError):
            rename_task_branch(m, "task-unknown", "feat/d")
    finally:
        m.close()


# ------------------------------------------------------------ A2A
class _RenameDaemon:
    def resolve(self, ref):
        if not str(ref).startswith("task-"):
            raise KeyError(f"Unknown task reference {ref!r}")
        return ref

    def rename_branch(self, task_id, branch):
        if branch == "taken":
            raise ValueError("already exists")
        return {"task_id": task_id, "old_branch": "maestro/x", "branch": branch, "git_renamed": True}

    def status_a2a(self, task_id):
        return {"kind": "task", "id": task_id, "metadata": {"branch": "feat/x"}}


def _rpc(method, params):
    return A2ADispatcher(_RenameDaemon()).handle({"jsonrpc": "2.0", "id": 7, "method": method, "params": params})


def test_a2a_rename_branch():
    ok = _rpc("tasks/renameBranch", {"id": "task-1", "branch": "feat/x"})
    assert ok["result"]["rename"]["branch"] == "feat/x"
    assert ok["result"]["task"]["metadata"]["branch"] == "feat/x"
    assert _rpc("tasks/renameBranch", {"branch": "feat/x"})["error"]["code"] == ERR_INVALID_PARAMS
    assert _rpc("tasks/renameBranch", {"id": "task-1", "branch": " "})["error"]["code"] == ERR_INVALID_PARAMS
    assert _rpc("tasks/renameBranch", {"id": "nope", "branch": "feat/x"})["error"]["code"] == ERR_TASK_NOT_FOUND
    taken = _rpc("tasks/renameBranch", {"id": "task-1", "branch": "taken"})
    assert taken["error"]["code"] == ERR_INVALID_PARAMS and "already exists" in taken["error"]["message"]


# ------------------------------------------------------------ MCP
def test_mcp_delegate_branch_argument(monkeypatch, tmp_path):
    handoff = tmp_path / "h.toml"
    handoff.write_text('[handoff]\ntitle = "T"\nrequest = "R"\n[expectations]\nbranch = "feat/from-file"\n', encoding="utf-8")
    seen = []

    class FakeDaemon:
        def delegate(self, doc, workspace):
            seen.append(doc.branch)
            return {"task_id": "task-1", "queued": False}

        def wait(self, task_id, timeout=None):
            return {"status": {"state": "completed"}}

    monkeypatch.setattr(mcp_server, "get_daemon", lambda: FakeDaemon())
    mcp_server.delegate("w", str(handoff))
    mcp_server.delegate("w", str(handoff), branch="feat/from-arg")
    assert seen == ["feat/from-file", "feat/from-arg"]
    assert "Invalid branch name" in json.loads(mcp_server.delegate("w", str(handoff), branch="bad name"))["error"]


def test_mcp_rename_task_branch(monkeypatch):
    class FakeDaemon(_RenameDaemon):
        pass

    monkeypatch.setattr(mcp_server, "get_daemon", lambda: FakeDaemon())
    assert json.loads(mcp_server.rename_task_branch("w", "task-1", "feat/x"))["branch"] == "feat/x"
    assert "error" in json.loads(mcp_server.rename_task_branch("w", "nope", "feat/x"))
    assert "already exists" in json.loads(mcp_server.rename_task_branch("w", "task-1", "taken"))["error"]


# ------------------------------------------------------------ CLI
def _capture_post(monkeypatch, result):
    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://127.0.0.1:9")
    captured = {}

    def fake_post(url, method, payload, token=None):
        captured["method"] = method
        captured["payload"] = payload
        return result

    monkeypatch.setattr(cli, "_post_jsonrpc", fake_post)
    monkeypatch.setattr(cli, "_stream_task", lambda url, task_id, token=None: 0)
    return captured


def test_cli_delegate_branch_flag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    captured = _capture_post(monkeypatch, {"task": {"id": "task-x"}})
    assert cli.main(["delegate", "--title", "T", "--request", "R", "--target", "codex", "--branch", "feat/cli"]) == 0
    assert captured["payload"]["message"]["parts"][0]["data"]["expectations"]["branch"] == "feat/cli"

    handoff = tmp_path / "h.toml"
    handoff.write_text('[handoff]\ntitle = "T"\nrequest = "R"\n[expectations]\nbranch = "feat/file"\n', encoding="utf-8")
    assert cli.main(["delegate", "--file", str(handoff), "--branch", "feat/override"]) == 0
    assert captured["payload"]["message"]["parts"][0]["data"]["expectations"]["branch"] == "feat/override"
    assert cli.main(["delegate", "--file", str(handoff)]) == 0
    assert captured["payload"]["message"]["parts"][0]["data"]["expectations"]["branch"] == "feat/file"


def test_cli_delegate_rejects_bad_branch(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    _capture_post(monkeypatch, {"task": {"id": "task-x"}})
    assert cli.main(["delegate", "--title", "T", "--request", "R", "--branch", "bad name"]) == 2
    assert "Invalid branch name" in capsys.readouterr().err


@pytest.mark.parametrize("rename, expected", [
    ({"task_id": "task-1", "old_branch": "maestro/task-1", "branch": "feat/x", "git_renamed": True}, "Renamed branch maestro/task-1 -> feat/x"),
    ({"task_id": "task-1", "old_branch": "feat/x", "branch": "feat/x", "git_renamed": False}, "already on branch feat/x"),
    ({"task_id": "task-1", "old_branch": "maestro/task-1", "branch": "feat/x", "git_renamed": False}, "Recorded branch feat/x"),
])
def test_cli_task_rename_branch_through_daemon(monkeypatch, tmp_path, capsys, rename, expected):
    monkeypatch.chdir(tmp_path)
    captured = _capture_post(monkeypatch, {"rename": rename, "task": {}})
    assert cli.main(["task", "rename-branch", "1", "feat/x"]) == 0
    assert captured == {"method": "tasks/renameBranch", "payload": {"id": "1", "branch": "feat/x"}}
    assert expected in capsys.readouterr().out


def test_cli_task_rename_branch_without_daemon(daemon, tmp_path, binpath, monkeypatch, capsys):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    number = daemon.maestro.status(task_id)["task_number"]
    monkeypatch.chdir(ws)
    assert cli.main(["task", "rename-branch", str(number), "feat/local"]) == 0
    assert "Renamed branch" in capsys.readouterr().out
    assert branch_exists(ws, "feat/local")
    assert cli.main(["task", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [t["branch"] for t in listed if t["task_id"] == task_id] == ["feat/local"]

    assert cli.main(["task", "rename-branch", "999", "feat/none"]) == 2
    assert "Unknown task number" in capsys.readouterr().err


# ------------------------------------------------------------ terminal dashboard
def test_tui_applies_branch_event():
    from maestro.tui import _State

    state = _State()
    state.apply_event("task-1", "state", {"state": "completed"})
    state.apply_event("task-1", "branch", {"old_branch": "maestro/task-1", "branch": "feat/x"})
    assert state.by_id["task-1"]["branch"] == "feat/x"
    state.apply_event("task-1", "branch", {})
    assert state.by_id["task-1"]["branch"] == "feat/x"
