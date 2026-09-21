"""Routing defaults ([defaults] config) and the interactive routing question.

Covers: daemon._apply_defaults, _routing_question, _parse_routing_answer, and
the parking/resume wiring in delegate(), _start_queued(), and answer_question().
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from maestro.daemon import MaestroDaemon
from maestro.handoff import HandoffDoc


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git_repo(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(ws), *args], text=True, capture_output=True, env=env)

    git("init", "-q")
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "initial")
    return ws


def _doc(**kw) -> HandoffDoc:
    base = dict(title="Do the thing", request="Implement it", verification="none", commit_policy="no-commit")
    base.update(kw)
    return HandoffDoc(**base)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    yield d
    d.stop()


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    return bp


# ------------------------------------------------------------------- parking

def test_no_defaults_parks_with_routing_question(daemon, tmp_path, binpath):
    # No [defaults], no explicit target: the task must park, not run codex.
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    assert started["queued"] is False and started["state"] == "input-required"
    record = daemon._tasks[started["task_id"]]
    assert record["awaiting"] == "routing"
    question = record.get("question") or ""
    assert "Which agent (and model) should run this task?" in question
    assert "Available agents:" in question
    # Nothing ran: no attempts; the parked task still holds its workspace slot.
    assert record["attempts"] == []
    with daemon._lock:
        assert daemon._active.get(str(ws)) == started["task_id"]


def test_parked_task_lists_registered_agents(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", "exit 0")
    _fake_bin(binpath, "opencode", "exit 0")
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    question = daemon._tasks[started["task_id"]].get("question") or ""
    assert "- codex" in question and "- opencode" in question


def test_question_lists_registered_agent_with_model(daemon, tmp_path, binpath):
    from maestro.agents import AgentSpec

    daemon.registry.save(AgentSpec(name="codex", kind="codex", model="gpt-5.6-luna"))
    _fake_bin(binpath, "codex", "exit 0")
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    question = daemon._tasks[started["task_id"]].get("question") or ""
    assert "- codex" in question and "[model: gpt-5.6-luna]" in question


def test_parked_task_can_be_canceled(daemon, tmp_path):
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    result = daemon.cancel(started["task_id"])
    assert result["state"] == "canceled"


# --------------------------------------------------------------- [defaults]

def test_defaults_resolve_target_fallback_model_effort(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\necho done\nexit 0')
    daemon.maestro.config["defaults"] = {"agent": "codex", "fallback": ["opencode"], "model": "gpt-5.6-luna", "effort": "max"}
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(), ws)
    assert started["state"] == "submitted"  # not parked
    final = daemon.wait(started["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
    doc = daemon._tasks[started["task_id"]]["doc"]
    assert doc["routing"]["target_agent"] == "codex"
    assert doc["routing"]["explicit_target"] is True
    assert doc["routing"]["fallback"] == ["opencode"]
    assert doc["agent_settings"] == {"model": "gpt-5.6-luna", "effort": "max"}


def test_defaults_do_not_override_explicit_handoff(daemon, tmp_path, binpath):
    _fake_bin(binpath, "claude", 'cat > /dev/null\nexit 0')
    daemon.maestro.config["defaults"] = {"agent": "codex", "model": "default-model"}
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(target_agent="claude_code", explicit_target=True), ws)
    assert started["state"] == "submitted"
    final = daemon.wait(started["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
    doc = daemon._tasks[started["task_id"]]["doc"]
    assert doc["routing"]["target_agent"] == "claude_code"
    # Explicit handoff sets its own model: the default must not clobber it.
    started2 = daemon.delegate(_doc(target_agent="claude_code", explicit_target=True, agent_settings={"model": "handoff-model"}), _git_repo(tmp_path, "ws2"))
    final2 = daemon.wait(started2["task_id"], timeout=30)
    assert final2["status"]["state"] == "completed"
    doc2 = daemon._tasks[started2["task_id"]]["doc"]
    assert doc2["agent_settings"]["model"] == "handoff-model"


def test_defaults_unknown_agent_rejected(daemon, tmp_path):
    daemon.maestro.config["defaults"] = {"agent": "nope"}
    ws = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="Unknown default agent 'nope'"):
        daemon.delegate(_doc(), ws)


def test_defaults_self_delegation_rejected(daemon, tmp_path):
    daemon.maestro.config["defaults"] = {"agent": "codex"}
    ws = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="cannot delegate to itself"):
        daemon.delegate(_doc(origin_agent="codex"), ws)


def test_defaults_applied_to_queued_task_on_promotion(daemon, tmp_path, binpath):
    # First task occupies the workspace; second is queued WITHOUT defaults at
    # delegate time, then [defaults] appears before promotion: it must resolve.
    _fake_bin(binpath, "codex", 'cat > /dev/null\ni=0\nwhile [ $i -lt 100 ]; do echo tick; sleep 0.1; i=$((i+1)); done')
    ws = _git_repo(tmp_path)
    first = daemon.delegate(_doc(title="first", target_agent="codex", explicit_target=True), ws)
    time.sleep(0.3)
    second = daemon.delegate(_doc(title="second"), ws)
    assert second["queued"] is True
    daemon.maestro.config["defaults"] = {"agent": "codex"}
    daemon.cancel(first["task_id"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        record = daemon._tasks[second["task_id"]]
        if record["state"] not in ("submitted",):
            break
        time.sleep(0.1)
    final = daemon.wait(second["task_id"], timeout=60)
    assert final["status"]["state"] == "completed"


def test_queued_task_parks_when_still_unresolved(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\ni=0\nwhile [ $i -lt 100 ]; do echo tick; sleep 0.1; i=$((i+1)); done')
    ws = _git_repo(tmp_path)
    first = daemon.delegate(_doc(title="first", target_agent="codex", explicit_target=True), ws)
    time.sleep(0.3)
    second = daemon.delegate(_doc(title="second"), ws)
    assert second["queued"] is True
    daemon.cancel(first["task_id"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        record = daemon._tasks[second["task_id"]]
        if record["state"] != "submitted":
            break
        time.sleep(0.1)
    assert daemon._tasks[second["task_id"]]["state"] == "input-required"
    assert daemon._tasks[second["task_id"]].get("awaiting") == "routing"


# ---------------------------------------------------------------- answering

def _parked_task(daemon, tmp_path, **doc_kw):
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(**doc_kw), ws)
    assert started["state"] == "input-required"
    return started, ws


def test_answer_bare_agent_name_resumes(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\necho done\nexit 0')
    started, ws = _parked_task(daemon, tmp_path)
    result = daemon.answer_question(started["task_id"], "codex")
    assert result["state"] == "working"
    final = daemon.wait(started["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
    doc = daemon._tasks[started["task_id"]]["doc"]
    assert doc["routing"]["target_agent"] == "codex" and doc["routing"]["explicit_target"] is True
    assert daemon._tasks[started["task_id"]].get("awaiting") is None


def test_answer_key_value_pairs_with_model(daemon, tmp_path, binpath):
    _fake_bin(binpath, "claude", 'cat > /dev/null\nexit 0')
    started, ws = _parked_task(daemon, tmp_path)
    daemon.answer_question(started["task_id"], "agent=claude_code model=sonnet-4")
    final = daemon.wait(started["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
    doc = daemon._tasks[started["task_id"]]["doc"]
    assert doc["routing"]["target_agent"] == "claude_code"
    assert doc["agent_settings"]["model"] == "sonnet-4"


def test_answer_json_object(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    started, ws = _parked_task(daemon, tmp_path)
    daemon.answer_question(started["task_id"], '{"agent": "codex", "model": "gpt-5.6-luna"}')
    final = daemon.wait(started["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
    doc = daemon._tasks[started["task_id"]]["doc"]
    assert doc["routing"]["target_agent"] == "codex"
    assert doc["agent_settings"]["model"] == "gpt-5.6-luna"


def test_answer_unknown_agent_stays_parked(daemon, tmp_path):
    started, ws = _parked_task(daemon, tmp_path)
    with pytest.raises(ValueError, match="Unknown agent 'bogus'"):
        daemon.answer_question(started["task_id"], "bogus")
    record = daemon._tasks[started["task_id"]]
    assert record["state"] == "input-required" and record.get("awaiting") == "routing"


def test_answer_unparseable_stays_parked(daemon, tmp_path):
    started, ws = _parked_task(daemon, tmp_path)
    with pytest.raises(ValueError, match="Could not determine an agent"):
        daemon.answer_question(started["task_id"], "no idea which one to pick")
    record = daemon._tasks[started["task_id"]]
    assert record["state"] == "input-required" and record.get("awaiting") == "routing"


def test_answer_bare_token_among_words(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    started, ws = _parked_task(daemon, tmp_path)
    daemon.answer_question(started["task_id"], "please use codex for this")
    final = daemon.wait(started["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"


def test_answer_json_non_dict_falls_through(daemon, tmp_path):
    started, ws = _parked_task(daemon, tmp_path)
    with pytest.raises(ValueError, match="Could not determine an agent"):
        daemon.answer_question(started["task_id"], "[1, 2]")
    assert daemon._tasks[started["task_id"]]["state"] == "input-required"


def test_answer_json_non_string_agent_ignored(daemon, tmp_path):
    started, ws = _parked_task(daemon, tmp_path)
    with pytest.raises(ValueError, match="Could not determine an agent"):
        daemon.answer_question(started["task_id"], '{"agent": 5}')
    assert daemon._tasks[started["task_id"]]["state"] == "input-required"


def test_answer_json_non_string_model_ignored(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    started, ws = _parked_task(daemon, tmp_path)
    daemon.answer_question(started["task_id"], '{"agent": "codex", "model": 7}')
    final = daemon.wait(started["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
    doc = daemon._tasks[started["task_id"]]["doc"]
    assert doc["routing"]["target_agent"] == "codex" and "model" not in doc["agent_settings"]


def test_answer_json_blank_model_ignored(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    started, ws = _parked_task(daemon, tmp_path)
    daemon.answer_question(started["task_id"], '{"agent": "codex", "model": "   "}')
    final = daemon.wait(started["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
    doc = daemon._tasks[started["task_id"]]["doc"]
    assert "model" not in doc["agent_settings"]


def test_answer_empty_rejected(daemon, tmp_path):
    started, ws = _parked_task(daemon, tmp_path)
    with pytest.raises(ValueError, match="Answer cannot be empty"):
        daemon.answer_question(started["task_id"], "   ")


# ------------------------------------------- sensitive + unresolved routing

def test_sensitive_parks_first_then_routing(daemon, tmp_path):
    # Sensitive workspace without a resolvable target: the approval gate wins,
    # and answering it must re-park with the routing question instead of running.
    started, ws = _parked_task(daemon, tmp_path, sensitive=True)
    record = daemon._tasks[started["task_id"]]
    assert "Approval required" in (record.get("question") or "")
    result = daemon.answer_question(started["task_id"], "approved")
    assert result["state"] == "input-required"  # re-parked for routing
    record = daemon._tasks[started["task_id"]]
    assert record.get("awaiting") == "routing"
    assert "Which agent (and model) should run this task?" in (record.get("question") or "")


def test_sensitive_with_explicit_target_runs_after_approval(daemon, tmp_path, binpath):
    _fake_bin(binpath, "codex", 'cat > /dev/null\nexit 0')
    ws = _git_repo(tmp_path)
    started = daemon.delegate(_doc(sensitive=True, target_agent="codex", explicit_target=True), ws)
    assert started["state"] == "input-required"
    result = daemon.answer_question(started["task_id"], "approved")
    assert result["state"] == "working"
    final = daemon.wait(started["task_id"], timeout=30)
    assert final["status"]["state"] == "completed"
