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
