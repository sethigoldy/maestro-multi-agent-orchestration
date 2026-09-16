from pathlib import Path

from maestro.core import Maestro
from maestro.models import Phase


def test_phase_lifecycle_names():
    assert Phase.DESIGNED.value == "DESIGNED"
    assert Phase.IMPLEMENTING.value == "IMPLEMENTING"
    assert Phase.VERIFYING.value == "VERIFYING"
    assert Phase.REVIEWING.value == "REVIEWING"
    assert Phase.FIXING.value == "FIXING"
    assert Phase.COMPLETE.value == "COMPLETE"


def test_numbered_task_resolution(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff("Test", "Do it", "# Design\n\nhello")
        assert task["task_number"] == 1
        assert m.resolve_task("1") == task["task_id"]
        assert m.status("1")["task_number"] == 1
        assert m.status("1")["phase"] == Phase.DESIGNED.value
        assert m.status("1")["workspace"] == str(tmp_path.resolve())
    finally:
        m.close()


def test_list_tasks(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        a = m.create_handoff("A", "A", "A")
        b = m.create_handoff("B", "B", "B")
        tasks = m.list_tasks()
        assert [x["number"] for x in tasks] == [1, 2]
        assert tasks[0]["task_id"] == a["task_id"]
        assert tasks[1]["task_id"] == b["task_id"]
    finally:
        m.close()


def test_workspace_resolution_rejects_non_git(tmp_path: Path):
    import pytest
    with pytest.raises(ValueError, match="not a Git repository"):
        Maestro.git_root(tmp_path)


def test_workspace_resolution_uses_git_root(tmp_path: Path):
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    nested = tmp_path / "nested"
    nested.mkdir()
    assert Maestro.git_root(nested) == tmp_path.resolve()


def test_codex_model_and_effort_are_persisted(tmp_path):
    (tmp_path / ".maestro").mkdir()
    (tmp_path / ".maestro" / "config.toml").write_text('[codex]\nmodel = "gpt-test"\neffort = "high"\n')
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff("T", "R", "D")
        status = m.status(task["task_id"])
        assert status["model"] == "gpt-test"
        assert status["effort"] == "high"
    finally:
        m.close()



def test_registry_survives_deleted_index(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff("Durable", "Keep it", "D")
        m.close()
        (tmp_path / ".maestro" / "tasks.json").unlink()
        m2 = Maestro(tmp_path)
        try:
            assert m2.list_tasks()[0]["task_id"] == task["task_id"]
            assert m2.status("1")["title"] == "Durable"
        finally:
            m2.close()
    finally:
        try:
            m.close()
        except Exception:
            pass



def test_legacy_index_migrates_into_memvara(tmp_path: Path):
    state = tmp_path / ".maestro"
    state.mkdir()
    task_id = "task-legacy-001"
    (state / "tasks.json").write_text(
        '[{"number": 7, "task_id": "%s", "title": "Legacy", "created_at": "2026-09-16T00:00:00+00:00"}]' % task_id,
        encoding="utf-8",
    )
    m = Maestro(tmp_path)
    try:
        assert m.resolve_task("7") == task_id
        assert m._registry_records()[0]["title"] == "Legacy"
    finally:
        m.close()


def test_unknown_numeric_task_has_friendly_error(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        import pytest
        with pytest.raises(KeyError, match="Unknown task number 6"):
            m.status("6")
    finally:
        m.close()


def test_codex_max_effort_is_supported(tmp_path: Path):
    state = tmp_path / ".maestro"
    state.mkdir()
    (state / "config.toml").write_text(
        '[codex]\nmodel = "gpt-5.6-luna"\neffort = "max"\n',
        encoding="utf-8",
    )
    m = Maestro(tmp_path)
    try:
        assert m.codex_defaults() == {"model": "gpt-5.6-luna", "effort": "max"}
        task = m.create_handoff("Luna Max", "R", "D")
        status = m.status(task["task_id"])
        assert status["model"] == "gpt-5.6-luna"
        assert status["effort"] == "max"
    finally:
        m.close()


def test_staged_handoff_round_trip(tmp_path: Path):
    stage = tmp_path / ".maestro" / "staged"
    stage.mkdir(parents=True)
    design = stage / "feature.md"
    design.write_text("# Feature\n\nDo the thing.", encoding="utf-8")
    descriptor = stage / "handoff.json"
    descriptor.write_text(
        __import__("json").dumps({
            "title": "Feature",
            "request": "Implement it",
            "design_file": str(design.relative_to(tmp_path)),
            "model": "gpt-5.6-luna",
            "effort": "max",
        }),
        encoding="utf-8",
    )
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff_from_file(descriptor)
        assert task["task_number"] == 1
        assert task["model"] == "gpt-5.6-luna"
        assert task["effort"] == "max"
        assert descriptor.exists()
        # A retry sees the same staged task and does not create a duplicate.
        retry = m.create_handoff_from_file(descriptor)
        assert retry["task_id"] == task["task_id"]
        assert m.list_tasks()[0]["number"] == 1
    finally:
        m.close()
