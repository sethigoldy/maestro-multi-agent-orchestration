from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro.core import Maestro
from maestro.models import Phase
from maestro import worker


def test_now_and_python_executable(tmp_path: Path, monkeypatch):
    ts = worker.now()
    assert ts.tzinfo is not None
    venv = tmp_path / ".venv" / "bin"; venv.mkdir(parents=True)
    py = venv / "python"; py.write_text("x"); py.chmod(0o755)
    assert worker._python_executable(tmp_path) == str(py)
    monkeypatch.setenv("MAESTRO_PYTHON", str(py))
    assert worker._python_executable(tmp_path) == str(py)
    monkeypatch.setenv("MAESTRO_PYTHON", str(tmp_path / "missing"))
    monkeypatch.setattr(worker.sys, "executable", "/system/python")
    assert worker._python_executable(tmp_path) == str(py)


def test_verification_command_selection(tmp_path: Path):
    assert worker._verification_command(tmp_path) == ["git", "diff", "--check"]
    (tmp_path / "tests").mkdir()
    assert worker._verification_command(tmp_path)[1:3] == ["-m", "pytest"]
    make_root = tmp_path / "make"; make_root.mkdir(); (make_root / "Makefile").write_text("check:\n\ttrue\n")
    assert worker._verification_command(make_root) == ["make", "check"]


def test_design_for_and_claim_value(tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff("T", "R", "DESIGN")
        assert worker.design_for(m, task["task_id"]) == "DESIGN"
        assert worker._claim_value(m, task["task_id"], "task_design")
        assert worker._claim_value(m, task["task_id"], "missing") is None
        with pytest.raises(KeyError):
            worker.design_for(m, "task-missing")
    finally:
        m.close()


def test_run_codex_success_and_failure(monkeypatch, tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff("T", "R", "DESIGN", model="model-x", effort="max")
        class Result:
            def __init__(self, code, out="OUT", err="ERR"):
                self.returncode = code; self.stdout = out; self.stderr = err
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs)); return Result(0)
        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        assert worker.run_codex(m, task["task_id"], "implement", None) == 0
        payload = json.loads((tmp_path / ".maestro" / "tasks" / task["task_id"] / "codex-implement-result.json").read_text())
        assert payload["model"] == "model-x" and payload["effort"] == "max"
        assert m.status(task["task_id"])["phase"] == Phase.REVIEWING.value
        assert "--model" in calls[0][0] and "--config" in calls[0][0]

        def fake_verify(*_): return True
        monkeypatch.setattr(worker, "verify", fake_verify)
        calls.clear()
        def fake_run_fail(cmd, **kwargs): return Result(1, "bad", "oops")
        monkeypatch.setattr(worker.subprocess, "run", fake_run_fail)
        assert worker.run_codex(m, task["task_id"], "fix", "review me") == 1
        text = (tmp_path / ".maestro" / "tasks" / task["task_id"] / "codex-fix-result.json").read_text()
        assert "review me" not in text  # review is sent to codex prompt, not stored in result
    finally:
        m.close()


def test_verify_pass_and_fail(monkeypatch, tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff("T", "R", "D")
        class R:
            def __init__(self, code, out="", err=""): self.returncode=code; self.stdout=out; self.stderr=err
        monkeypatch.setattr(worker.subprocess, "run", lambda cmd, **kwargs: R(0, "ok", ""))
        assert worker.verify(m, task["task_id"]) is True
        assert m.status(task["task_id"])["phase"] == Phase.REVIEWING.value
        assert "PASSED" in m.status(task["task_id"])["verification"]
        monkeypatch.setattr(worker.subprocess, "run", lambda cmd, **kwargs: R(1, "", "failed"))
        assert worker.verify(m, task["task_id"]) is False
        assert "FAILED" in m.status(task["task_id"])["verification"]
    finally:
        m.close()



def test_worker_main(monkeypatch, tmp_path: Path):
    called = []
    class FakeMaestro:
        def __init__(self, workspace): called.append(("init", workspace))
        def resolve_task(self, task_id): called.append(("resolve", task_id)); return "resolved"
        def close(self): called.append(("close",))
    monkeypatch.setattr(worker, "Maestro", FakeMaestro)
    monkeypatch.setattr(worker, "run_codex", lambda m, task_id, action, review: called.append(("run", task_id, action, review)) or 7)
    monkeypatch.setattr(worker.sys, "argv", ["maestro.worker", "fix", "10", "--review", "needs", "--workspace", str(tmp_path)])
    assert worker.main() == 7
    assert ("resolve", "10") in called and ("run", "resolved", "fix", "needs") in called


def test_run_codex_without_model_or_effort(monkeypatch, tmp_path: Path):
    m = Maestro(tmp_path)
    try:
        task = m.create_handoff("T", "R", "D")
        class R:
            returncode = 1
            stdout = ""
            stderr = ""
        calls = []
        monkeypatch.setattr(worker.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd) or R())
        assert worker.run_codex(m, task["task_id"], "implement", None) == 1
        assert "--model" not in calls[0] and "--config" not in calls[0]
    finally:
        m.close()
