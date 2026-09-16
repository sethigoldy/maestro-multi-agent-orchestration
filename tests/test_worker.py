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


def test_verification_command_selection(tmp_path: Path, monkeypatch):
    cmd, note = worker._verification_command(tmp_path)
    assert cmd == ["git", "diff", "--check"] and note is not None
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs: type("R", (), {"returncode": 0})())
    cmd, note = worker._verification_command(tmp_path)
    assert cmd[1:3] == ["-m", "pytest"] and note is None
    make_root = tmp_path / "make"; make_root.mkdir(); (make_root / "Makefile").write_text("check:\n\ttrue\n")
    cmd, note = worker._verification_command(make_root)
    assert cmd == ["make", "check"] and note is None


def test_verification_command_config_and_common_runners(tmp_path: Path):
    assert worker._verification_command(tmp_path, ["./check.sh"])[0] == ["./check.sh"]
    node = tmp_path / "node"; node.mkdir()
    (node / "package.json").write_text('{"scripts":{"test":"jest"}}')
    assert worker._verification_command(node)[0] == ["npm", "test"]
    (node / "pnpm-lock.yaml").write_text("")
    assert worker._verification_command(node)[0] == ["pnpm", "test"]
    yarn = tmp_path / "yarn"; yarn.mkdir()
    (yarn / "package.json").write_text('{"scripts":{"test":"jest"}}')
    (yarn / "yarn.lock").write_text("")
    assert worker._verification_command(yarn)[0] == ["yarn", "test"]
    go = tmp_path / "go"; go.mkdir(); (go / "go.mod").write_text("module x")
    assert worker._verification_command(go)[0] == ["go", "test", "./..."]
    cargo = tmp_path / "cargo"; cargo.mkdir(); (cargo / "Cargo.toml").write_text("[package]")
    assert worker._verification_command(cargo)[0] == ["cargo", "test"]
    bad_node = tmp_path / "badnode"; bad_node.mkdir(); (bad_node / "package.json").write_text('{')
    assert worker._verification_command(bad_node)[0] == ["git", "diff", "--check"]


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


def test_verification_selection_without_pytest(tmp_path: Path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs: type("R", (), {"returncode": 1})())
    cmd, note = worker._verification_command(tmp_path)
    assert cmd == ["git", "diff", "--check"]
    assert note and "pytest is not installed" in note


def test_verification_selection_package_without_test_script(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"scripts":{"build":"x"}}')
    cmd, note = worker._verification_command(tmp_path)
    assert cmd == ["git", "diff", "--check"]
    assert note and "no project test runner" in note
