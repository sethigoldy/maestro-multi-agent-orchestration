"""Execution receipts: projection semantics, CLI, HTTP endpoint, durable reload."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from maestro import cli
from maestro.core import Maestro
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
    # Without a work mode, pin an explicit target: since 0.10 a handoff that
    # names no agent (and has no [defaults]) parks with a routing question.
    if not kw.get("mode"):
        base["target_agent"] = "codex"
        base["explicit_target"] = True
    base.update(kw)
    return HandoffDoc(**base)


@pytest.fixture
def live_daemon(tmp_path, monkeypatch):
    """Daemon with a real HTTP server on an ephemeral port."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    yield d
    d.stop()


@pytest.fixture
def plain_state(tmp_path, monkeypatch):
    """A bare state directory + workspace (no daemon) for projection tests."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    ws = _git_repo(tmp_path)
    return Maestro(ws), ws


def _seed(m: Maestro, tid: str, runtime: dict | None = None, number: int = 1, title: str = "Seed task") -> None:
    """Register a task and write its durable claims (what the daemon would have)."""
    m._register_task(tid, title, number)
    m._write_claim(tid, "task_title", title)
    m._write_claim(tid, "task_number", str(number))
    m._write_claim(tid, "task_request", "the request text")
    m._write_claim(tid, "task_workspace", "/ws/here")
    if runtime is not None:
        m._write_claim(tid, "task_runtime", json.dumps(runtime, ensure_ascii=False))


# ---------------------------------------------------------------- projection

def test_receipt_completed_with_costs(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-aaaaaa"
    _seed(
        m, tid,
        runtime={
            "state": "completed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "attempts": [
                {"agent": "codex", "role": "implement", "ok": True, "exit_code": 0,
                 "duration_s": 271.0, "usage": {"cost_usd": 0.41}, "finished_at": "2026-01-01T00:04:31+00:00"},
                {"agent": "codex", "role": "fix", "ok": True, "exit_code": 0,
                 "duration_s": 182.0, "usage": {"cost_usd": 0.28}, "finished_at": "2026-01-01T00:07:33+00:00"},
            ],
        },
    )
    m._write_claim(tid, "task_verification", f"PASSED: {m.state_dir / 'tasks' / tid / 'verification.txt'}")
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert r["state"] == "completed"
    assert r["task"]["number"] == 1 and r["task"]["title"] == "Seed task"
    assert r["task"]["request"] == "the request text"
    assert r["task"]["workspace"] == "/ws/here"
    assert r["task"]["completed_at"].startswith("2026-01-01T00:07:33")
    assert [a["phase"] for a in r["attempts"]] == ["IMPLEMENT", "FIX"]
    assert r["attempts"][0]["cost_usd"] == 0.41 and r["attempts"][0]["usage"] == {"cost_usd": 0.41}
    assert r["verification"]["ran"] is True and r["verification"]["result"] == "PASSED"
    assert r["verification"]["command"] is None  # report file does not exist here
    totals = r["totals"]
    assert totals["agent_attempts"] == 2 and totals["fix_attempts"] == 1
    assert totals["attempts"] == 3  # two agent turns + one verification run
    assert totals["cost_reported"] is True and totals["cost_usd"] == pytest.approx(0.69)
    assert totals["duration_basis"] == "wall_clock"
    # JSON-safe end to end.
    json.dumps(r)


def test_receipt_failed_task_carries_error(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-bbbbbb"
    _seed(
        m, tid,
        runtime={
            "state": "failed",
            "error": "boom: the agent exploded\nsecond line",
            "started_at": "2026-01-01T00:00:00Z",
            "attempts": [
                {"agent": "codex", "role": "implement", "ok": False, "exit_code": 1,
                 "duration_s": 3.5, "error": "boom: the agent exploded\nsecond line",
                 "finished_at": "2026-01-01T00:00:03+00:00"},
            ],
        },
    )
    from maestro.receipt import build_receipt, format_receipt

    r = build_receipt(tid, m)
    assert r["state"] == "failed"
    assert r["error"].startswith("boom:")
    assert r["attempts"][0]["ok"] is False and r["attempts"][0]["exit_code"] == 1
    # No usage reported anywhere -> no fabricated cost.
    assert r["totals"]["cost_reported"] is False and r["totals"]["cost_usd"] is None
    text = format_receipt(r)
    assert "✗" in text and "boom: the agent exploded" in text
    assert "—" in text  # cost column renders as em dash when unreported


def test_receipt_canceled_state(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-cccccc"
    _seed(m, tid, runtime={"state": "canceled", "attempts": []})
    from maestro.receipt import build_receipt, format_receipt

    r = build_receipt(tid, m)
    assert r["state"] == "canceled"
    text = format_receipt(r)
    assert "CANCELED" in text and "skipped (no deterministic verification recorded)" in text


def test_receipt_phase_fallback_without_runtime(plain_state):
    """Pre-0.9 records: no runtime snapshot, only the phase claim."""
    m, _ = plain_state
    tid = "task-20260101-000000-dddddd"
    m._register_task(tid, "Old task", 7)
    m._write_claim(tid, "task_status", "COMPLETE")
    m._write_claim(tid, "task_number", "abc-not-an-int")  # malformed number must not break
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert r["state"] == "completed"  # COMPLETE phase -> completed (0.8.x semantics)
    assert r["task"]["number"] == 7  # registry wins; the malformed claim is ignored
    assert r["totals"]["duration_s"] is None and r["totals"]["duration_basis"] is None


def test_receipt_turns_knowledge_and_context(plain_state):
    """A continued task surfaces turn count, knowledge metadata, and context stats."""
    m, _ = plain_state
    tid = "task-20260101-000000-kk0001"
    stats = {
        "mode": "reuse", "knowledge_chars": 512, "context_chars": 480,
        "raw_history_bytes": 4096, "estimated_tokens": 120, "reduction_ratio": 0.883,
    }
    _seed(
        m, tid,
        runtime={
            "state": "completed",
            "turn": 2,
            "context_stats": stats,
            "attempts": [
                {"agent": "codex", "role": "implement", "ok": True, "exit_code": 0,
                 "duration_s": 10.0, "finished_at": "2026-01-01T00:00:10+00:00"},
            ],
        },
    )
    m._write_claim(
        tid, "task_knowledge",
        json.dumps({"schema_version": 1, "source_turn": 2, "last_updated": "2026-01-01T00:00:11+00:00"}),
    )
    from maestro.receipt import build_receipt, format_receipt

    r = build_receipt(tid, m)
    assert r["turns"] == 2
    assert r["knowledge"] == {"schema_version": 1, "source_turn": 2, "last_updated": "2026-01-01T00:00:11+00:00"}
    assert r["context"] == stats
    text = format_receipt(r)
    assert "Turns       2 (continuations on the same task)" in text
    assert "Knowledge   schema v1, turn 2" in text


def test_receipt_legacy_task_without_knowledge(plain_state):
    """Old tasks (no task_knowledge claim, single turn) stay clean: no new lines."""
    m, _ = plain_state
    tid = "task-20260101-000000-kk0002"
    _seed(m, tid, runtime={"state": "completed", "attempts": []})
    from maestro.receipt import build_receipt, format_receipt

    r = build_receipt(tid, m)
    assert r["turns"] is None and r["knowledge"] is None and r["context"] is None
    text = format_receipt(r)
    assert "Turns" not in text and "Knowledge" not in text


def test_receipt_malformed_knowledge_claim_ignored(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-kk0003"
    _seed(m, tid, runtime={"state": "completed", "turn": 1, "attempts": []})
    m._write_claim(tid, "task_knowledge", "{not valid json")
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert r["knowledge"] is None and r["turns"] == 1


def test_receipt_knowledge_without_source_turn(plain_state):
    """Knowledge metadata without a source turn renders the bare schema line."""
    m, _ = plain_state
    tid = "task-20260101-000000-kk0004"
    _seed(m, tid, runtime={"state": "completed", "turn": 3, "attempts": []})
    m._write_claim(tid, "task_knowledge", json.dumps({"schema_version": 2}))
    from maestro.receipt import build_receipt, format_receipt

    r = build_receipt(tid, m)
    assert r["knowledge"] == {"schema_version": 2, "source_turn": None, "last_updated": None}
    text = format_receipt(r)
    assert "Knowledge   schema v2" in text and ", turn" not in text.split("Knowledge")[1]


def test_receipt_number_from_registry_when_claim_missing(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-eeeeee"
    m._register_task(tid, "Only registry", 9)
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert r["task"]["number"] == 9 and r["task"]["title"] == "Only registry"


def test_receipt_fallback_attempts(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-ffffff"
    _seed(
        m, tid,
        runtime={
            "state": "completed",
            "attempts": [
                {"agent": "ghost", "role": "implement", "ok": False, "error": "agent 'ghost' unavailable: no adapter"},
                {"agent": "codex", "role": "implement", "ok": True, "exit_code": 0, "duration_s": 12.0,
                 "usage": {"cost_usd": 0.10}, "finished_at": "2026-01-01T00:00:12+00:00"},
            ],
        },
    )
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert [a["agent"] for a in r["attempts"]] == ["ghost", "codex"]
    assert r["attempts"][0]["error"] is not None and r["attempts"][0]["cost_usd"] is None
    # No started_at: duration falls back to the attempt sum.
    assert r["totals"]["duration_basis"] == "attempt_sum"
    assert r["totals"]["duration_s"] == 12.0


def test_receipt_work_mode_gates(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-gggggg"
    _seed(
        m, tid,
        runtime={
            "state": "completed",
            "doc": {"routing": {"verify_agent": "mini", "review_agent": "codex", "fix_agent": "mini"}},
            "attempts": [
                {"agent": "mini", "role": "implement", "ok": True, "duration_s": 10.0, "finished_at": "2026-01-01T00:00:10+00:00"},
                {"agent": "mini", "role": "verifier", "ok": True, "duration_s": 5.0, "finished_at": "2026-01-01T00:00:15+00:00"},
                {"agent": "mini", "role": "fix", "ok": True, "duration_s": 7.0, "finished_at": "2026-01-01T00:00:22+00:00"},
                {"agent": "codex", "role": "reviewer", "ok": True, "duration_s": 9.0, "finished_at": "2026-01-01T00:00:31+00:00"},
            ],
        },
    )
    m._write_claim(tid, "task_gates", json.dumps({
        "verdicts": {
            "verify": {"agent": "mini", "ok": False, "issues": ["flaky assertion"]},
            "review": {"agent": "codex", "ok": True, "issues": []},
        },
        "bounces": 1,
    }))
    from maestro.receipt import build_receipt, format_receipt

    r = build_receipt(tid, m)
    assert [a["phase"] for a in r["attempts"]] == ["IMPLEMENT", "VERIFY", "FIX", "REVIEW"]
    gates = r["gates"]
    assert gates["verifier"] == "mini" and gates["reviewer"] == "codex" and gates["fixer"] == "mini"
    assert gates["verdicts"]["verify"]["ok"] is False and gates["bounces"] == 1
    totals = r["totals"]
    assert totals["review_attempts"] == 1 and totals["fix_attempts"] == 1 and totals["agent_attempts"] == 4
    text = format_receipt(r)
    assert "Gates" in text and "verify: FAIL (mini), 1 issue(s)" in text and "bounces: 1" in text


def test_receipt_verification_command_from_report(plain_state):
    m, ws = plain_state
    tid = "task-20260101-000000-hhhhhh"
    task_dir = m.state_dir / "tasks" / tid
    task_dir.mkdir(parents=True, exist_ok=True)
    report = task_dir / "verification.txt"
    report.write_text("verification command: make check\nexit status: 0\n", encoding="utf-8")
    _seed(m, tid, runtime={"state": "completed", "attempts": []})
    m._write_claim(tid, "task_verification", f"PASSED: {report}")
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert r["verification"]["command"] == "make check"
    assert r["verification"]["report"] == str(report)


def test_receipt_verification_failed_and_unreadable_report(plain_state):
    m, ws = plain_state
    tid = "task-20260101-000000-iiiiii"
    task_dir = m.state_dir / "tasks" / tid
    task_dir.mkdir(parents=True, exist_ok=True)
    report = task_dir / "verification.txt"
    report.write_text("verification command: pytest\nexit status: 1\n", encoding="utf-8")
    report.chmod(0)  # unreadable -> command lookup degrades to None
    _seed(m, tid, runtime={"state": "input-required", "attempts": []})
    m._write_claim(tid, "task_verification", f"FAILED: {report}")
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert r["verification"]["result"] == "FAILED"
    assert r["verification"]["command"] is None  # read failed, not fabricated
    report.chmod(0o644)


def test_receipt_verification_report_missing_and_odd_claim(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-jjjjjj"
    _seed(m, tid, runtime={"state": "completed", "attempts": []})
    # Report path that does not exist: section still built.
    m._write_claim(tid, "task_verification", f"PASSED: {m.state_dir / 'nowhere' / 'v.txt'}")
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert r["verification"]["ran"] is True and r["verification"]["command"] is None

    # Claim without a ": " separator: report stays None.
    m._write_claim(tid, "task_verification", "PASSED")
    r = build_receipt(tid, m)
    assert r["verification"]["result"] == "PASSED" and r["verification"]["report"] is None

    # Non-standard result value passes through untouched.
    m._write_claim(tid, "task_verification", "SKIPPED: /tmp/v.txt")
    r = build_receipt(tid, m)
    assert r["verification"]["result"] == "SKIPPED"


def test_receipt_gates_malformed_and_partial(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-kkkkkk"
    _seed(m, tid, runtime={"state": "completed", "attempts": []})
    from maestro.receipt import build_receipt

    m._write_claim(tid, "task_gates", "{not json")
    r = build_receipt(tid, m)
    assert r["gates"]["verdicts"] == {} and r["gates"]["bounces"] is None

    m._write_claim(tid, "task_gates", json.dumps({"verdicts": ["not", "a", "dict"], "bounces": 2}))
    r = build_receipt(tid, m)
    assert r["gates"]["verdicts"] == {} and r["gates"]["bounces"] == 2

    m._write_claim(tid, "task_gates", json.dumps({"verdicts": {"review": {"agent": "x"}, "junk": "not-a-dict"}}))
    r = build_receipt(tid, m)
    assert set(r["gates"]["verdicts"]) == {"review"}


def test_receipt_runtime_malformed_and_unknown_task(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-llllll"
    m._register_task(tid, "Broken", 3)
    m._write_claim(tid, "task_runtime", "[1, 2, 3]")  # JSON but not an object
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert r["state"] == "unknown" and r["attempts"] == []

    # Non-dict attempt entries are skipped, numbering stays dense.
    m._write_claim(tid, "task_runtime", json.dumps({"state": "working", "attempts": ["junk", None, {"agent": "codex", "ok": True}]}))
    r = build_receipt(tid, m)
    assert len(r["attempts"]) == 1 and r["attempts"][0]["n"] == 1

    # A task with no claims at all: empty receipt, not an error.
    r = build_receipt("task-20260101-000000-zzzzzz", m)
    assert r["state"] == "unknown" and r["task"]["id"].endswith("zzzzzz")
    assert r["totals"]["attempts"] == 0


def test_receipt_usage_variants(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-mmmmmm"
    _seed(
        m, tid,
        runtime={
            "state": "completed",
            "attempts": [
                {"agent": "a", "role": "implement", "ok": True, "usage": {"tokens": 1200}},          # usage, no cost
                {"agent": "b", "role": "implement", "ok": True, "usage": {"total_cost_usd": 0.5}},   # alternate key
                {"agent": "c", "role": "implement", "ok": True, "usage": "not-a-dict"},              # malformed usage
                {"agent": "d", "role": "mystery", "ok": True},                                       # unknown role
            ],
        },
    )
    from maestro.receipt import build_receipt

    r = build_receipt(tid, m)
    assert r["attempts"][0]["cost_usd"] is None  # tokens only: no cost fabricated
    assert r["attempts"][1]["cost_usd"] == 0.5   # total_cost_usd spelling counts
    assert r["attempts"][2]["usage"] is None     # non-dict usage dropped
    assert r["attempts"][3]["phase"] == "IMPLEMENT"  # unknown role falls back
    assert r["totals"]["cost_reported"] is True and r["totals"]["cost_usd"] == pytest.approx(0.5)


def test_format_receipt_duration_and_cost_helpers(plain_state):
    from maestro.receipt import _fmt_cost, _fmt_duration

    assert _fmt_duration(None) is None
    assert _fmt_duration(True) is None
    assert _fmt_duration("soon") is None
    assert _fmt_duration(45) == "45s"
    assert _fmt_duration(271) == "4m 31s"
    assert _fmt_duration(3600 + 125) == "1h 2m"
    assert _fmt_cost(None) is None
    assert _fmt_cost(False) is None
    assert _fmt_cost("rich") is None
    assert _fmt_cost(0.91) == "$0.91"


def test_receipt_parse_edges_and_claim_only_number(plain_state):
    from maestro.receipt import _parse_ts, build_receipt, format_receipt

    # _parse_ts: invalid string -> None; naive datetime -> assumed UTC.
    assert _parse_ts("not-a-date") is None
    assert _parse_ts(None) is None and _parse_ts("") is None and _parse_ts(123) is None
    naive = _parse_ts("2026-01-01T05:00:00")
    assert naive is not None and naive.strftime("%H:%M") == "05:00"

    m, _ = plain_state
    tid = "task-20260101-000000-pppppp"

    # Invalid JSON in the runtime claim degrades to an empty snapshot.
    m._write_claim(tid, "task_runtime", "{definitely not json")
    r = build_receipt(tid, m)
    assert r["state"] == "unknown"

    # Number from a valid numeric claim when no registry record exists.
    m._write_claim(tid, "task_number", "42")
    r = build_receipt(tid, m)
    assert r["task"]["number"] == 42

    # Malformed number claim with no registry record: stays None, no crash.
    tid_bad = "task-20260101-000000-qqqqqq"
    m._write_claim(tid_bad, "task_number", "ninety-nine")
    r = build_receipt(tid_bad, m)
    assert r["task"]["number"] is None and r["state"] == "unknown"

    # Verification report that exists but carries no command line.
    task_dir = m.state_dir / "tasks" / tid
    task_dir.mkdir(parents=True, exist_ok=True)
    report = task_dir / "verification.txt"
    report.write_text("exit status: 0\n", encoding="utf-8")
    m._write_claim(tid, "task_verification", f"PASSED: {report}")
    r = build_receipt(tid, m)
    assert r["verification"]["ran"] is True and r["verification"]["command"] is None

    # A gate verdict without an agent name still renders; no bounces key.
    m._write_claim(tid, "task_gates", json.dumps({"verdicts": {"review": {"ok": True}}}))
    text = format_receipt(build_receipt(tid, m))
    assert "review: PASS" in text and "(None)" not in text and "bounces:" not in text


def test_format_receipt_minimal_task(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-nnnnnn"
    # No registry record, no claims: the receipt still renders.
    from maestro.receipt import build_receipt, format_receipt

    r = build_receipt(tid, m)
    text = format_receipt(r)
    assert "Task task-20260101-000000-nnnnnn" in text  # id fallback when no number
    assert "Status      UNKNOWN" in text
    assert "Final result\nUNKNOWN" in text


def test_format_receipt_runs_breakdown_and_single_line_attempts(plain_state):
    """Human format: 'Runs' disambiguates total executions from the Attempts
    list, and each attempt is one scannable line (phase/agent/duration/cost/mark)."""
    m, _ = plain_state
    tid = "task-20260101-000000-fmtfmt"
    _seed(
        m, tid,
        runtime={
            "state": "completed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "attempts": [
                {"agent": "codex", "role": "implement", "ok": True, "exit_code": 0,
                 "duration_s": 245.0, "usage": {"cost_usd": 0.51}, "finished_at": "2026-01-01T00:04:05+00:00"},
                {"agent": "codex-mini", "role": "verifier", "ok": True, "exit_code": 0,
                 "duration_s": 28.0, "finished_at": "2026-01-01T00:04:33+00:00"},
                {"agent": "codex-mini", "role": "fix", "ok": False, "exit_code": 1,
                 "duration_s": 9.0, "error": "fixer exploded\nmore detail",
                 "finished_at": "2026-01-01T00:04:42+00:00"},
            ],
        },
    )
    m._write_claim(tid, "task_verification", f"PASSED: {m.state_dir / 'tasks' / tid / 'verification.txt'}")
    from maestro.receipt import build_receipt, format_receipt

    r = build_receipt(tid, m)
    text = format_receipt(r)
    # Total runs include the verification run; breakdown shown when present.
    assert "Runs        4 (3 agent · 1 verification)" in text
    # Each attempt is a single line: n, phase, agent, duration, cost, mark.
    assert "1  IMPLEMENT codex" in text
    assert "2  VERIFY    codex-mini" in text
    assert "3  FIX       codex-mini" in text
    line1 = next(l for l in text.splitlines() if l.startswith("1  IMPLEMENT"))
    assert "$0.51" in line1 and "✓" in line1  # cost + mark on the same line as the phase
    # Failed attempt: em dash cost, ✗ mark, first error line indented below.
    line3 = next(l for l in text.splitlines() if l.startswith("3  FIX"))
    assert "—" in line3 and "✗" in line3
    assert "        fixer exploded" in text
    # The summary no longer uses the ambiguous bare 'Attempts' count line.
    assert not any(l.startswith("Attempts") and l[8:].strip().isdigit() for l in text.splitlines())


def test_format_receipt_runs_without_verification(plain_state):
    m, _ = plain_state
    tid = "task-20260101-000000-fmtfmt"
    _seed(
        m, tid,
        runtime={
            "state": "completed",
            "attempts": [
                {"agent": "codex", "role": "implement", "ok": True, "exit_code": 0, "duration_s": 5.0},
            ],
        },
    )
    from maestro.receipt import build_receipt, format_receipt

    text = format_receipt(build_receipt(tid, m))
    assert "Runs        1" in text  # no verification runs: bare count, no breakdown


# ---------------------------------------------------------------- live daemon

def _run_fake_task(live_daemon, tmp_path, monkeypatch, doc, agent="codex", body=None, ws_name=None):
    """Delegate with a fake agent binary on PATH; return (task_id, workspace).

    PATH is set via monkeypatch so it stays in place for the whole test — the
    agent runs on a daemon thread and must not race a premature restore.
    """
    bp = tmp_path / "bin"
    bp.mkdir(exist_ok=True)
    default = 'cat > /dev/null\necho working\nexit 0'
    _fake_bin(bp, agent if agent in ("codex", "claude") else "codex", body or default)
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ.get('PATH', '')}")
    ws = _git_repo(tmp_path, name=ws_name or f"ws-{agent}")
    started = live_daemon.delegate(doc, ws)
    return started["task_id"], ws


def _register_generic(registry_home: Path, name: str, command: str) -> None:
    """Register a generic agent whose command is an explicit script path."""
    from maestro.agents import AgentRegistry, AgentSpec

    AgentRegistry(registry_home).save(
        AgentSpec(name=name, kind="generic", command=command, input_mode="arg", output_format="jsonl")
    )


def test_live_receipt_http_and_durable_reload(live_daemon, tmp_path, monkeypatch):
    body = 'cat > /dev/null\necho \'{"total_cost_usd": 0.77}\'\nexit 0'
    tid, _ws = _run_fake_task(live_daemon, tmp_path, monkeypatch, _doc(verification="auto"), body=body)
    live_daemon.wait(tid, timeout=60)

    # Live: HTTP endpoint serves the receipt.
    with urllib.request.urlopen(f"http://127.0.0.1:{live_daemon.port}/tasks/{tid}/receipt", timeout=5) as resp:
        assert resp.status == 200
        live = json.loads(resp.read().decode("utf-8"))
    assert live["state"] == "completed"
    assert live["attempts"][0]["cost_usd"] == pytest.approx(0.77)
    assert live["verification"]["ran"] is True

    # Durable: daemon stopped, receipt still available from state alone.
    live_daemon.stop()
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    m = Maestro(_git_repo(tmp_path, name="ws-reload"))
    try:
        from maestro.receipt import build_receipt

        r = build_receipt(tid, m)
    finally:
        m.close()
    assert r["state"] == "completed" and len(r["attempts"]) == 1


def test_live_receipt_failed_and_canceled(live_daemon, tmp_path, monkeypatch):
    # Failed: agent exits non-zero.
    tid, _ = _run_fake_task(
        live_daemon, tmp_path, monkeypatch, _doc(),
        body="cat > /dev/null\necho 'it broke'\nexit 1",
    )
    final = live_daemon.wait(tid, timeout=60)
    assert final["status"]["state"] == "failed"

    # Canceled: slow agent, cancel mid-flight.
    tid2, _ = _run_fake_task(
        live_daemon, tmp_path, monkeypatch, _doc(),
        body="cat > /dev/null\nsleep 30\nexit 0",
        ws_name="ws-canceled",
    )
    import time

    deadline = time.time() + 15
    while time.time() < deadline:
        if (live_daemon._tasks.get(tid2) or {}).get("state") == "working":
            break
        time.sleep(0.1)
    live_daemon.cancel(tid2, reason="test cancel")

    from maestro.receipt import build_receipt

    failed = build_receipt(tid, live_daemon.maestro)
    assert failed["state"] == "failed" and failed["attempts"][0]["ok"] is False
    canceled = build_receipt(tid2, live_daemon.maestro)
    assert canceled["state"] == "canceled"


def test_live_receipt_fallback_chain(live_daemon, tmp_path, monkeypatch):
    # Hermetic: two registered generic agents. The primary points at a binary
    # that does not exist anywhere; the fallback is a real fake script.
    bp = tmp_path / "bin"
    bp.mkdir(exist_ok=True)
    good = bp / "workfake"
    _fake_bin(bp, "workfake", 'cat > /dev/null\necho ok\nexit 0')
    home = Path(os.environ["MAESTRO_HOME"])
    _register_generic(home, "ghost-agent", "/nonexistent/ghost-agent {prompt}")
    _register_generic(home, "work-agent", f"{good} {{prompt}}")
    ws = _git_repo(tmp_path, name="ws-fb")
    doc = _doc(target_agent="ghost-agent", fallback=["work-agent"])
    started = live_daemon.delegate(doc, ws)
    tid = started["task_id"]
    final = live_daemon.wait(tid, timeout=60)
    assert final["status"]["state"] == "completed"
    from maestro.receipt import build_receipt

    r = build_receipt(tid, live_daemon.maestro)
    agents = [(a["agent"], a["ok"]) for a in r["attempts"]]
    assert ("ghost-agent", False) in agents and ("work-agent", True) in agents


def test_live_receipt_work_mode_bounce(live_daemon, tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir(exist_ok=True)
    # Implementer ok; verifier FAILs with one issue (drives one fix bounce);
    # deterministic verification (auto-detected) passes after the fix.
    _fake_bin(bp, "codex", 'cat > /dev/null\necho \'{"total_cost_usd": 0.1}\'\nexit 0')
    bp2 = tmp_path / "bin2"
    bp2.mkdir(exist_ok=True)
    _fake_bin(bp2, "claude", 'cat > /dev/null\necho "VERDICT: FAIL"\necho "- flaky assertion"\nexit 0')
    monkeypatch.setenv("PATH", f"{bp2}{os.pathsep}{bp}{os.pathsep}{os.environ.get('PATH', '')}")
    ws = _git_repo(tmp_path, name="ws-wm")
    doc = _doc(
        target_agent="codex",
        verification="auto",
        verify_agent="claude_code",
        fix_agent="codex",
        max_bounces=1,
    )
    started = live_daemon.delegate(doc, ws)
    tid = started["task_id"]
    final = live_daemon.wait(tid, timeout=90)
    assert final["status"]["state"] in ("completed", "input-required")
    from maestro.receipt import build_receipt

    r = build_receipt(tid, live_daemon.maestro)
    roles = [a["role"] for a in r["attempts"]]
    assert "implement" in roles and "verifier" in roles
    verify_verdict = r["gates"]["verdicts"].get("verify", {})
    assert verify_verdict.get("ok") is False and verify_verdict.get("issues") == ["flaky assertion"]
    if final["status"]["state"] == "completed":
        assert "fix" in roles  # the bounce ran a fixer turn
        assert r["gates"]["bounces"] == 1


def test_http_receipt_unknown_task(live_daemon):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"http://127.0.0.1:{live_daemon.port}/tasks/task-20990101-000000-deadbe/receipt", timeout=5)
    assert excinfo.value.code == 404
    # Numeric reference that matches nothing: also 404.
    with pytest.raises(urllib.error.HTTPError) as excinfo2:
        urllib.request.urlopen(f"http://127.0.0.1:{live_daemon.port}/tasks/999/receipt", timeout=5)
    assert excinfo2.value.code == 404


# ---------------------------------------------------------------- CLI

def test_cli_task_receipt_human_and_json(live_daemon, tmp_path, monkeypatch, capsys):
    body = 'cat > /dev/null\necho \'{"total_cost_usd": 0.5}\'\nexit 0'
    tid, _ws = _run_fake_task(live_daemon, tmp_path, monkeypatch, _doc(verification="auto"), body=body)
    live_daemon.wait(tid, timeout=60)

    # Human-readable (daemon reachable: served over HTTP).
    assert cli.main(["task", "receipt", tid]) == 0
    out = capsys.readouterr().out
    assert "Maestro Execution Receipt" in out
    assert "IMPLEMENT" in out and "Verification" in out

    # JSON.
    assert cli.main(["task", "receipt", tid, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["task"]["id"] == tid and data["state"] == "completed"

    # Numeric reference resolves through the registry.
    number = live_daemon.maestro.status(tid)["task_number"]
    assert cli.main(["task", "receipt", str(number), "--json"]) == 0
    by_number = json.loads(capsys.readouterr().out)
    assert by_number["task"]["id"] == tid

    # Durable: with the daemon stopped, the local fallback still answers.
    live_daemon.stop()
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    assert cli.main(["task", "receipt", tid, "--json"]) == 0
    durable = json.loads(capsys.readouterr().out)
    assert durable["task"]["id"] == tid and durable["state"] == "completed"


def test_cli_task_receipt_unknown_task(live_daemon, monkeypatch, capsys):
    # Unknown locally AND daemon up: the daemon 404s, local build has no data.
    code = cli.main(["task", "receipt", "task-20990101-000000-deadbe"])
    out = capsys.readouterr().out
    assert code == 0 and "UNKNOWN" in out

    # Unknown everywhere with no daemon: clear error, exit 2.
    live_daemon.stop()
    empty_home = Path(os.environ["MAESTRO_HOME"]) / "empty-home"
    empty_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MAESTRO_HOME", str(empty_home))
    code = cli.main(["task", "receipt", "424242"])
    assert code == 2
    assert "Unknown task number" in capsys.readouterr().err
