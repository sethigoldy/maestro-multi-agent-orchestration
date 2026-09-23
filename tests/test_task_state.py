"""Workspace slots, cancellation, follow-ups and answers behave as documented.

Each test here reproduces a defect found in review and checks the fixed
behaviour: queued tasks that were silently dropped, follow-ups that ran
their instruction as a shell command or shared a working tree with another
task, cancels that were lost or undone, answers that never reached the
agent, and waits that missed the finishing event.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from maestro.adapters.generic import GenericAdapter
from maestro.agents import AgentSpec
from maestro.daemon import MaestroDaemon
from maestro.events import EventBus, TaskEvent
from maestro.handoff import HandoffDoc, from_dict, validate_handoff


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    # The guard makes version and help probes (run in the test process's
    # current directory) do nothing; only a real run passes "--go".
    path = dirpath / name
    path.write_text(f'#!/bin/sh\n[ "$1" = "--go" ] || exit 0\n{body}\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git_repo(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "initial", "--allow-empty"]):
        subprocess.run(["git", "-C", str(ws), *args], capture_output=True, env=env)
    return ws


def _agent(daemon: MaestroDaemon, binpath: Path, name: str, body: str, command: str | None = None) -> None:
    _fake_bin(binpath, name, body)
    daemon.registry.save(AgentSpec(name=name, kind="generic", command=command or f"{name} --go", input_mode="stdin"))


def _doc(**kw) -> HandoffDoc:
    base = dict(title="t", request="do it", verification="none", commit_policy="no-commit", target_agent="impl", explicit_target=True)
    base.update(kw)
    return HandoffDoc(**base)


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.02)


def _new_daemon(home: Path) -> MaestroDaemon:
    return MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    return bp


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = _new_daemon(home)
    yield d
    for tid, rec in list(d._tasks.items()):
        if rec.get("state") not in ("completed", "failed", "canceled"):
            try:
                d.cancel(tid, reason="test teardown")
            except (KeyError, ValueError):
                pass
    d.stop()


# ------------------------------------------------------------ workspace slots
def test_every_queued_task_for_a_workspace_runs(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nsleep 0.5\nexit 0")
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(title="a"), ws)
    b = daemon.delegate(_doc(title="b"), ws)
    c = daemon.delegate(_doc(title="c"), ws)
    d = daemon.delegate(_doc(title="d"), ws)
    assert not a["queued"] and b["queued"] and c["queued"] and d["queued"]
    for started in (a, b, c, d):
        assert daemon.wait(started["task_id"], timeout=30)["status"]["state"] == "completed"
    assert daemon._queue == [] and daemon._active == {}


def test_followup_takes_the_workspace_slot(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(title="a"), ws)
    assert daemon.wait(a["task_id"], timeout=30)["status"]["state"] == "completed"
    _agent(daemon, binpath, "impl", "cat > /dev/null\nsleep 1\nexit 0")
    daemon.followup(a["task_id"], "more work")
    assert daemon._active[str(ws)] == a["task_id"]
    b = daemon.delegate(_doc(title="b"), ws)
    assert b["queued"] is True  # waits for the follow-up instead of sharing the tree
    assert daemon.wait(a["task_id"], timeout=30)["status"]["state"] == "completed"
    assert daemon.wait(b["task_id"], timeout=30)["status"]["state"] == "completed"


def test_followup_waits_for_a_busy_workspace(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(title="a"), ws)
    assert daemon.wait(a["task_id"], timeout=30)["status"]["state"] == "completed"
    _agent(daemon, binpath, "impl", "cat > /dev/null\nsleep 1\nexit 0")
    b = daemon.delegate(_doc(title="b"), ws)
    out = daemon.followup(a["task_id"], "more work")
    assert out["queued"] is True and daemon._tasks[a["task_id"]]["state"] == "submitted"
    assert daemon.wait(b["task_id"], timeout=30)["status"]["state"] == "completed"
    final = daemon.wait(a["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
    assert len(final["metadata"]["attempts"]) == 2  # the follow-up turn did run


def test_canceling_a_queued_followup_drops_it(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(title="a"), ws)
    daemon.wait(a["task_id"], timeout=30)
    _agent(daemon, binpath, "impl", "cat > /dev/null\nsleep 1\nexit 0")
    b = daemon.delegate(_doc(title="b"), ws)
    daemon.followup(a["task_id"], "more work")
    daemon.cancel(a["task_id"], reason="changed my mind")
    assert a["task_id"] not in daemon._queue and "continuation" not in daemon._tasks[a["task_id"]]
    assert daemon.wait(b["task_id"], timeout=30)["status"]["state"] == "completed"
    assert daemon._tasks[a["task_id"]]["state"] == "canceled"


def test_answer_after_restart_queues_behind_a_busy_workspace(tmp_path, monkeypatch, binpath):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    ws = _git_repo(tmp_path)
    first = _new_daemon(home)
    _agent(first, binpath, "impl", "cat > /dev/null\nsleep 1\nexit 0")
    parked = first.delegate(_doc(title="parked", sensitive=True), ws)
    assert parked["state"] == "input-required"
    first.stop()
    second = _new_daemon(home)
    try:
        other = second.delegate(_doc(title="other"), ws)  # the restart freed the parked task's slot
        assert other["queued"] is False
        out = second.answer_question(parked["task_id"], "approved")
        assert out == {"task_id": parked["task_id"], "state": "submitted", "queued": True}
        assert second.wait(other["task_id"], timeout=30)["status"]["state"] == "completed"
        assert second.wait(parked["task_id"], timeout=30)["status"]["state"] == "completed"
    finally:
        second.stop()


# ------------------------------------------------------------ verification command
def test_followup_never_runs_its_instruction_as_the_verification_command(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\necho change >> notes.txt\nexit 0")
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(verification="command", request="true"), ws)
    assert daemon.wait(a["task_id"], timeout=30)["status"]["state"] == "completed"
    daemon.followup(a["task_id"], "touch SHOULD_NOT_EXIST")
    assert daemon.wait(a["task_id"], timeout=30)["status"]["state"] == "completed"
    assert not (ws / "SHOULD_NOT_EXIST").exists()
    report = (daemon.state_dir / "tasks" / a["task_id"] / "verification.txt").read_text(encoding="utf-8")
    assert "verification command: true" in report
    # A second follow-up still carries the original command.
    daemon.followup(a["task_id"], "touch ALSO_NOT")
    daemon.wait(a["task_id"], timeout=30)
    assert not (ws / "ALSO_NOT").exists()
    assert daemon._tasks[a["task_id"]]["doc"]["expectations"]["verification_command"] == "true"


def test_verification_command_lets_the_request_stay_prose(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\ntouch built.txt\nexit 0")
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(verification="command", verification_command="test -f built.txt", request="Build the thing."), ws)
    final = daemon.wait(a["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
    assert daemon.maestro._claims(a["task_id"])["task_verification"].startswith("PASSED")


def test_verification_command_validation_and_round_trip():
    doc = validate_handoff(_doc(verification="command", verification_command="pytest -q"))
    assert from_dict(doc.to_dict()).verification_command == "pytest -q"
    assert from_dict(_doc().to_dict()).verification_command is None
    with pytest.raises(ValueError, match="requires verification = 'command'"):
        validate_handoff(_doc(verification="auto", verification_command="pytest"))
    with pytest.raises(ValueError, match="non-empty string"):
        validate_handoff(_doc(verification="command", verification_command="  "))


# ------------------------------------------------------------ cancellation
def test_followup_after_cancel_runs_normally(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nsleep 5\nexit 0")
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(), ws)
    _wait_for(lambda: daemon._tasks[a["task_id"]]["state"] == "working")
    daemon.cancel(a["task_id"])
    _agent(daemon, binpath, "impl", "cat > /dev/null\necho ok\nexit 0")
    daemon.followup(a["task_id"], "try again")
    final = daemon.wait(a["task_id"], timeout=30, stop_states=("completed", "failed"))
    assert final["status"]["state"] == "completed", final["metadata"]["error"]


def test_cancel_during_a_gate_turn_is_not_undone(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    _agent(daemon, binpath, "ver", 'cat > /dev/null\nsleep 3\necho "VERDICT: PASS"\nexit 0')
    _agent(daemon, binpath, "rev", 'cat > /dev/null\necho "VERDICT: PASS"\nexit 0')
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(verify_agent="ver", review_agent="rev", verification="command", request="false"), ws)
    tid = a["task_id"]
    _wait_for(lambda: daemon._tasks[tid].get("gate_seq") is not None)
    daemon.cancel(tid, reason="stop")
    time.sleep(1.5)  # the verifier notices the cancel within a poll interval
    record = daemon._tasks[tid]
    assert record["state"] == "canceled"
    assert [a["role"] for a in record["attempts"] if a.get("role")] == ["implement", "verifier"]


def test_cancel_during_the_first_review_is_not_undone(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    _agent(daemon, binpath, "rev", 'cat > /dev/null\nsleep 3\necho "VERDICT: PASS"\nexit 0')
    _agent(daemon, binpath, "fix", "cat > /dev/null\nexit 0")
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(review_agent="rev", fix_agent="fix", verification="command", request="false"), ws)
    tid = a["task_id"]
    _wait_for(lambda: daemon._tasks[tid].get("gate_seq") is not None)
    daemon.cancel(tid, reason="stop")
    # Wait until the reviewer's attempt is recorded, so whatever the turn does
    # after the review has happened before we look.
    _wait_for(lambda: any(att["agent"] == "rev" for att in daemon._tasks[tid]["attempts"]))
    time.sleep(0.5)
    record = daemon._tasks[tid]
    assert record["state"] == "canceled"
    assert "fix" not in [att["agent"] for att in record["attempts"]]


def test_a_canceled_task_ignores_late_reports_from_its_old_turn(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    tid, _ = daemon._make_record(_doc(), str(ws))
    daemon._register_and_claims(tid, _doc(), str(ws))
    daemon._set_state(tid, "canceled", reason="stop")
    for late in ("working", "input-required", "completed", "failed"):
        daemon._set_state(tid, late)
        assert daemon._tasks[tid]["state"] == "canceled"
    daemon._set_state(tid, "submitted")  # a follow-up starts a new turn
    assert daemon._tasks[tid]["state"] == "submitted"


def test_a_silent_agent_is_canceled_promptly(tmp_path, binpath):
    _fake_bin(binpath, "quiet", "cat > /dev/null\nsleep 20\necho done")
    adapter = GenericAdapter(AgentSpec(name="quiet", kind="generic", command="quiet --go", input_mode="stdin"))
    flag = threading.Event()
    threading.Timer(0.3, flag.set).start()
    started = time.monotonic()
    result = adapter.run("p", tmp_path, "task-1", should_cancel=flag.is_set)
    assert result.ok is False and "canceled" in (result.error or "")
    assert time.monotonic() - started < 5


def test_next_line_outcomes():
    import queue

    from maestro.adapters.base import _next_line

    q: queue.Queue = queue.Queue()
    assert _next_line(q, time.monotonic() - 1, None) == ("timeout", None)
    assert _next_line(q, time.monotonic() + 0.1, lambda: False) == ("timeout", None)  # polls, then the deadline passes
    assert _next_line(q, None, lambda: True) == ("cancel", None)
    q.put("hello\n")
    assert _next_line(q, None, None) == ("line", "hello\n")
    q.put(None)
    assert _next_line(q, None, lambda: False) == ("line", None)


# ------------------------------------------------------------ answers
def test_answer_to_a_gate_park_reaches_the_agent(daemon, tmp_path, binpath):
    log = tmp_path / "prompts.log"
    _agent(daemon, binpath, "impl", f"cat >> {log}\necho ===== >> {log}\necho work >> notes.txt\nexit 0")
    _agent(daemon, binpath, "rev", 'cat > /dev/null\necho "VERDICT: FAIL"\necho "ISSUES:"\necho "- nope"\nexit 0')
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(review_agent="rev", max_bounces=0), ws)
    assert daemon.wait(a["task_id"], timeout=30)["status"]["state"] == "input-required"
    daemon.answer_question(a["task_id"], "PLEASE_RENAME_FOO_TO_BAR")
    daemon.wait(a["task_id"], timeout=30, stop_states=("completed", "failed", "canceled"))
    _wait_for(lambda: log.read_text(encoding="utf-8").count("=====") >= 2)
    second_prompt = log.read_text(encoding="utf-8").split("=====")[1]
    assert "PLEASE_RENAME_FOO_TO_BAR" in second_prompt
    assert "Work-mode gates parked this task" in second_prompt  # the question comes with it


def test_routing_answer_after_restart_is_used(tmp_path, monkeypatch, binpath):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    ws = _git_repo(tmp_path)
    first = _new_daemon(home)
    _agent(first, binpath, "impl", "cat > /dev/null\nexit 0")
    parked = first.delegate(HandoffDoc(title="t", request="r", verification="none", commit_policy="no-commit"), ws)
    assert parked["state"] == "input-required"
    first.stop()
    second = _new_daemon(home)
    try:
        assert second._tasks.get(parked["task_id"]) is None
        out = second.answer_question(parked["task_id"], "impl")
        assert out["state"] == "working"
        final = second.wait(parked["task_id"], timeout=30)
        assert final["status"]["state"] == "completed" and final["metadata"]["target_agent"] == "impl"
    finally:
        second.stop()


def test_durable_record_keeps_park_details(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    ws = _git_repo(tmp_path)
    first = _new_daemon(home)
    tid, record = first._make_record(_doc(), str(ws))
    first._register_and_claims(tid, _doc(), str(ws))
    record.update({"awaiting": "routing", "gates": {"review": {"ok": False}}, "bounces": 1, "base_head": "abc", "verification": "FAILED"})
    first._set_state(tid, "input-required", question="Which agent?")
    first.stop()
    second = _new_daemon(home)
    try:
        rebuilt = second._durable_record(tid)
        assert rebuilt["awaiting"] == "routing" and rebuilt["question"] == "Which agent?"
        assert rebuilt["gates"] == {"review": {"ok": False}} and rebuilt["bounces"] == 1
        assert rebuilt["base_head"] == "abc" and rebuilt["verification"] == "FAILED"
    finally:
        second.stop()


# ------------------------------------------------------------ completion label
def test_completed_label_reflects_the_final_verification(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    _agent(daemon, binpath, "fix", "cat > /dev/null\ntouch .fixed\nexit 0")
    _agent(daemon, binpath, "rev", 'cat > /dev/null\necho "VERDICT: PASS"\nexit 0')
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(review_agent="rev", fix_agent="fix", verification="command", request="test -f .fixed"), ws)
    assert daemon.wait(a["task_id"], timeout=30)["status"]["state"] == "completed"
    assert daemon._tasks[a["task_id"]]["verification"] == "PASSED"


# ------------------------------------------------------------ waiting and streaming
def test_wait_sees_a_transition_right_after_it_subscribes(daemon, tmp_path, monkeypatch):
    ws = _git_repo(tmp_path)
    tid, record = daemon._make_record(_doc(), str(ws))
    daemon._register_and_claims(tid, _doc(), str(ws))
    record["state"] = "working"
    real_subscribe = daemon.bus.subscribe

    def subscribe_then_finish(*types):
        sub = real_subscribe(*types)
        daemon._set_state(tid, "completed")  # the turn ends between subscribing and the state check
        return sub

    monkeypatch.setattr(daemon.bus, "subscribe", subscribe_then_finish)
    started = time.monotonic()
    assert daemon.wait(tid, timeout=5)["status"]["state"] == "completed"
    assert time.monotonic() - started < 1


def test_subscription_start_seq():
    bus = EventBus()
    assert bus.subscribe().start_seq == 0
    bus.publish(TaskEvent(task_id="t", type="state", data={}))
    bus.publish(TaskEvent(task_id="t", type="state", data={}))
    assert bus.subscribe().start_seq == 2


def test_task_event_stream_follows_the_current_turn(tmp_path, monkeypatch, binpath):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("MAESTRO_DISCOVERY_TTL", "0")
    d = _new_daemon(home)
    port = d.start_http(0)
    try:
        _agent(d, binpath, "impl", "cat > /dev/null\necho first-turn\nexit 0")
        ws = _git_repo(tmp_path)
        a = d.delegate(_doc(), ws)
        assert d.wait(a["task_id"], timeout=30)["status"]["state"] == "completed"
        _agent(d, binpath, "impl", "cat > /dev/null\nsleep 1\necho second-turn\nexit 0")
        d.followup(a["task_id"], "again")
        events = []
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/tasks/{a['task_id']}/events", timeout=30) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
        states = [e["data"].get("state") for e in events if e["type"] == "state"]
        lines = [e["data"].get("line") for e in events if e["type"] == "output"]
        assert states[0] == "submitted" and states[-1] == "completed"
        assert "second-turn" in lines and "first-turn" not in lines
    finally:
        d.stop()
