from pathlib import Path

from maestro import worker


def test_verification_command(tmp_path: Path):
    cmd, note = worker._verification_command(tmp_path)
    assert cmd == ['git', 'diff', '--check'] and note


def test_python_executable(tmp_path: Path):
    v = tmp_path / '.venv' / 'bin'
    v.mkdir(parents=True)
    py = v / 'python'
    py.write_text('x')
    py.chmod(0o755)
    assert worker._python_executable(tmp_path) == str(py)


def test_python_executable_falls_back_to_maestro_interpreter(tmp_path: Path, monkeypatch):
    import sys

    monkeypatch.delenv('MAESTRO_PYTHON', raising=False)
    assert worker._python_executable(tmp_path) == sys.executable


def test_verification_command_node_lockfiles_and_other_runners(tmp_path: Path):
    import json

    (tmp_path / 'package.json').write_text(json.dumps({'scripts': {'test': 'jest'}}))
    (tmp_path / 'pnpm-lock.yaml').write_text('')
    assert worker._verification_command(tmp_path)[0] == ['pnpm', 'test']
    (tmp_path / 'pnpm-lock.yaml').unlink()
    (tmp_path / 'yarn.lock').write_text('')
    assert worker._verification_command(tmp_path)[0] == ['yarn', 'test']
    (tmp_path / 'yarn.lock').unlink()
    # A package.json that is not valid JSON is ignored, and detection continues.
    (tmp_path / 'package.json').write_text('{not json')
    (tmp_path / 'Cargo.toml').write_text('[package]\n')
    assert worker._verification_command(tmp_path)[0] == ['cargo', 'test']


def test_python_executable_falls_back_to_the_main_repository_venv(tmp_path, monkeypatch):
    from maestro import worker, worktrees

    monkeypatch.delenv("MAESTRO_PYTHON", raising=False)
    main = tmp_path / "main"
    venv_python = main / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_python.chmod(0o755)
    run_dir = tmp_path / "wt"
    run_dir.mkdir()
    monkeypatch.setattr(worktrees, "main_repo_root", lambda path: main)
    assert worker._python_executable(run_dir) == str(venv_python)
