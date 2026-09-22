"""Task knowledge: schema, projection from durable state, budgeted rendering."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from maestro.knowledge import (
    CHARS_PER_TOKEN,
    DEFAULT_MAX_TOKENS,
    SCHEMA_VERSION,
    TaskKnowledge,
    continuation_budget_chars,
    estimate_tokens,
    parse_continuation,
    project_knowledge,
    render_continuation_block,
)


# ------------------------------------------------------------------ primitives
def test_estimate_tokens_is_labeled_approximation():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1  # max(1, ...) keeps tiny text at one token
    assert estimate_tokens("x" * 8) == 2
    assert estimate_tokens("y" * 100) == 25
    assert CHARS_PER_TOKEN == 4


@pytest.mark.parametrize("raw,expected", [
    (None, {"enabled": True, "max_tokens": DEFAULT_MAX_TOKENS}),
    ({}, {"enabled": True, "max_tokens": DEFAULT_MAX_TOKENS}),
    ({"enabled": False}, {"enabled": False, "max_tokens": DEFAULT_MAX_TOKENS}),
    ({"max_tokens": 100}, {"enabled": True, "max_tokens": 100}),
    ({"enabled": True, "max_tokens": 1}, {"enabled": True, "max_tokens": 1}),
])
def test_parse_continuation_valid(raw, expected):
    assert parse_continuation(raw) == expected


@pytest.mark.parametrize("raw", [
    "nope",
    ["enabled"],
    {"nope": 1},
    {"enabled": "yes"},
    {"max_tokens": "big"},
    {"max_tokens": True},
    {"max_tokens": -5},
    {"max_tokens": 0},
    {"max_tokens": 2.5},
])
def test_parse_continuation_rejects_malformed(raw):
    with pytest.raises(ValueError):
        parse_continuation(raw)


def test_continuation_budget_chars_default_and_env(monkeypatch):
    assert continuation_budget_chars({}) == DEFAULT_MAX_TOKENS * CHARS_PER_TOKEN
    assert continuation_budget_chars(None) == DEFAULT_MAX_TOKENS * CHARS_PER_TOKEN  # non-dict config tolerated
    monkeypatch.setenv("MAESTRO_CONTINUATION_MAX_TOKENS", "10")
    assert continuation_budget_chars({}) == 40
    monkeypatch.setenv("MAESTRO_CONTINUATION_MAX_TOKENS", "garbage")
    assert continuation_budget_chars({"continuation": {"max_tokens": 7}}) == 28  # invalid env falls back
    monkeypatch.delenv("MAESTRO_CONTINUATION_MAX_TOKENS")
    assert continuation_budget_chars({"continuation": {"max_tokens": 7}}) == 28


# ------------------------------------------------------------------ schema
def test_serialize_is_deterministic_and_roundtrips():
    k = TaskKnowledge(
        task_id="task-1", goal="Do X", constraints=["verification=auto"],
        decisions=["d1"], assumptions=["a1"], files_changed=["b.py", "a.py"],
        current_state="completed, branch b, turn 2",
        verification={"status": "FAILED", "command": "pytest", "failures": ["FAILED tests/t.py::a"]},
        known_issues=["reviewer: bug"], latest_summary="done-ish",
        last_updated="2026-01-01T00:00:00+00:00", source_turn=2,
    )
    assert k.schema_version == SCHEMA_VERSION
    again = TaskKnowledge.from_dict(json.loads(k.serialize()))
    assert again is not None and again.serialize() == k.serialize()
    assert json.loads(k.serialize())["files_changed"] == ["b.py", "a.py"]  # to_dict copies lists


@pytest.mark.parametrize("payload,expect_none", [
    ("not a dict", True),
    ([1, 2], True),
    ({"task_id": "t"}, True),  # no schema_version at all
    ({"schema_version": 0}, True),
    ({"schema_version": -1}, True),
    ({"schema_version": True}, True),  # bool is not a usable version here
    ({"schema_version": "1"}, True),
])
def test_from_dict_rejects_unusable_payloads(payload, expect_none):
    assert TaskKnowledge.from_dict(payload) is None


def test_from_dict_tolerates_missing_and_unknown_fields():
    old = TaskKnowledge.from_dict({"schema_version": 1, "task_id": "t", "goal": "g"})
    assert old.goal == "g" and old.constraints == [] and old.source_turn == 0
    assert old.verification == {"status": None, "command": None, "failures": []}
    future = TaskKnowledge.from_dict({"schema_version": 2, "goal": "g", "brand_new_field": [1]})
    assert future is not None and future.schema_version == 2 and future.goal == "g"


def test_from_dict_tolerates_malformed_nested_fields():
    k = TaskKnowledge.from_dict({
        "schema_version": 1,
        "constraints": "not-a-list",
        "verification": "PASSED: /x",
        "source_turn": True,
        "goal": 42,  # coerced to str
    })
    assert k is not None
    assert k.constraints == [] and k.verification["status"] is None
    assert k.source_turn == 0 and k.goal == "42"
    k2 = TaskKnowledge.from_dict({"schema_version": 1, "verification": {"failures": "nope"}})
    assert k2.verification["failures"] == []


# ------------------------------------------------------------------ tail / git
def test_tail_bounds_and_marks_truncation():
    from maestro.knowledge import _tail

    assert _tail("short", 100) == "short"
    long = "x" * 50
    out = _tail(long, 10)
    assert out.endswith("x" * 10) and "[truncated 40 chars]" in out


def _git_env() -> dict:
    return dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def test_git_changed_files_tracked_and_untracked(tmp_path):
    from maestro.knowledge import _git_changed_files

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True, env=_git_env())
    (ws / "a.py").write_text("a\n")
    subprocess.run(["git", "-C", str(ws), "add", "."], check=True, env=_git_env())
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "c1"], check=True, env=_git_env())
    (ws / "a.py").write_text("aa\n")  # tracked modification
    (ws / "new.txt").write_text("n\n")  # untracked

    assert _git_changed_files(ws) == ["a.py", "new.txt"]


@pytest.mark.parametrize("workspace", [None, "missing-dir"])
def test_git_changed_files_degrades_to_empty(tmp_path, workspace):
    from maestro.knowledge import _git_changed_files

    ws = tmp_path / workspace if isinstance(workspace, str) else None
    assert _git_changed_files(ws) == []


def test_git_changed_files_repo_without_commits_and_git_failure(tmp_path, monkeypatch):
    from maestro.knowledge import _git_changed_files

    bare = tmp_path / "bare"  # a directory that is not a git repo: rc != 0 on both calls
    bare.mkdir()
    (bare / "f.txt").write_text("x\n")
    assert _git_changed_files(bare) == []

    no_git_repo = tmp_path / "nogit"
    no_git_repo.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))  # git itself missing -> OSError
    assert _git_changed_files(no_git_repo) == []


# ------------------------------------------------------------------ verification claim
def _write_report(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_verification_claim_full_failure(tmp_path):
    from maestro.knowledge import _parse_verification_claim

    report = _write_report(tmp_path / "v.txt", "verification command: pytest -q\nFAILED tests/a.py::t1 (boom)\nFAILED tests/b.py::t2 (bang)\nok line\n")
    section = _parse_verification_claim(f"FAILED: {report}")
    assert section == {"status": "FAILED", "command": "pytest -q", "failures": ["FAILED tests/a.py::t1 (boom)", "FAILED tests/b.py::t2 (bang)"]}


def test_parse_verification_claim_caps_failures_at_20(tmp_path):
    from maestro.knowledge import _parse_verification_claim

    lines = "\n".join(f"FAILED t{i}" for i in range(25))
    report = _write_report(tmp_path / "v.txt", lines + "\n")
    section = _parse_verification_claim(f"FAILED: {report}")
    assert len(section["failures"]) == 20 and section["failures"][0] == "FAILED t0"


def test_parse_verification_claim_failed_without_failure_lines_keeps_tail(tmp_path):
    from maestro.knowledge import _parse_verification_claim

    report = _write_report(tmp_path / "v.txt", "no failure markers here, just prose\n" + "z" * 600)
    section = _parse_verification_claim(f"FAILED: {report}")
    assert len(section["failures"]) == 1 and "[truncated" in section["failures"][0]


def test_parse_verification_claim_passed_and_edge_claims(tmp_path):
    from maestro.knowledge import _parse_verification_claim

    report = _write_report(tmp_path / "v.txt", "verification command: make test\nall green\n")
    assert _parse_verification_claim(f"PASSED: {report}") == {"status": "PASSED", "command": "make test", "failures": []}
    assert _parse_verification_claim(None) == {"status": None, "command": None, "failures": []}
    assert _parse_verification_claim("") == {"status": None, "command": None, "failures": []}
    assert _parse_verification_claim("PASSED") == {"status": "PASSED", "command": None, "failures": []}  # no report part
    assert _parse_verification_claim(f"PASSED: {tmp_path / 'missing.txt'}")["command"] is None  # report missing


def test_parse_verification_claim_unreadable_report(tmp_path, monkeypatch):
    import pathlib

    import maestro.knowledge as knowledge

    from maestro.knowledge import _parse_verification_claim

    report = tmp_path / "v.txt"
    report.write_text("verification command: x\n")

    class _BrokenPath(pathlib.PosixPath):
        def read_text(self, *a, **kw):
            raise OSError("simulated unreadable file")

    monkeypatch.setattr(knowledge, "Path", _BrokenPath)
    section = _parse_verification_claim(f"PASSED: {report}")
    assert section["status"] == "PASSED" and section["command"] is None


# ------------------------------------------------------------------ known issues / summary
def test_known_issues_gates_attempts_dedup_and_cap():
    from maestro.knowledge import _known_issues

    claims = {"task_gates": json.dumps({"verdicts": {
        "reviewer": {"ok": False, "issues": ["bug A", "bug B"]},
        "flaky": "not-a-dict",
    }})}
    runtime = {"attempts": [
        {"agent": "codex", "ok": True},
        {"agent": "codex", "ok": False, "error": "first line of error\nsecond line"},
        {"agent": "codex", "ok": False, "error": "bug A"},  # duplicate of a gate issue? no: different text; dedup below
        "not-a-dict",
        {"agent": "codex", "ok": False, "error": "   "},
    ]}
    issues = _known_issues(claims, runtime)
    assert issues == ["reviewer: bug A", "reviewer: bug B", "first line of error", "bug A"]

    many = {"task_gates": json.dumps({"verdicts": {f"r{i}": {"issues": [f"i{i}"]} for i in range(12)}})}
    assert len(_known_issues(many, {})) == 10  # capped


def test_known_issues_malformed_gates_and_empty_errors():
    from maestro.knowledge import _known_issues

    assert _known_issues({"task_gates": "{not json"}, {"attempts": []}) == []
    assert _known_issues({"task_gates": json.dumps([1, 2])}, {}) == []  # parsed but not a dict
    assert _known_issues({"task_gates": json.dumps({"verdicts": "nope"})}, {}) == []


def test_latest_summary_precedence(tmp_path):
    from maestro.knowledge import _latest_summary

    out = tmp_path / "out.log"
    out.write_text("S" * 2000)
    assert _latest_summary({"result": {"output_path": str(out)}}, "working") == ("...[truncated 500 chars] " + "S" * 1500)

    missing = tmp_path / "gone.log"
    err_tail = _latest_summary({"result": {"output_path": str(missing)}, "attempts": [
        {"ok": True},
        {"ok": False, "error": "E" * 2000},
    ]}, "working")
    assert err_tail.endswith("E" * 800) and "[truncated 1200 chars]" in err_tail

    assert _latest_summary({}, "completed") == "completed"
    assert _latest_summary({}, "") == "no execution recorded"


def test_latest_summary_failed_attempt_without_error_text():
    from maestro.knowledge import _latest_summary

    # A failed attempt whose error is only whitespace contributes nothing; the
    # projection falls back to the state.
    assert _latest_summary({"attempts": [{"ok": False, "error": "   "}]}, "failed") == "failed"


# ------------------------------------------------------------------ projection
def test_project_knowledge_full(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True, env=_git_env())
    (ws / "m.py").write_text("m\n")
    subprocess.run(["git", "-C", str(ws), "add", "."], check=True, env=_git_env())
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "c"], check=True, env=_git_env())
    (ws / "m.py").write_text("mm\n")

    claims = {
        "task_request": "Implement OAuth",
        "task_status": "REVIEWING",
        "task_branch": "maestro/task-1",
        "task_gates": json.dumps({"verdicts": {"reviewer": {"issues": ["bug"]}}}),
    }
    runtime = {
        "state": "working",
        "turn": 3,
        "branch": "fallback-branch",
        "attempts": [{"agent": "codex", "ok": True}],
        "doc": {"expectations": {"verification": "auto", "commit_policy": "branch"}, "constraints": {"sensitive": True}},
    }
    k = project_knowledge("task-1", claims, runtime, workspace=ws)
    assert k.task_id == "task-1" and k.goal == "Implement OAuth"
    assert k.constraints == ["verification=auto", "commit_policy=branch", "sensitive workspace (approval-gated)"]
    assert k.files_changed == ["m.py"]
    assert k.current_state == "working, branch maestro/task-1, turn 3, last agent codex"
    assert k.verification["status"] is None
    assert k.known_issues == ["reviewer: bug"]
    assert k.source_turn == 3 and k.last_updated.startswith("20")


def test_project_knowledge_degraded_inputs():
    k = project_knowledge("t", {}, "not-a-dict")  # non-dict runtime tolerated
    assert k.goal == "" and k.constraints == [] and k.files_changed == []
    assert k.current_state == "unknown" and k.source_turn == 0
    assert k.latest_summary == "unknown"

    claims = {"task_status": "COMPLETE"}  # no task_request -> empty goal; phase fallback map
    runtime = {"turn": True, "attempts": ["junk"], "branch": "fb", "doc": "not-a-dict"}
    k2 = project_knowledge("t", claims, runtime)
    assert k2.current_state == "completed, branch fb"  # REVIEWING/COMPLETE -> completed; bool turn ignored
    assert k2.latest_summary == "completed"


def test_project_knowledge_phase_fallback_and_branch_sources():
    k = project_knowledge("t", {"task_status": "FAILED"}, {"state": None})
    assert k.current_state.startswith("failed")  # claim-driven state when runtime lacks one
    k2 = project_knowledge("t", {}, {"branch": "runtime-branch"})
    assert "branch runtime-branch" in k2.current_state


def test_project_knowledge_attempt_without_agent_name():
    k = project_knowledge("t", {}, {"state": "completed", "attempts": [{"ok": True}]})
    assert k.current_state == "completed"  # no agent name -> no "last agent" part


# ------------------------------------------------------------------ rendering
def _full_knowledge() -> TaskKnowledge:
    return TaskKnowledge(
        task_id="task-1", goal="Do the thing", constraints=["verification=auto"],
        files_changed=["a.py"], current_state="completed, branch b, turn 2",
        verification={"status": "FAILED", "command": "pytest -q", "failures": ["FAILED tests/t.py::x"]},
        known_issues=["reviewer: bug"], latest_summary="tail of output", source_turn=2,
    )


def test_render_section_order_and_content():
    block = render_continuation_block(_full_knowledge(), 10_000)
    assert block.startswith("TASK KNOWLEDGE")
    order = [block.index(marker) for marker in ("GOAL:", "CURRENT STATE:", "VERIFICATION: FAILED", "KNOWN ISSUES:", "CONSTRAINTS:", "LATEST SUMMARY")]
    assert order == sorted(order)
    assert "pytest -q" in block and "a.py" in block


def test_render_empty_and_degenerate_knowledge():
    assert render_continuation_block(TaskKnowledge(task_id="t"), 10_000) == ""
    degenerate = TaskKnowledge(task_id="t", verification={"status": None, "command": None, "failures": []})
    assert render_continuation_block(degenerate, 10_000) == ""


def test_render_budget_floor_and_exact_fit():
    full = render_continuation_block(_full_knowledge(), 10_000)
    assert "(continuation context budget exceeded" not in full
    assert render_continuation_block(_full_knowledge(), len(full)) == full  # exact fit, no note
    assert render_continuation_block(_full_knowledge(), 10) == ""  # header alone does not fit


def test_render_truncation_drops_low_value_sections_and_marks():
    k = TaskKnowledge(task_id="t", goal="G" * 800, latest_summary="S" * 800)
    for budget in (1200, 700, 400, 300):
        block = render_continuation_block(k, budget)
        assert len(block) <= budget, (budget, len(block))
    full = render_continuation_block(k, 5000)
    assert "(continuation context budget exceeded" not in full and "S" * 800 in full
    tight = render_continuation_block(k, 1200)
    assert "[truncated to fit continuation budget]" in tight
    assert "truncated or omitted: latest_summary" in tight  # goal kept; summary truncated + listed


def test_render_tiny_budgets_degrade_to_minimal_marker():
    k = TaskKnowledge(task_id="t", goal="G" * 500)
    # header (98) + minimal marker fits, full note does not -> minimal marker
    block = render_continuation_block(k, 121)
    assert len(block) <= 121 and "[context truncated]" in block
    # pathological: only the header fits
    floor = render_continuation_block(k, 98)
    assert floor == "TASK KNOWLEDGE — compact continuation snapshot (raw history stays in the task record):\n"


def test_render_first_section_too_large():
    k = TaskKnowledge(task_id="t", goal="G" * 3000)
    block = render_continuation_block(k, 500)
    assert len(block) <= 500
    assert block.startswith("TASK KNOWLEDGE") and "GOAL:\nG" in block
    assert "[truncated to fit continuation budget]" in block


def test_render_cutoff_section_dropped_when_marker_does_not_fit():
    # The kept sections just fit, but the remaining room cannot hold the
    # truncation marker: the cutoff section is dropped whole and named in the
    # omission note instead of being truncated.
    k = TaskKnowledge(task_id="t", goal="G" * 100, constraints=["x"], latest_summary="S" * 100)
    block = render_continuation_block(k, 300)
    assert len(block) <= 300
    assert "GOAL:\n" + "G" * 100 in block and "CONSTRAINTS:" in block
    assert "[truncated to fit continuation budget]" not in block
    assert "truncated or omitted: latest_summary" in block
