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
    running = tmp_path / "verifier-running"
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    _agent(daemon, binpath, "ver", f'cat > /dev/null\ntouch {running}\nsleep 3\necho "VERDICT: PASS"\nexit 0')
    _agent(daemon, binpath, "rev", 'cat > /dev/null\necho "VERDICT: PASS"\nexit 0')
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(verify_agent="ver", review_agent="rev", verification="command", request="false"), ws)
    tid = a["task_id"]
    _wait_for(running.exists)  # the verifier agent is running
    daemon.cancel(tid, reason="stop")
    time.sleep(1.5)  # the verifier notices the cancel within a poll interval
    record = daemon._tasks[tid]
    assert record["state"] == "canceled"
    assert [a["role"] for a in record["attempts"] if a.get("role")] == ["implement", "verifier"]


def test_cancel_during_the_first_review_is_not_undone(daemon, tmp_path, binpath):
    running = tmp_path / "reviewer-running"
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    _agent(daemon, binpath, "rev", f'cat > /dev/null\ntouch {running}\nsleep 3\necho "VERDICT: PASS"\nexit 0')
    _agent(daemon, binpath, "fix", "cat > /dev/null\nexit 0")
    ws = _git_repo(tmp_path)
    a = daemon.delegate(_doc(review_agent="rev", fix_agent="fix", verification="command", request="false"), ws)
    tid = a["task_id"]
    _wait_for(running.exists)  # the reviewer agent is running
    daemon.cancel(tid, reason="stop")
    # Wait until the reviewer's attempt is recorded, so whatever the turn does
    # after the review has happened before we look.
    _wait_for(lambda: any(att["agent"] == "rev" for att in daemon._tasks[tid]["attempts"]))
    time.sleep(0.5)
    record = daemon._tasks[tid]
    assert record["state"] == "canceled"
    assert "fix" not in [att["agent"] for att in record["attempts"]]


def test_cancel_during_a_fix_turn_is_not_undone(daemon, tmp_path, binpath):
    running = tmp_path / "fixer-running"
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    _agent(daemon, binpath, "rev", 'cat > /dev/null\necho "VERDICT: FAIL"\necho "ISSUES:"\necho "- nope"\nexit 0')
    _agent(daemon, binpath, "fix", f"cat > /dev/null\ntouch {running}\nsleep 3\nexit 0")
    ws = _git_repo(tmp_path)
    tid = daemon.delegate(_doc(review_agent="rev", fix_agent="fix", max_bounces=2), ws)["task_id"]
    _wait_for(running.exists)  # the fixer agent is running
    daemon.cancel(tid, reason="stop")
    _wait_for(lambda: any(att["agent"] == "fix" for att in daemon._tasks[tid]["attempts"]))
    time.sleep(0.5)
    record = daemon._tasks[tid]
    assert record["state"] == "canceled"
    # The canceled fix bounce is the last step: no second review, no second fix.
    assert [att["role"] for att in record["attempts"]] == ["implement", "reviewer", "fix"]


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


# ------------------------------------------------------------ cancel during promotion
@pytest.mark.parametrize("kind", ["delegation", "follow-up"])
@pytest.mark.parametrize("moment", ["before-promotion", "before-the-turn-runs"])
def test_a_task_canceled_while_it_is_promoted_stays_canceled(daemon, tmp_path, binpath, kind, moment):
    # A cancel can arrive after _release took the task off the queue but
    # before it started, or after it started a thread but before that thread
    # ran. Either way the task must stay canceled, run no agent, and hand the
    # workspace to the next queued task.
    _agent(daemon, binpath, "impl", "cat > /dev/null\nsleep 0.3\nexit 0")
    ws = _git_repo(tmp_path)
    if kind == "follow-up":
        victim = daemon.delegate(_doc(title="victim"), ws)["task_id"]
        assert daemon.wait(victim, timeout=30)["status"]["state"] == "completed"
        runner = daemon.delegate(_doc(title="runner"), ws)["task_id"]
        assert daemon.followup(victim, "more work")["queued"] is True
    else:
        runner = daemon.delegate(_doc(title="runner"), ws)["task_id"]
        victim = daemon.delegate(_doc(title="victim"), ws)["task_id"]
    after = daemon.delegate(_doc(title="after"), ws)["task_id"]
    attempts_before = len(daemon._tasks[victim]["attempts"])

    if moment == "before-promotion":
        real_start = daemon._start_queued

        def cancel_then_start(task_id):
            if task_id == victim:
                daemon.cancel(victim, reason="changed my mind")
            real_start(task_id)

        daemon._start_queued = cancel_then_start
    else:
        real_run = daemon._run_task

        def cancel_then_run(task_id, *args, **kwargs):
            if task_id == victim:
                daemon.cancel(victim, reason="changed my mind")
            real_run(task_id, *args, **kwargs)

        daemon._run_task = cancel_then_run

    assert daemon.wait(runner, timeout=30)["status"]["state"] == "completed"
    assert daemon.wait(after, timeout=30)["status"]["state"] == "completed"
    time.sleep(0.5)  # give a wrongly started turn time to show itself
    record = daemon._tasks[victim]
    assert record["state"] == "canceled"
    assert len(record["attempts"]) == attempts_before  # no agent ran for the canceled turn
    assert record.get("queued") is False and "continuation" not in record
    assert daemon._queue == [] and daemon._active == {}


