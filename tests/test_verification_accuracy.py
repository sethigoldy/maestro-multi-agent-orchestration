"""Tests for the accuracy of automatic verification.

Each test here reproduces a case where automatic verification used to give the
wrong answer: a project whose tests were skipped because pytest was missing,
a Makefile without a ``check`` target, a Python project with no tests, and the
default ``npm init`` test script. Fake executables used here never write
files and never run git; they only print a message and exit.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from maestro import worker
from maestro.daemon import MaestroDaemon
from maestro.handoff import HandoffDoc

GIT_ENV = dict(
    os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"
)
FALLBACK = ["git", "diff", "--check"]


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(ws), *args], text=True, capture_output=True, env=GIT_ENV, check=True)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a committed git repository under tmp_path holding the given files."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    for name, text in files.items():
        path = ws / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "initial")
    return ws


def _python_without_pytest(tmp_path: Path) -> Path:
    """A fake interpreter that behaves like a Python with no pytest installed.

    It prints the same error a real interpreter prints and exits 1 for every
    invocation, including the import probe. It writes nothing anywhere.
    """
    bindir = tmp_path / "fakepy" / "bin"
    bindir.mkdir(parents=True)
    python = bindir / "python"
    python.write_text("#!/bin/sh\necho \"$0: No module named pytest\" >&2\nexit 1\n", encoding="utf-8")
    python.chmod(python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return python


def _doc(**kw) -> HandoffDoc:
    base = dict(title="Do the thing", request="Implement it", verification="auto", commit_policy="no-commit")
    base["target_agent"] = "codex"
    base["explicit_target"] = True
    base.update(kw)
    return HandoffDoc(**base)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    yield d
    d.stop()


def _verify(daemon: MaestroDaemon, ws: Path, task_id: str = "task-v", **doc_kw) -> tuple[bool, str]:
    """Run daemon verification on a workspace as a fresh turn and return (ok, report)."""
    daemon._tasks[task_id] = {"base_head": daemon._base_head(ws)}
    ok = daemon._verify(ws, task_id, _doc(**doc_kw))
    report = (daemon.state_dir / "tasks" / task_id / "verification.txt").read_text(encoding="utf-8")
    return ok, report


# ------------------------------------------------ bug 1: pytest missing
def test_missing_pytest_with_tests_dir_fails_verification(daemon, tmp_path, monkeypatch):
    """A project with a tests/ directory must not be certified when pytest is missing.

    Before the fix, verification fell back to `git diff --check` and reported
    PASSED as soon as the workspace had changes, so the tests never ran.
    """
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n", "tests/test_x.py": "def test_x():\n    assert False\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", str(_python_without_pytest(tmp_path)))
    (ws / "work.py").write_text("x = 1\n", encoding="utf-8")  # the agent did some work
    ok, report = _verify(daemon, ws)
    assert ok is False
    assert "pytest was not found" in report
    assert str(tmp_path / "fakepy" / "bin" / "python") in report
    assert "MAESTRO_PYTHON" in report
    assert "explicit verification command" in report
    claims = daemon.maestro._claims("task-v")
    assert claims["task_verification"].startswith("FAILED")


@pytest.mark.parametrize(
    "files",
    [
        {"tests/test_x.py": "def test_x():\n    pass\n"},
        {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n"},
        {"pytest.ini": "[pytest]\n"},
        {"setup.cfg": "[metadata]\nname = x\n\n[tool:pytest]\ntestpaths = t\n"},
        {"tox.ini": "[tox]\nenvlist = py3\n\n[pytest]\naddopts = -q\n"},
    ],
    ids=["tests-dir", "pyproject-pytest-config", "pytest-ini", "setup-cfg-pytest", "tox-ini-pytest"],
)
def test_missing_pytest_with_test_suite_selects_failing_pytest_run(tmp_path, monkeypatch, files):
    ws = _repo(tmp_path, files)
    python = _python_without_pytest(tmp_path)
    monkeypatch.setenv("MAESTRO_PYTHON", str(python))
    cmd, note = worker._verification_command(ws)
    assert cmd == [str(python), "-m", "pytest"]
    assert note is not None and "pytest was not found" in note and str(python) in note


@pytest.mark.parametrize(
    "files",
    [
        {"pyproject.toml": "[project]\nname = 'x'\n"},
        {"setup.cfg": "[metadata]\nname = x\n"},
        {"tox.ini": "[tox]\nenvlist = py3\n"},
    ],
    ids=["pyproject-only", "setup-cfg-only", "tox-ini-only"],
)
def test_missing_pytest_without_test_suite_keeps_fallback(tmp_path, monkeypatch, files):
    """A Python project with no tests and no pytest configuration still uses the fallback."""
    ws = _repo(tmp_path, files)
    monkeypatch.setenv("MAESTRO_PYTHON", str(_python_without_pytest(tmp_path)))
    cmd, note = worker._verification_command(ws)
    assert cmd == FALLBACK
    assert note is not None and "no tests/ directory" in note


def test_unreadable_pyproject_is_not_a_test_suite(tmp_path, monkeypatch):
    ws = _repo(tmp_path, {})
    (ws / "pyproject.toml").mkdir()  # exists, but reading it raises an OSError
    monkeypatch.setenv("MAESTRO_PYTHON", str(_python_without_pytest(tmp_path)))
    cmd, _note = worker._verification_command(ws)
    assert cmd == FALLBACK


def test_uncallable_interpreter_counts_as_missing_pytest(tmp_path, monkeypatch):
    """An interpreter that cannot be started is treated the same as one without pytest."""
    ws = _repo(tmp_path, {"tests/test_x.py": "def test_x():\n    pass\n"})
    not_executable = tmp_path / "python-not-executable"
    not_executable.write_text("not a program\n", encoding="utf-8")
    monkeypatch.setenv("MAESTRO_PYTHON", str(not_executable))
    cmd, note = worker._verification_command(ws)
    assert cmd == [str(not_executable), "-m", "pytest"]
    assert note is not None and "pytest was not found" in note


def test_python_executable_finds_venv_directory(tmp_path, monkeypatch):
    """A project environment kept in venv/ (not .venv/) is used for verification."""
    monkeypatch.delenv("MAESTRO_PYTHON", raising=False)
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    python = bindir / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    assert worker._python_executable(tmp_path) == str(python)
    # .venv/ is still preferred when both exist.
    dot = tmp_path / ".venv" / "bin"
    dot.mkdir(parents=True)
    (dot / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (dot / "python").chmod(0o755)
    assert worker._python_executable(tmp_path) == str(dot / "python")


# ------------------------------------------------ bug 2: Makefile without check
def test_makefile_without_check_target_falls_through(tmp_path, monkeypatch):
    """A Makefile with no check target must not make every task fail.

    Before the fix, any Makefile selected `make check`, which then failed with
    "No rule to make target 'check'".
    """
    ws = _repo(tmp_path, {"Makefile": "build:\n\techo building\n"})
    cmd, _note = worker._verification_command(ws)
    assert cmd == FALLBACK


def test_makefile_without_check_target_verifies_by_next_detector(daemon, tmp_path):
    ws = _repo(tmp_path, {"Makefile": "build:\n\techo building\n"})
    (ws / "work.txt").write_text("done\n", encoding="utf-8")
    ok, report = _verify(daemon, ws)
    assert ok is True
    assert "No rule to make target" not in report


@pytest.mark.parametrize(
    "makefile",
    [
        "check:\n\t@echo ok\n",
        "all: build\n\nlint check: all\n\t@echo ok\n",
        "check::\n\t@echo ok\n",
        ".PHONY: check\ncheck : build\n\t@echo ok\n",
    ],
    ids=["simple", "several-targets", "double-colon", "phony-and-space"],
)
def test_makefile_with_check_target_selects_make_check(tmp_path, monkeypatch, makefile):
    ws = _repo(tmp_path, {"Makefile": makefile})

    def no_subprocess(*_a, **_k):
        raise AssertionError("a check target written in the Makefile must be found without running make")

    monkeypatch.setattr(worker.subprocess, "run", no_subprocess)
    assert worker._verification_command(ws) == (["make", "check"], None)


@pytest.mark.parametrize(
    "makefile",
    [
        "CHECK := yes\nbuild:\n\t@true\n",
        "check:=yes\nbuild:\n\t@true\n",
        "check ::= yes\nbuild:\n\t@true\n",
        "X = a:check\nbuild:\n\t@true\n",
        "# check: commented out\nbuild:\n\t@true\n",
        "build:\n\tcheck: this is a recipe line\n",
        ".PHONY: check\nbuild:\n\t@true\n",
    ],
    ids=["variable", "variable-no-space", "posix-assignment", "value-with-colon", "comment", "recipe-line", "phony-only"],
)
def test_makefile_lines_that_are_not_a_check_rule(tmp_path, makefile):
    ws = _repo(tmp_path, {"Makefile": makefile})
    assert worker._verification_command(ws)[0] == FALLBACK


def test_makefile_check_target_in_included_file_is_found(tmp_path):
    """A check target defined in an included file is found by asking make itself."""
    ws = _repo(tmp_path, {"Makefile": "include rules.mk\n", "rules.mk": "check:\n\t@echo ok\n"})
    assert worker._verification_command(ws)[0] == ["make", "check"]


def test_file_named_check_is_not_a_check_target(tmp_path):
    """A file called `check` makes `make check` succeed with nothing to do; that is not a check."""
    ws = _repo(tmp_path, {"Makefile": "build:\n\t@true\n", "check": "just a file\n"})
    assert worker._verification_command(ws)[0] == FALLBACK


def test_makefile_probe_failures_fall_through(tmp_path, monkeypatch):
    ws = _repo(tmp_path, {"Makefile": "build:\n\t@true\n"})

    def make_missing(*_a, **_k):
        raise FileNotFoundError("make")

    monkeypatch.setattr(worker.subprocess, "run", make_missing)
    assert worker._makefile_has_check_target(ws) is False

    def make_hangs(*_a, **_k):
        raise subprocess.TimeoutExpired("make", 30)

    monkeypatch.setattr(worker.subprocess, "run", make_hangs)
    assert worker._makefile_has_check_target(ws) is False


def test_unreadable_makefile_falls_through(tmp_path):
    ws = _repo(tmp_path, {})
    (ws / "Makefile").mkdir()  # exists, but reading it raises an OSError
    assert worker._makefile_has_check_target(ws) is False


# ------------------------------------------------ bug 3: pytest collects no tests
def test_pytest_no_tests_with_changes_passes(daemon, tmp_path, monkeypatch):
    """pytest exit code 5 ("no tests collected") is not a test failure.

    Before the fix, a pyproject.toml project with no tests failed every task.
    """
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", sys.executable)  # this interpreter has pytest
    assert worker._verification_command(ws)[0] == [sys.executable, "-m", "pytest"]
    (ws / "work.txt").write_text("done\n", encoding="utf-8")
    ok, report = _verify(daemon, ws)
    assert ok is True
    assert "pytest found no tests to run" in report


def test_pytest_no_tests_without_changes_fails(daemon, tmp_path, monkeypatch):
    """A run where pytest found no tests cannot certify a turn that changed nothing."""
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", sys.executable)
    ok, report = _verify(daemon, ws)
    assert ok is False
    assert "no changes detected" in report
    assert "pytest found no tests to run" in report
    assert "no project test runner was detected" not in report


def test_explicit_command_exit_code_5_still_fails(daemon, tmp_path):
    """An explicitly configured command keeps its meaning: any non-zero exit is a failure."""
    ws = _repo(tmp_path, {})
    (ws / "work.txt").write_text("done\n", encoding="utf-8")
    ok, _report = _verify(daemon, ws, verification="command", request="sh -c 'exit 5'")
    assert ok is False


def test_pytest_no_tests_helper():
    assert worker._pytest_found_no_tests(["/py", "-m", "pytest"], 5) is True
    assert worker._pytest_found_no_tests(["/py", "-m", "pytest"], 1) is False
    assert worker._pytest_found_no_tests(["npm", "test"], 5) is False


# ------------------------------------------------ bug 4: default npm test script
def test_default_npm_test_script_is_not_a_test_runner(tmp_path):
    """The script `npm init` writes always fails; it means "no tests", not "tests failed"."""
    package = {"name": "x", "scripts": {"test": 'echo "Error: no test specified" && exit 1'}}
    ws = _repo(tmp_path, {"package.json": json.dumps(package)})
    cmd, _note = worker._verification_command(ws)
    assert cmd == FALLBACK


def test_default_npm_test_script_verifies_by_next_detector(tmp_path):
    package = {"name": "x", "scripts": {"test": 'echo "Error: no test specified" && exit 1'}}
    ws = _repo(tmp_path, {"package.json": json.dumps(package), "go.mod": "module x\n"})
    assert worker._verification_command(ws)[0] == ["go", "test", "./..."]


def test_real_npm_test_script_is_still_used(tmp_path):
    package = {"name": "x", "scripts": {"test": "jest"}}
    ws = _repo(tmp_path, {"package.json": json.dumps(package)})
    assert worker._verification_command(ws)[0] == ["npm", "test"]
