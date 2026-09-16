from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from maestro.core import Maestro
from maestro.models import Phase


def test_phase_enum_values():
    assert {p.value for p in Phase} == {
        "DESIGNED", "IMPLEMENTING", "VERIFYING", "REVIEWING", "FIXING", "COMPLETE", "FAILED"
    }


def test_workspace_and_git_root(tmp_path: Path):
    with pytest.raises(ValueError, match="Workspace does not exist"):
        Maestro(tmp_path / "missing")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    nested = tmp_path / "nested"
    nested.mkdir()
    assert Maestro.git_root(nested) == tmp_path.resolve()


def test_git_root_failure(tmp_path: Path):
    with pytest.raises(ValueError, match="not a Git repository"):
        Maestro.git_root(tmp_path)


def test_task_lock_and_basic_handoff(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        with m._task_lock():
            pass
        task = m.create_handoff("Test", "Do it", "# Design")
        assert task["task_number"] == 1
        assert m.resolve_task("1") == task["task_id"]
        assert m.resolve_task(task["task_id"]) == task["task_id"]
        assert m.status("1")["phase"] == Phase.DESIGNED.value
        assert m.status("1")["workspace"] == str(tmp_path.resolve())
        assert m.list_tasks()[0]["title"] == "Test"
    finally:
        m.close()


def test_task_numbering_and_index_persistence(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        a = m.create_handoff("A", "A", "A")
        b = m.create_handoff("B", "B", "B")
        assert [x["number"] for x in m.list_tasks()] == [1, 2]
        assert m.status(b["task_id"])["task_number"] == 2
        with pytest.raises(KeyError, match="Unknown task number 99.*1, 2"):
            m.status("99")
        with pytest.raises(KeyError, match="Unknown task reference 'nope'"):
            m.resolve_task("nope")
        with pytest.raises(KeyError, match="Unknown task reference 'task-missing'"):
            m.resolve_task("task-missing")
        assert a["task_id"].startswith("task-")
    finally:
        m.close()


def test_config_defaults_and_invalid_values(tmp_path: Path, monkeypatch):
    state = tmp_path / ".maestro"
    state.mkdir()
    (state / "config.toml").write_text('[codex]\nmodel="cfg-model"\neffort="HIGH"\n', encoding="utf-8")
    m = Maestro(tmp_path)
    try:
        assert m.codex_defaults() == {"model": "cfg-model", "effort": "high"}
    finally:
        m.close()

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".maestro").mkdir()
    (bad / ".maestro" / "config.toml").write_text('[codex]\neffort="bogus"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported Codex reasoning effort"):
        Maestro(bad)

    env = tmp_path / "env"
    env.mkdir()
    monkeypatch.setenv("MAESTRO_CODEX_MODEL", "env-model")
    monkeypatch.setenv("MAESTRO_CODEX_EFFORT", "xHIGH")
    m2 = Maestro(env)
    try:
        assert m2.codex_defaults() == {"model": "env-model", "effort": "xhigh"}
    finally:
        m2.close()


def test_invalid_config_is_ignored(tmp_path: Path):
    state = tmp_path / ".maestro"
    state.mkdir()
    (state / "config.toml").write_text("not = [valid", encoding="utf-8")
    m = Maestro(tmp_path)
    try:
        assert m.codex_defaults() == {"model": None, "effort": None}
    finally:
        m.close()


def test_legacy_index_migration(tmp_path: Path):
    state = tmp_path / ".maestro"
    state.mkdir()
    task_id = "task-legacy-001"
    (state / "tasks.json").write_text(json.dumps([
        {"number": 7, "task_id": task_id, "title": "Legacy", "created_at": "2026-09-16T00:00:00+00:00"},
        {"number": "bad", "task_id": "bad-number"},
        {"task_id": "no-number"},
    ]), encoding="utf-8")
    m = Maestro(tmp_path)
    try:
        assert m.resolve_task("7") == task_id
        assert m._registry_records()[0]["title"] == "Legacy"
    finally:
        m.close()


def test_corrupt_and_non_list_index(tmp_path: Path):
    state = tmp_path / ".maestro"
    state.mkdir()
    (state / "tasks.json").write_text("{bad", encoding="utf-8")
    m = Maestro(tmp_path)
    try:
        assert m._load_index() == []
    finally:
        m.close()
    (state / "tasks.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    m2 = Maestro(tmp_path)
    try:
        assert m2._load_index() == []
    finally:
        m2.close()


def test_registry_bad_claims_are_ignored(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        m.mem.remember(m._REGISTRY_SUBJECT, m._REGISTRY_PREDICATE, "not-json")
        m.mem.remember(m._REGISTRY_SUBJECT, m._REGISTRY_PREDICATE, json.dumps({"task_id": "x"}))
        m.mem.remember(m._REGISTRY_SUBJECT, m._REGISTRY_PREDICATE, json.dumps({"task_id": "x", "number": "bad"}))
        m.mem.remember(m._REGISTRY_SUBJECT, m._REGISTRY_PREDICATE, json.dumps({"task_id": "x", "number": 3, "title": "ok"}))
        assert m._registry_records()[0]["number"] == 3
    finally:
        m.close()


def test_recovery_from_live_claims(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        m.mem.remember("maestro:task:t1", "task_number", "4")
        m.mem.remember("maestro:task:t1", "task_title", "One")
        m.mem.remember("maestro:task:t1", "task_workspace", "/w1")
        m.mem.remember("maestro:task:t1", "task_design", "/d1")
        m.mem.remember("maestro:task:t2", "task_title", "Two")
        m.mem.remember("other", "task_number", "99")
        recovered = m._recover_from_live_claims()
        assert [x["number"] for x in recovered] == [4, 5]
        assert recovered[0]["workspace"] == "/w1"
        assert m.resolve_task("5") == "t2"
    finally:
        m.close()


def test_stage_validation_and_round_trip(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="must be under"):
            m._stage_path(outside)
        stage = m.staged_dir / "handoff.json"
        with pytest.raises(ValueError, match="does not exist"):
            m._load_staged_handoff(stage)
        stage.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON object"):
            m._load_staged_handoff(stage)
        stage.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="missing required field: title"):
            m._load_staged_handoff(stage)
        stage.write_text(json.dumps({"title": "T", "request": "R", "design_file": "missing.md"}), encoding="utf-8")
        with pytest.raises(ValueError, match="does not exist"):
            m._load_staged_handoff(stage)

        design = m.staged_dir / "feature.md"
        design.write_text("# design", encoding="utf-8")
        payload = {"title": "T", "request": "R", "design_file": str(design.relative_to(tmp_path)), "model": "m", "effort": "max"}
        stage.write_text(json.dumps(payload), encoding="utf-8")
        task = m.create_handoff_from_file(stage)
        assert task["task_number"] == 1
        assert json.loads(stage.read_text())["task_id"] == task["task_id"]
        retry = m.create_handoff_from_file(stage)
        assert retry["task_id"] == task["task_id"]
        destination = m.finalize_staged_handoff(stage, task["task_id"])
        assert Path(destination).is_file()
        assert m.finalize_staged_handoff(stage, task["task_id"]).endswith("handoff.json")
    finally:
        m.close()


def test_stage_design_outside_workspace_and_invalid_descriptor(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        design = tmp_path.parent / "outside.md"
        design.write_text("d", encoding="utf-8")
        stage = m.staged_dir / "x.json"
        stage.write_text(json.dumps({"title": "T", "request": "R", "design_file": str(design)}), encoding="utf-8")
        with pytest.raises(ValueError, match="inside the active workspace"):
            m._load_staged_handoff(stage)
        stage.write_text(json.dumps({"title": "T", "request": "R", "design_file": "x.md"}), encoding="utf-8")
        with pytest.raises(ValueError, match="does not exist"):
            m._load_staged_handoff(stage)
    finally:
        m.close()


def test_handoff_with_overrides_and_invalid_effort(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff("T", "R", "D", model="custom", effort="MAX")
        assert task["model"] == "custom"
        assert task["effort"] == "max"
        with pytest.raises(ValueError, match="Unsupported"):
            m.create_handoff("T2", "R", "D", effort="bad")
    finally:
        m.close()


def test_launch_implement_fix_and_review(monkeypatch, tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff("T", "R", "D")
        commands = []

        class Proc:
            pid = 4321

        def fake_popen(cmd, **kwargs):
            commands.append((cmd, kwargs))
            return Proc()

        monkeypatch.setattr("maestro.core.subprocess.Popen", fake_popen)
        launched = m.implement_async(task["task_id"])
        fixed = m.fix_async(task["task_id"], "please fix")
        assert launched["pid"] == 4321 and fixed["action"] == "fix"
        assert any("--review" in cmd for cmd, _ in commands)
        assert m.status(task["task_id"])["phase"] == Phase.FIXING.value
        approved = m.review(task["task_id"], "looks good", True)
        assert approved["phase"] == Phase.COMPLETE.value
        rejected = m.review(task["task_id"], "needs work", False)
        assert rejected["phase"] == Phase.FIXING.value
        assert (tmp_path / ".maestro" / "tasks" / task["task_id"] / "implement.log").exists()
    finally:
        m.close()



def test_task_lock_tolerates_fcntl_errors(monkeypatch, tmp_path: Path):
    import sys
    import types
    fake = types.SimpleNamespace(flock=lambda *args: (_ for _ in ()).throw(OSError("lock unavailable")), LOCK_EX=1, LOCK_UN=2)
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    m = Maestro(tmp_path)
    try:
        with m._task_lock():
            pass
    finally:
        m.close()


def test_registry_history_failure_and_non_dict_claim(tmp_path: Path, monkeypatch):
    m = Maestro(tmp_path)
    try:
        original = m.mem.history
        def boom(subject, predicate):
            if subject == m._REGISTRY_SUBJECT:
                raise RuntimeError("history down")
            return original(subject, predicate)
        monkeypatch.setattr(m.mem, "history", boom)
        assert m._registry_records() == []
    finally:
        m.close()


def test_registry_skips_missing_task_id_and_duplicates(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        m.mem.remember(m._REGISTRY_SUBJECT, m._REGISTRY_PREDICATE, json.dumps({"number": 1, "title": "missing"}))
        m.mem.remember(m._REGISTRY_SUBJECT, m._REGISTRY_PREDICATE, json.dumps({"task_id": "dup", "number": 2, "title": "old"}))
        m.mem.remember(m._REGISTRY_SUBJECT, m._REGISTRY_PREDICATE, json.dumps({"task_id": "dup", "number": 3, "title": "new"}))
        records = m._registry_records()
        assert len(records) == 1 and records[0]["number"] == 3
    finally:
        m.close()


def test_legacy_migration_duplicate_and_empty(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        m._write_registry_record(m._registry_record("known", 1, "Known"))
        result = m._migrate_legacy_index([
            {"task_id": "known", "number": 99, "title": "Duplicate"},
            {"task_id": "new", "number": 2, "title": "New"},
            {"task_id": "bad", "number": "x"},
            {"number": 3, "title": "No ID"},
        ])
        assert [x["task_id"] for x in result] == ["known", "new"]
        assert m._migrate_legacy_index([]) == result
    finally:
        m.close()


def test_live_claim_recovery_failure_and_invalid_number(tmp_path: Path, monkeypatch):
    m = Maestro(tmp_path)
    try:
        original = m.mem.get_all
        class BadClaim:
            subject = "maestro:task:tbad"
            predicate = "task_number"
            object = "not-int"
        original_claims = original()
        m.mem.remember("maestro:task:t1", "task_title", "One")
        claims = original() + [BadClaim()]
        monkeypatch.setattr(m.mem, "get_all", lambda: claims)
        recovered = m._recover_from_live_claims()
        assert recovered
        def get_all_boom(): raise RuntimeError("get all down")
        monkeypatch.setattr(m.mem, "get_all", get_all_boom)
        assert m._recover_from_live_claims() == []
    finally:
        m.close()


def test_register_task_allocates_number_when_omitted(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        assert m._register_task("task-direct", "Direct") == 1
    finally:
        m.close()


def test_resolve_task_history_fallback_and_failure(tmp_path: Path, monkeypatch):
    m = Maestro(tmp_path)
    try:
        original = m.mem.history
        m.mem.history = lambda subject, predicate: [object()] if subject.endswith("ghost") else original(subject, predicate)
        assert m.resolve_task("task-ghost") == "task-ghost"
        def boom(subject, predicate):
            if subject.endswith("blocked"):
                raise RuntimeError("history down")
            return original(subject, predicate)
        m.mem.history = boom
        with pytest.raises(KeyError, match="Unknown task reference 'task-blocked'"):
            m.resolve_task("task-blocked")
    finally:
        m.close()


def test_list_tasks_skips_missing_status(tmp_path: Path, monkeypatch):
    m = Maestro(tmp_path)
    try:
        m.create_handoff("A", "A", "A")
        m.create_handoff("B", "B", "B")
        original = m.status
        second = m.list_tasks if False else None
        task_ids = [x["task_id"] for x in m._index_items()]
        def status(ref):
            if str(ref) == task_ids[1]:
                raise KeyError("gone")
            return original(ref)
        m.status = status
        assert len(m.list_tasks()) == 1
    finally:
        m.close()


def test_invalid_staged_json(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        path = m.staged_dir / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid handoff file"):
            m._load_staged_handoff(path)
    finally:
        m.close()



def test_recovery_skips_empty_task_id(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        class EmptyClaim:
            subject = "maestro:task:"
            predicate = "task_title"
            object = "ignored"
        original = m.mem.get_all
        m.mem.get_all = lambda: [EmptyClaim(), *original()]
        assert m._recover_from_live_claims() == []
    finally:
        m.close()


def test_stage_path_accepts_relative_path(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        stage = m.staged_dir / "relative.json"
        assert m._stage_path(str(stage.relative_to(tmp_path))) == stage.resolve()
    finally:
        m.close()


def test_legacy_migration_empty_has_false_branch(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        assert m._migrate_legacy_index([]) == []
    finally:
        m.close()


def test_recovery_ignores_unknown_predicate(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        class UnknownClaim:
            subject = "maestro:task:t1"
            predicate = "unrelated"
            object = "ignored"
        m.mem.get_all = lambda: [UnknownClaim()]
        recovered = m._recover_from_live_claims()
        assert recovered and recovered[0]["task_id"] == "t1"
    finally:
        m.close()