# ------------------------------------------------------------ stale turns
def _blocking_verify(daemon):
    """Make each call to _verify wait until the test lets it go.

    Returns (entered, release): entered[n] is set when call n starts and
    release[n] lets call n finish.
    """
    entered = [threading.Event() for _ in range(4)]
    release = [threading.Event() for _ in range(4)]
    calls = []
    real_verify = daemon._verify

    def verify(*args, **kwargs):
        n = len(calls)
        calls.append(n)
        entered[n].set()
        assert release[n].wait(30)
        return real_verify(*args, **kwargs)

    daemon._verify = verify
    return entered, release


def _count_finished_turns(daemon):
    finished = []
    real_run = daemon._run_task

    def run(*args, **kwargs):
        try:
            real_run(*args, **kwargs)
        finally:
            finished.append(args[0])

    daemon._run_task = run
    return finished


def test_a_canceled_turn_cannot_finish_the_follow_up_or_free_its_workspace(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    ws = _git_repo(tmp_path)
    entered, release = _blocking_verify(daemon)
    finished = _count_finished_turns(daemon)
    tid = daemon.delegate(_doc(verification="command", verification_command="true"), ws)["task_id"]
    assert entered[0].wait(30)  # the first turn is verifying
    daemon.cancel(tid, reason="stop")
    daemon.followup(tid, "more work")
    assert entered[1].wait(30)  # the follow-up turn is verifying too
    release[0].set()  # the canceled turn now finishes its verification
    _wait_for(lambda: len(finished) == 1)
    assert daemon._tasks[tid]["state"] == "working"  # still the follow-up's turn
    assert daemon._active[str(ws)] == tid  # and it still holds the workspace
    release[1].set()
    assert daemon.wait(tid, timeout=30)["status"]["state"] == "completed"
    assert daemon._active == {}


def test_a_canceled_turn_does_not_start_a_gate_agent_after_a_follow_up(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    _agent(daemon, binpath, "rev", 'cat > /dev/null\necho "VERDICT: PASS"\nexit 0')
    ws = _git_repo(tmp_path)
    entered, release = _blocking_verify(daemon)
    finished = _count_finished_turns(daemon)
    tid = daemon.delegate(_doc(review_agent="rev", verification="command", verification_command="true"), ws)["task_id"]
    assert entered[0].wait(30)
    daemon.cancel(tid, reason="stop")
    daemon.followup(tid, "more work")
    assert entered[1].wait(30)
    release[0].set()
    _wait_for(lambda: len(finished) == 1)
    roles = [att.get("role") for att in daemon._tasks[tid]["attempts"]]
    assert "reviewer" not in roles  # the canceled turn started no reviewer
    release[1].set()
    assert daemon.wait(tid, timeout=30)["status"]["state"] == "completed"
    roles = [att.get("role") for att in daemon._tasks[tid]["attempts"]]
    assert roles.count("reviewer") == 1  # only the follow-up's own review ran


def test_a_canceled_turn_does_not_retry_its_agent_after_a_follow_up(tmp_path, monkeypatch, binpath):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=1, backoff_s=0)
    try:
        log = tmp_path / "runs.log"
        _agent(d, binpath, "impl", f"cat > /dev/null\necho run >> {log}\nexit 1")
        ws = _git_repo(tmp_path)
        finished = _count_finished_turns(d)
        entered, release = threading.Event(), threading.Event()
        real_record = d._record_attempt
        calls = []

        def record_attempt(*args, **kwargs):
            real_record(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:  # the first turn's first failed attempt
                entered.set()
                assert release.wait(30)

        d._record_attempt = record_attempt
        tid = d.delegate(_doc(), ws)["task_id"]
        assert entered.wait(30)
        d.cancel(tid, reason="stop")
        d.followup(tid, "try again")
        _wait_for(lambda: d._tasks[tid]["state"] == "failed")  # the follow-up used both of its attempts
        release.set()
        _wait_for(lambda: len(finished) == 2)
        # One run for the canceled turn, two for the follow-up; the canceled
        # turn must not retry once the follow-up replaced its cancel flag.
        assert log.read_text(encoding="utf-8").count("run") == 3
        assert len(d._tasks[tid]["attempts"]) == 3
        assert d._tasks[tid]["state"] == "failed"
    finally:
        d.stop()


def test_a_stale_turn_starts_no_agent(daemon, tmp_path, binpath):
    # Each agent-starting step checks that its turn is still the task's
    # current, uncanceled turn right before it starts the agent.
    _agent(daemon, binpath, "impl", "cat > /dev/null\nexit 0")
    ws = _git_repo(tmp_path)
    doc = _doc(review_agent="impl")
    tid, record = daemon._make_record(doc, str(ws))
    daemon._register_and_claims(tid, doc, str(ws))
    stale = threading.Event()  # not the task's current flag: a replaced turn
    daemon._run_turn(tid, doc, ws, stale)
    turn = daemon._gate_turn(tid, doc, ws, agent_name="impl", role="reviewer", verification_ok=True, report_path=ws / "none.txt", turn_flag=stale)
    assert turn == {"ok": True, "issues": [], "parked": False}
    assert daemon._fix_turn(tid, doc, ws, "impl", ["- x"], False, turn=1, turn_flag=stale) == ("canceled", None)
    assert record["attempts"] == []
    # A turn that was replaced cannot end the task or free its workspace.
    with daemon._lock:
        daemon._active[str(ws)] = tid
    daemon._finish_turn(tid, stale, "failed", error="late")
    assert record["state"] == "submitted" and daemon._active[str(ws)] == tid
    # Nor can it start a queued task's first turn.
    assert daemon._launch(tid, doc, ws, stale, True) == "canceled"
    assert record["state"] == "submitted" and record["attempts"] == []


# ------------------------------------------------------------ concurrent continuations
def _race(calls):
    """Run the given callables at once; return their results or exceptions."""
    results: list = [None] * len(calls)

    def run(i, fn):
        try:
            results[i] = fn()
        except Exception as exc:  # collected for the assertions
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


def test_two_concurrent_follow_ups_start_one_turn(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nsleep 0.3\nexit 0")
    ws = _git_repo(tmp_path)
    tid = daemon.delegate(_doc(), ws)["task_id"]
    assert daemon.wait(tid, timeout=30)["status"]["state"] == "completed"
    # Hold both calls after their early state check, so both see "completed".
    barrier = threading.Barrier(2, timeout=10)
    real_doc = daemon._doc_from_record

    def doc_from_record(record):
        barrier.wait()
        return real_doc(record)

    daemon._doc_from_record = doc_from_record
    results = _race([lambda: daemon.followup(tid, "first"), lambda: daemon.followup(tid, "second")])
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], ValueError) and "still active" in str(errors[0])
    assert daemon.wait(tid, timeout=30)["status"]["state"] == "completed"
    time.sleep(0.5)
    assert len(daemon._tasks[tid]["attempts"]) == 2  # the original turn and one follow-up


def test_two_concurrent_answers_start_one_turn(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "impl", "cat > /dev/null\nsleep 0.3\nexit 0")
    ws = _git_repo(tmp_path)
    tid = daemon.delegate(_doc(sensitive=True), ws)["task_id"]
    assert daemon._tasks[tid]["state"] == "input-required"
    barrier = threading.Barrier(2, timeout=10)
    real_defaults = daemon._apply_defaults

    def apply_defaults(doc):
        barrier.wait()
        return real_defaults(doc)

    daemon._apply_defaults = apply_defaults
    results = _race([lambda: daemon.answer_question(tid, "approved"), lambda: daemon.answer_question(tid, "approved too")])
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], ValueError) and "no longer awaiting" in str(errors[0])
    assert daemon.wait(tid, timeout=30, stop_states=("completed", "failed"))["status"]["state"] == "completed"
    time.sleep(0.5)
    assert len(daemon._tasks[tid]["attempts"]) == 1


