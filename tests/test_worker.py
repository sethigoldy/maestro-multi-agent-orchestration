import json
from pathlib import Path
import pytest
from maestro.core import Maestro
from maestro.models import Phase
from maestro import worker

def test_run_codex_uses_model_effort_and_followup(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    m = Maestro(tmp_path)

    try:
        task = m.create_handoff(
            "T",
            "R",
            "DESIGN",
            model="gpt-5.6-luna",
            effort="max",
        )

        class R:
            returncode = 1
            stdout = ""
            stderr = ""

        calls = []
        monkeypatch.setattr(
            worker.subprocess,
            "run",
            lambda cmd, **kw: calls.append(cmd) or R(),
        )

        assert worker.run_codex(
            m,
            task["task_id"],
            "followup",
            "fix it",
        ) == 1

        cmd = calls[-1]  # calls[0] is the flag-surface probe

        assert "--model" in cmd
        assert "gpt-5.6-luna" in cmd
        assert "--config" in cmd
        assert 'model_reasoning_effort="max"' in cmd

        payload = json.loads(
            (
                home
                / "tasks"
                / task["task_id"]
                / "codex-followup-result.json"
            ).read_text()
        )

        assert payload["mode"] == "followup"

    finally:
        m.close()

def test_verification_command(tmp_path: Path):
    cmd,note=worker._verification_command(tmp_path)
    assert cmd==['git','diff','--check'] and note

def test_python_executable(tmp_path: Path):
    v=tmp_path/'.venv'/'bin'; v.mkdir(parents=True); py=v/'python'; py.write_text('x'); py.chmod(0o755)
    assert worker._python_executable(tmp_path)==str(py)
