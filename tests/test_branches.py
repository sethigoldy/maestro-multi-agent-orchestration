"""Custom task branch names at delegation time, and renaming a task's branch later."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from maestro import cli, mcp_server
from maestro.a2a import ERR_INVALID_PARAMS, ERR_TASK_NOT_FOUND, A2ADispatcher
from maestro.agents import AgentSpec
from maestro.branches import (
    branch_exists,
    branch_name_clash,
    find_renamed_branches,
    rename_task_branch,
    validate_branch_name,
)
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

    monkeypatch.setattr(dm, "branch_name_clash", lambda workspace, name: None)  # let delegate through
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
    with pytest.raises(ValueError, match="cannot have both 'feat/dir/child' and 'feat/dir'"):
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
    ({"task_id": "task-1", "old_branch": "feat/taken", "branch": "feat/x", "git_renamed": False, "pending": True},
     "has no branch yet; its next turn will create feat/x (instead of feat/taken)"),
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


# ------------------------------------------------------------ remote agents
def test_forwarded_handoff_leaves_out_the_branch():
    doc = validate_handoff(_doc(branch="feat/local-only"))
    settings = MaestroDaemon._turn_settings(AgentSpec(name="remote-node", kind="a2a_remote"), doc)
    assert settings["maestro_handoff"]["expectations"]["branch"] is None
    assert settings["maestro_handoff"]["handoff"]["title"] == "Do the thing"
    assert doc.branch == "feat/local-only"  # the local turn still uses the name


def test_remote_task_with_a_named_branch_runs_every_turn(tmp_path, binpath, monkeypatch):
    """A same-machine remote daemon: the local daemon creates the named branch on
    turn 1, and the remote daemon gets a new message/send on every turn. Neither
    send may be refused because the branch already exists."""
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    remote = MaestroDaemon(state_dir=tmp_path / "remote-home", start_http=True, port=0, max_retries=0, backoff_s=0)
    local = MaestroDaemon(state_dir=tmp_path / "local-home", start_http=False, max_retries=0, backoff_s=0)
    try:
        remote.registry.save(AgentSpec(name="codex", kind="codex"))
        local.registry.save(AgentSpec(name="remote-node", kind="a2a_remote", command=f"http://127.0.0.1:{remote.port}"))
        started = local.delegate(_doc(target_agent="remote-node", branch="feat/remote"), ws)
        final = local.wait(started["task_id"], timeout=60)
        assert final["status"]["state"] == "completed", final["metadata"].get("error")
        local.followup(started["task_id"], "one more change")
        final = local.wait(started["task_id"], timeout=60)
        assert final["status"]["state"] == "completed", final["metadata"].get("error")
        assert local._tasks[started["task_id"]]["branch"] == "feat/remote"
        assert branch_exists(ws, "feat/remote")
    finally:
        local.stop()
        remote.stop()


# ------------------------------------------------------------ branch names that clash as folders
def test_branch_name_clash_detects_folder_conflicts(tmp_path):
    ws = _git_repo(tmp_path)
    _git(ws, "branch", "feat")
    _git(ws, "branch", "fix/one")
    assert branch_name_clash(ws, "feat") == "a branch named 'feat' already exists"
    assert "cannot have both 'feat' and 'feat/login'" in branch_name_clash(ws, "feat/login")
    assert "cannot have both 'fix/one' and 'fix'" in branch_name_clash(ws, "fix")
    assert branch_name_clash(ws, "fix/two") is None
    assert branch_name_clash(ws, "feature") is None  # a shared prefix without '/' is fine
    assert branch_name_clash(ws, "feat/login", ignore="feat") is None


def test_delegate_refuses_a_branch_that_clashes_as_a_folder(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    _git(ws, "branch", "feat")
    _git(ws, "branch", "fix/x")
    with pytest.raises(ValueError, match="cannot have both 'feat' and 'feat/login'.*pick a new name"):
        daemon.delegate(_doc(branch="feat/login"), ws)
    with pytest.raises(ValueError, match="cannot have both 'fix/x' and 'fix'"):
        daemon.delegate(_doc(branch="fix"), ws)
    assert daemon._tasks == {}  # refused before any task was made


def test_rename_branch_refuses_a_name_inside_an_existing_branch(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    _git(ws, "branch", "feat")
    with pytest.raises(ValueError, match="cannot have both 'feat' and 'feat/sub'"):
        daemon.rename_branch(task_id, "feat/sub")
    assert branch_exists(ws, f"maestro/{task_id}")


def test_rename_branch_may_move_a_branch_into_a_folder_of_its_own_name(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws, branch="feat")
    assert daemon.rename_branch(task_id, "feat/done")["git_renamed"] is True
    assert branch_exists(ws, "feat/done") and not branch_exists(ws, "feat")


def test_rename_branch_reports_a_git_failure(daemon, tmp_path, binpath, monkeypatch):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    import maestro.branches as br

    real_run = br.subprocess.run

    def _run(cmd, *args, **kwargs):
        if "-m" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: forced")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(br.subprocess, "run", _run)
    with pytest.raises(ValueError, match="git branch -m .* failed: fatal: forced"):
        daemon.rename_branch(task_id, "feat/x")


# ------------------------------------------------------------ a requested branch the first turn could not create
def _task_whose_first_turn_could_not_create_its_branch(daemon, ws, monkeypatch) -> str:
    import maestro.daemon as dm

    monkeypatch.setattr(dm, "branch_name_clash", lambda workspace, name: None)  # as if created after delegation
    _git(ws, "branch", "feat/raced")
    started = daemon.delegate(_doc(branch="feat/raced"), ws)
    final = daemon.wait(started["task_id"], timeout=60)
    assert final["status"]["state"] == "failed"
    return started["task_id"]


def test_failed_first_turn_names_the_command_that_fixes_it(daemon, tmp_path, binpath, monkeypatch):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _task_whose_first_turn_could_not_create_its_branch(daemon, ws, monkeypatch)
    number = daemon.maestro.status(task_id)["task_number"]
    error = daemon._tasks[task_id]["error"]
    assert f"'maestro task rename-branch {number} <new-name>'" in error
    assert f"'maestro task continue {number} --request <instruction> --branch <new-name>'" in error


def test_rename_branch_on_a_task_without_a_branch_changes_the_name_the_next_turn_creates(daemon, tmp_path, binpath, monkeypatch):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _task_whose_first_turn_could_not_create_its_branch(daemon, ws, monkeypatch)
    events_before = len([e for e in daemon.bus.history(task_id) if e.type == "branch"])

    result = daemon.rename_branch(task_id, "feat/other")
    assert result == {"task_id": task_id, "old_branch": "feat/raced", "branch": "feat/other", "git_renamed": False, "pending": True}
    assert daemon._tasks[task_id]["branch"] is None
    assert daemon._tasks[task_id]["doc"]["expectations"]["branch"] == "feat/other"
    assert json.loads(daemon.maestro._claims(task_id)["task_runtime"])["doc"]["expectations"]["branch"] == "feat/other"
    assert len([e for e in daemon.bus.history(task_id) if e.type == "branch"]) == events_before
    assert not branch_exists(ws, "feat/other")  # created by the next turn, not now

    daemon.followup(task_id, "try again")
    assert daemon.wait(task_id, timeout=60)["status"]["state"] == "completed"
    assert _current_branch(ws) == "feat/other"
    assert daemon.maestro.status(task_id)["branch"] == "feat/other"


def test_followup_with_a_branch_recovers_a_failed_first_turn(daemon, tmp_path, binpath, monkeypatch):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _task_whose_first_turn_could_not_create_its_branch(daemon, ws, monkeypatch)
    daemon.followup(task_id, "try again", branch="feat/second-try")
    assert daemon.wait(task_id, timeout=60)["status"]["state"] == "completed"
    assert _current_branch(ws) == "feat/second-try"
    assert daemon._tasks[task_id]["branch"] == "feat/second-try"


def test_followup_with_a_branch_renames_an_existing_task_branch(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    daemon.followup(task_id, "more", branch="feat/renamed-on-followup")
    assert daemon.wait(task_id, timeout=60)["status"]["state"] == "completed"
    assert _current_branch(ws) == "feat/renamed-on-followup"
    assert not branch_exists(ws, f"maestro/{task_id}")


def test_followup_with_a_bad_branch_is_refused_before_anything_changes(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    _git(ws, "branch", "feat/taken")
    with pytest.raises(ValueError, match="already exists"):
        daemon.followup(task_id, "more", branch="feat/taken")
    assert daemon._tasks[task_id]["state"] == "completed"
    assert daemon._tasks[task_id]["branch"] == f"maestro/{task_id}"


def test_pending_rename_edge_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    ws = _git_repo(tmp_path)
    _git(ws, "branch", "base")
    (tmp_path / "home").mkdir()
    m = Maestro(tmp_path / "home")
    try:
        def runtime(**expectations):
            return json.dumps({"doc": {"expectations": {"commit_policy": "branch", **expectations}}})

        m._write_claim("task-a", "task_workspace", str(ws))
        with pytest.raises(ValueError, match="its handoff was not recorded"):
            rename_task_branch(m, "task-a", "feat/a")

        m._write_claim("task-b", "task_workspace", str(ws))
        m._write_claim("task-b", "task_runtime", runtime())  # default name, never created
        result = rename_task_branch(m, "task-b", "feat/b")
        assert result["pending"] is True and result["old_branch"] == "maestro/task-b"
        assert json.loads(m._claims("task-b")["task_runtime"])["doc"]["expectations"]["branch"] == "feat/b"
        assert rename_task_branch(m, "task-b", "feat/b")["old_branch"] == "feat/b"  # same name: nothing to do

        with pytest.raises(ValueError, match="Cannot use 'base/c' as the branch for task task-b: .*cannot have both"):
            rename_task_branch(m, "task-b", "base/c")
    finally:
        m.close()


def test_cli_task_continue_branch_flag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    captured = _capture_post(monkeypatch, {"task": {"id": "task-1"}})
    assert cli.main(["task", "continue", "1", "--request", "again", "--branch", "feat/new", "--no-wait"]) == 0
    assert captured["payload"] == {"id": "1", "instruction": "again", "context_mode": "reuse", "branch": "feat/new"}


class _FollowupDaemon(_RenameDaemon):
    def __init__(self):
        self.calls = []

    def followup(self, task_id, instruction, context_mode="reuse", branch=None):
        self.calls.append((task_id, branch))
        return {"task_id": task_id, "state": "submitted"}

    def wait(self, task_id, timeout=None):
        return {"status": {"state": "completed"}, "metadata": {}}


def test_a2a_followup_branch_param():
    fake = _FollowupDaemon()
    dispatcher = A2ADispatcher(fake)

    def call(params):
        return dispatcher.handle({"jsonrpc": "2.0", "id": 1, "method": "tasks/followup", "params": params})

    assert "result" in call({"id": "task-1", "instruction": "go", "branch": "feat/x"})
    assert "result" in call({"id": "task-1", "instruction": "go"})
    assert fake.calls == [("task-1", "feat/x"), ("task-1", None)]
    for bad in ("  ", 7):
        assert call({"id": "task-1", "instruction": "go", "branch": bad})["error"]["code"] == ERR_INVALID_PARAMS


def test_mcp_followup_branch_argument(monkeypatch):
    fake = _FollowupDaemon()
    monkeypatch.setattr(mcp_server, "get_daemon", lambda: fake)
    mcp_server.followup("w", "task-1", "go", branch=" feat/x ")
    mcp_server.followup("w", "task-1", "go")
    assert fake.calls == [("task-1", "feat/x"), ("task-1", None)]


# ------------------------------------------------------------ renames racing a turn start
def test_followup_waits_for_a_rename_in_progress(daemon, tmp_path, binpath, monkeypatch):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    old = f"maestro/{task_id}"
    import maestro.daemon as dm

    real_rename = dm.rename_task_branch
    seen = {}

    def _slow_rename(maestro, tid, new_branch):
        # A follow-up arrives while the rename is under way. It must wait until
        # the rename is finished instead of starting a turn on the old name.
        follower = threading.Thread(target=daemon.followup, args=(task_id, "more"), daemon=True)
        follower.start()
        follower.join(0.5)
        seen["follower_waiting"] = follower.is_alive()
        seen["state_during_rename"] = daemon._tasks[task_id]["state"]
        seen["follower"] = follower
        return real_rename(maestro, tid, new_branch)

    monkeypatch.setattr(dm, "rename_task_branch", _slow_rename)
    daemon.rename_branch(task_id, "feat/renamed")
    seen["follower"].join(10)
    assert seen["follower_waiting"] is True
    assert seen["state_during_rename"] == "completed"
    assert daemon.wait(task_id, timeout=60)["status"]["state"] == "completed"
    assert _current_branch(ws) == "feat/renamed"
    assert not branch_exists(ws, old)


def test_rename_is_refused_while_an_answered_turn_is_starting(daemon, tmp_path, binpath, monkeypatch):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(sensitive=True), ws)
    task_id = started["task_id"]
    assert started["state"] == "input-required"
    gate = threading.Event()
    real_run_task = daemon._run_task

    def _held_run_task(*args):
        gate.wait(10)  # the answered turn's thread has not reached "working" yet
        real_run_task(*args)

    monkeypatch.setattr(daemon, "_run_task", _held_run_task)
    daemon.answer_question(task_id, "approved")
    try:
        with pytest.raises(ValueError, match="running"):
            daemon.rename_branch(task_id, "feat/x")
    finally:
        gate.set()
    deadline = time.monotonic() + 10
    while task_id in daemon._turn_starting and time.monotonic() < deadline:
        time.sleep(0.02)
    assert task_id not in daemon._turn_starting
    assert daemon.wait(task_id, timeout=60)["status"]["state"] == "completed"
    assert daemon._tasks[task_id]["branch"] == f"maestro/{task_id}"


# ------------------------------------------------------------ a recorded branch that is gone from git
def test_followup_adopts_a_branch_renamed_by_hand(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    old = f"maestro/{task_id}"
    _git(ws, "branch", "-m", old, "feat/by-hand")
    daemon.followup(task_id, "more")
    assert daemon.wait(task_id, timeout=60)["status"]["state"] == "completed"
    assert _current_branch(ws) == "feat/by-hand"
    assert not branch_exists(ws, old)  # not recreated
    assert daemon._tasks[task_id]["branch"] == "feat/by-hand"
    assert daemon.maestro._claims(task_id)["task_branch"] == "feat/by-hand"
    events = [e for e in daemon.bus.history(task_id) if e.type == "branch"]
    assert events and events[-1].data == {"old_branch": old, "branch": "feat/by-hand"}


def test_followup_fails_when_the_task_branch_was_deleted(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    old = f"maestro/{task_id}"
    _git(ws, "checkout", "-q", "-")
    _git(ws, "branch", "-D", old)
    attempts_before = len(daemon._tasks[task_id]["attempts"])

    daemon.followup(task_id, "more")
    final = daemon.wait(task_id, timeout=60)
    assert final["status"]["state"] == "failed"
    error = final["metadata"]["error"]
    assert f"the task branch {old!r} no longer exists, and git has no record that it was renamed" in error
    assert f"'git branch {old} <commit>'" in error
    assert not branch_exists(ws, old)  # no fresh branch in its place
    assert len(daemon._tasks[task_id]["attempts"]) == attempts_before


def test_followup_fails_when_a_renamed_branch_was_copied(daemon, tmp_path, binpath):
    _fake_codex(binpath)
    ws = _git_repo(tmp_path)
    task_id = _finished_task(daemon, ws)
    _git(ws, "branch", "-m", f"maestro/{task_id}", "feat/one")
    _git(ws, "branch", "-c", "feat/one", "feat/two")  # the copy carries the rename in its reflog
    assert sorted(find_renamed_branches(ws, f"maestro/{task_id}")) == ["feat/one", "feat/two"]
    daemon.followup(task_id, "more")
    final = daemon.wait(task_id, timeout=60)
    assert final["status"]["state"] == "failed"
    assert "more than one branch that came from it (feat/one, feat/two)" in final["metadata"]["error"]
