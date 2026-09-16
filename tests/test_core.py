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