# ------------------------------------------------------------ stale park details
def test_a_stale_routing_question_does_not_capture_a_later_answer(daemon, tmp_path, binpath):
    _agent(daemon, binpath, "codex", "cat > /dev/null\necho work >> notes.txt\nexit 0")
    _agent(daemon, binpath, "rev", 'cat > /dev/null\necho "VERDICT: FAIL"\necho "ISSUES:"\necho "- nope"\nexit 0')
    ws = _git_repo(tmp_path)
    # No target and no [defaults]: the task parks on the routing question.
    doc = HandoffDoc(title="t", request="r", verification="none", commit_policy="no-commit", review_agent="rev", max_bounces=0)
    tid = daemon.delegate(doc, ws)["task_id"]
    assert daemon._tasks[tid]["awaiting"] == "routing"
    daemon.cancel(tid, reason="later")
    assert "awaiting" not in daemon._tasks[tid] and "question" not in daemon._tasks[tid]
    daemon.followup(tid, "do the work")
    assert daemon.wait(tid, timeout=30)["status"]["state"] == "input-required"
    assert daemon._tasks[tid]["awaiting"] == "gate"  # the gate park names its own reason
    out = daemon.answer_question(tid, "fix it please")  # an ordinary answer, not a routing answer
    assert out["state"] == "working"


def test_cancel_clears_the_routing_question_across_a_restart(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    ws = _git_repo(tmp_path)
    first = _new_daemon(home)
    tid = first.delegate(HandoffDoc(title="t", request="r", verification="none", commit_policy="no-commit"), ws)["task_id"]
    first.cancel(tid)
    first.stop()
    second = _new_daemon(home)
    try:
        rebuilt = second._durable_record(tid)
        assert rebuilt["state"] == "canceled" and "awaiting" not in rebuilt and "question" not in rebuilt
    finally:
        second.stop()


# ------------------------------------------------------------ cancel versus completion
def test_a_cancel_and_a_completion_at_the_same_moment_do_not_both_happen(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    tid, record = daemon._make_record(_doc(), str(ws))
    daemon._register_and_claims(tid, _doc(), str(ws))
    daemon._set_state(tid, "working")
    finisher: list[threading.Thread] = []

    class CompletesOnSet(threading.Event):
        # The turn finishes on another thread exactly while cancel() runs.
        def set(self):
            done = threading.Event()

            def finish():
                daemon._set_state(tid, "completed")
                done.set()

            thread = threading.Thread(target=finish)
            finisher.append(thread)
            thread.start()
            done.wait(1.0)
            super().set()

    daemon._cancel_flags[tid] = CompletesOnSet()
    daemon.cancel(tid, reason="stop")
    finisher[0].join(10)
    states = [e.data.get("state") for e in daemon.bus.history(task_id=tid, types=("state",))]
    assert record["state"] == "canceled"
    assert "completed" not in states  # never reported as both completed and canceled
    # The other order: a task that already completed cannot be canceled.
    tid2, _ = daemon._make_record(_doc(), str(ws))
    daemon._set_state(tid2, "completed")
    with pytest.raises(ValueError, match="already finished"):
        daemon.cancel(tid2)


# ------------------------------------------------------------ remote agents
def test_a_silent_remote_agent_is_canceled_promptly(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from maestro.adapters.a2a_remote import A2ARemoteAdapter

    canceled = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            # The remote agent prints nothing until it is canceled.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            canceled.wait(20)
            frame = b'event: state\ndata: {"task_id": "r-1", "type": "state", "data": {"state": "canceled", "error": "canceled by orchestrator"}}\n\n'
            self.wfile.write(frame)
            self.wfile.flush()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            if body.get("method") == "tasks/cancel":
                canceled.set()
            out = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": {"task": {"kind": "task", "id": "r-1", "status": {"state": "submitted"}}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        adapter = A2ARemoteAdapter(AgentSpec(name="remote", kind="a2a_remote", command=f"http://127.0.0.1:{server.server_address[1]}"))
        flag = threading.Event()
        threading.Timer(0.3, flag.set).start()
        started = time.monotonic()
        result = adapter.run("p", tmp_path, "task-1", timeout=6, should_cancel=flag.is_set)
        assert result.ok is False and "canceled" in (result.error or "")
        assert time.monotonic() - started < 4
    finally:
        canceled.set()
        server.shutdown()
        server.server_close()
