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
import shutil
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
needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


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
    """Run daemon verification on a workspace as a fresh turn and return (ok, report).

    The turn's baseline is recorded first, the way a real turn records it
    before the agent runs.
    """
    doc = _doc(**doc_kw)
    daemon._tasks[task_id] = {}
    daemon._record_turn_baseline(task_id, ws, doc)
    ok = daemon._verify(ws, task_id, doc)
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
    assert note is not None and "no Python test suite" in note


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
    """A check target defined in an included file is found by reading that file."""
    ws = _repo(tmp_path, {"Makefile": "include rules.mk\n", "rules.mk": "check:\n\t@echo ok\n"})
    assert worker._verification_command(ws)[0] == ["make", "check"]


def test_file_named_check_is_not_a_check_target(tmp_path):
    """A file called `check` makes `make check` succeed with nothing to do; that is not a check."""
    ws = _repo(tmp_path, {"Makefile": "build:\n\t@true\n", "check": "just a file\n"})
    assert worker._verification_command(ws)[0] == FALLBACK


@pytest.mark.parametrize(
    "files",
    [
        {"Makefile": "build:\n\t@true\n"},
        {"Makefile": "include rules.mk\n", "rules.mk": "build:\n\t@true\n"},
        {"Makefile": "build:\n\t@true\n%:\n\t@:\n"},
    ],
    ids=["no-check", "include-without-check", "catch-all"],
)
def test_makefile_detection_never_runs_make(tmp_path, monkeypatch, files):
    """Detection reads the makefiles and never runs make, not even a dry run.

    A dry run is not harmless: GNU make remakes included makefiles, runs
    `$(MAKE)` and `+` recipe lines, and expands `$(shell ...)` even under -n.
    It also took up to 30 seconds on every verification.
    """
    ws = _repo(tmp_path, files)
    real_run = subprocess.run

    def no_make(cmd, *a, **k):
        if cmd and cmd[0] == "make":
            raise AssertionError(f"detection must not run make: {cmd}")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(worker.subprocess, "run", no_make)
    assert worker._makefile_has_check_target(ws) is False
    assert worker._verification_command(ws)[0] == FALLBACK


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


def _status(ws: Path) -> str:
    return subprocess.run(["git", "-C", str(ws), "status", "--porcelain"], text=True, capture_output=True).stdout


# ------------------------------------------------ review: the make dry run had side effects
def test_detection_does_not_remake_an_included_makefile(daemon, tmp_path):
    """Choosing a test command must not change the workspace.

    `make -n check` remade `config.mk` from its rule, and the new file then
    counted as the agent's work, so a turn that did nothing was PASSED.
    """
    makefile = "include config.mk\nbuild:\n\t@true\nconfig.mk:\n\techo X=1 > config.mk\n"
    ws = _repo(tmp_path, {"Makefile": makefile})
    assert worker._verification_command(ws)[0] == FALLBACK
    assert not (ws / "config.mk").exists()
    ok, report = _verify(daemon, ws)
    assert ok is False
    assert "no changes detected" in report
    assert _status(ws) == ""


def test_detection_does_not_run_recursive_make_lines(tmp_path):
    """A check target in an included file is found without running its recipe.

    Under -n, make still runs recipe lines that use $(MAKE), so the dry run
    used to create SIDE_EFFECT here.
    """
    rules = "check:\n\ttouch SIDE_EFFECT && $(MAKE) -C . build\nbuild:\n\t@true\n"
    ws = _repo(tmp_path, {"Makefile": "include rules.mk\n", "rules.mk": rules})
    assert worker._verification_command(ws)[0] == ["make", "check"]
    assert not (ws / "SIDE_EFFECT").exists()


def test_builtin_rule_is_not_a_check_target(daemon, tmp_path):
    """make's built-in rule `%: %.sh` is not a test suite.

    With a `check.sh` in the workspace, `make -n check` succeeded, `make check`
    then only copied the script to a file named `check`, ran no tests, and the
    turn was PASSED.
    """
    ws = _repo(tmp_path, {"Makefile": "build:\n\t@true\n", "check.sh": "#!/bin/sh\necho RUNNING TESTS\nexit 1\n"})
    assert worker._verification_command(ws)[0] == FALLBACK
    ok, report = _verify(daemon, ws)
    assert ok is False
    assert not (ws / "check").exists()
    assert "no changes detected" in report


@pytest.mark.parametrize(
    "makefile",
    ["build:\n\t@true\n%:\n\t@:\n", "build:\n\t@true\n.DEFAULT:\n\t@:\n", "build:\n\t@true\n%::\n\t@:\n"],
    ids=["catch-all-pattern", "default-rule", "double-colon-catch-all"],
)
def test_catch_all_rule_is_not_a_check_target(tmp_path, makefile):
    """A rule that matches every target makes `make check` succeed without testing anything."""
    ws = _repo(tmp_path, {"Makefile": makefile})
    assert worker._verification_command(ws)[0] == FALLBACK


# ------------------------------------------------ review: the text scan was too loose
@pytest.mark.parametrize(
    "makefile",
    [
        "define HELP\ncheck: run the tests\nendef\nexport HELP\nhelp:\n\t@echo \"$$HELP\"\n",
        "define OUTER\ndefine INNER\nendef\ncheck: still inside OUTER\nendef\nbuild:\n\t@true\n",
        "override define HELP =\ncheck: text\nendef\nbuild:\n\t@true\n",
        "check: PYTEST_ARGS = -q\nbuild:\n\t@true\n",
        "check: PYTEST_ARGS := -q\nbuild:\n\t@true\n",
        "check: PYTEST_ARGS += -q\nbuild:\n\t@true\n",
        "check: PYTEST_ARGS ?= -q\nbuild:\n\t@true\n",
        "check:: PYTEST_ARGS = -q\nbuild:\n\t@true\n",
        "check: export PYTEST_ARGS=-q\nbuild:\n\t@true\n",
        "check: override PYTEST_ARGS = -q\nbuild:\n\t@true\n",
        "check: private PYTEST_ARGS = -q\nbuild:\n\t@true\n",
        "check: PYTEST_ARGS != echo -q\nbuild:\n\t@true\n",
        "X = one \\\n  check: two\nbuild:\n\t@true\n",
        "build:\n\techo one \\\ncheck: part of the recipe\n",
        "ifeq ($(A),check:)\nendif\nbuild:\n\t@true\n",
        "\tcheck: a recipe-prefixed line\nbuild:\n\t@true\n",
        "$(NAME): build\nbuild:\n\t@true\n",
    ],
    ids=[
        "define-block", "nested-define", "override-define", "target-var", "target-var-simple", "target-var-append",
        "target-var-conditional", "target-var-double-colon", "target-var-export", "target-var-override",
        "target-var-private", "target-var-shell", "continued-assignment", "continued-recipe", "conditional",
        "tab-line", "variable-target",
    ],
)
def test_makefile_text_that_is_not_a_check_rule(tmp_path, makefile):
    ws = _repo(tmp_path, {"Makefile": makefile})
    assert worker._verification_command(ws)[0] == FALLBACK


@pytest.mark.parametrize(
    "makefile",
    [
        "define HELP\ncheck: text\nendef\ncheck:\n\t@true\n",
        "lint \\\n  check: build\n\t@true\nbuild:\n\t@true\n",
        "check: build ; @true\nbuild:\n\t@true\n",
        "check: build # run the tests\n\t@true\nbuild:\n\t@true\n",
        "lint check&: build\n\t@true\nbuild:\n\t@true\n",
        "check: $(filter a=b,x)\n\t@true\n",
        "endef\ncheck:\n\t@true\n",
        "lint\\#one check: build\n\t@true\nbuild:\n\t@true\n",
        "build:\n\t@true\ncheck: \\\n",
    ],
    ids=[
        "after-define", "continued-targets", "inline-recipe", "trailing-comment", "grouped-targets", "var-in-prereqs",
        "stray-endef", "escaped-hash", "continuation-at-end-of-file",
    ],
)
def test_makefile_text_that_is_a_check_rule(tmp_path, makefile):
    ws = _repo(tmp_path, {"Makefile": makefile})
    assert worker._verification_command(ws)[0] == ["make", "check"]


@needs_make
@pytest.mark.parametrize(
    "makefile",
    [
        "define HELP\ncheck: run the tests\nendef\nexport HELP\nhelp:\n\t@echo \"$$HELP\"\n",
        "check: PYTEST_ARGS = -q\nbuild:\n\t@true\n",
        "check: PYTEST_ARGS := -q\nbuild:\n\t@true\n",
        "check: export PYTEST_ARGS=-q\nbuild:\n\t@true\n",
        "X = one \\\n  check: two\nbuild:\n\t@true\n",
        "check:\n\t@true\n",
        "lint \\\n  check: build\n\t@true\nbuild:\n\t@true\n",
        "include rules.mk\n",
    ],
    ids=["define-block", "target-var", "target-var-simple", "target-var-export", "continued-assignment",
         "simple", "continued-targets", "included"],
)
def test_detection_agrees_with_make(tmp_path, makefile):
    """Whenever detection chooses `make check`, make really has a check target, and the other way round."""
    ws = _repo(tmp_path, {"Makefile": makefile, "rules.mk": "check:\n\t@true\n"})
    chosen = worker._verification_command(ws)[0] == ["make", "check"]
    run = subprocess.run(["make", "check"], cwd=ws, text=True, capture_output=True, env={**os.environ, "LC_ALL": "C"})
    if chosen:
        assert run.returncode == 0, run.stderr
    else:
        assert run.returncode != 0 and "No rule to make target" in run.stderr


def test_makefile_lookup_follows_make_order(tmp_path):
    """make reads GNUmakefile first and ignores Makefile when it exists; detection does the same."""
    ws = _repo(tmp_path, {"GNUmakefile": "build:\n\t@true\n", "Makefile": "check:\n\t@true\n"})
    assert worker._verification_command(ws)[0] == FALLBACK
    ws2 = tmp_path / "gnu-only"
    ws2.mkdir()
    (ws2 / "GNUmakefile").write_text("check:\n\t@true\n", encoding="utf-8")
    assert worker._verification_command(ws2)[0] == ["make", "check"]


def test_lowercase_makefile_is_read(tmp_path):
    ws = tmp_path / "lower"
    ws.mkdir()
    (ws / "makefile").write_text("check:\n\t@true\n", encoding="utf-8")
    assert worker._verification_command(ws)[0] == ["make", "check"]


@pytest.mark.parametrize(
    "files",
    [
        {"Makefile": "-include rules.mk\n", "rules.mk": "check:\n\t@true\n"},
        {"Makefile": "sinclude rules.mk\n", "rules.mk": "check:\n\t@true\n"},
        {"Makefile": "include a.mk b.mk\n", "a.mk": "build:\n\t@true\n", "b.mk": "check:\n\t@true\n"},
        {"Makefile": "include mk/a.mk\n", "mk/a.mk": "include mk/b.mk\n", "mk/b.mk": "check:\n\t@true\n"},
        {"Makefile": "include rules.mk # the rules\n", "rules.mk": "check:\n\t@true\n"},
    ],
    ids=["dash-include", "sinclude", "several-files", "nested-include", "commented-include"],
)
def test_included_check_target_is_found(tmp_path, files):
    ws = _repo(tmp_path, files)
    assert worker._verification_command(ws)[0] == ["make", "check"]


def test_includes_that_are_not_followed(tmp_path):
    """Only literal paths to files inside the workspace are read; nothing is expanded or run."""
    outside = tmp_path / "outside.mk"
    outside.write_text("check:\n\t@true\n", encoding="utf-8")
    ws = _repo(
        tmp_path,
        {
            "Makefile": "RULES = rules.mk\ninclude $(RULES)\ninclude *.mk\ninclude missing.mk\n"
            "include ../outside.mk\ninclude " + str(outside) + "\ninclude subdir\ninclude loop.mk\n",
            "rules.mk": "check:\n\t@true\n",
            "loop.mk": "include loop.mk Makefile\n",
            "subdir/keep.txt": "x\n",
        },
    )
    assert worker._verification_command(ws)[0] == FALLBACK


def test_unreadable_included_makefile_is_skipped(tmp_path, monkeypatch):
    ws = _repo(tmp_path, {"Makefile": "include rules.mk\n", "rules.mk": "check:\n\t@true\n"})
    real_read = Path.read_text

    def fail_rules(self, *a, **k):
        if self.name == "rules.mk":
            raise PermissionError("denied")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", fail_rules)
    assert worker._makefile_has_check_target(ws) is False


def test_include_limit_stops_reading(tmp_path, monkeypatch):
    """A long chain of includes is read only up to a fixed number of files."""
    files = {"Makefile": "include m1.mk\n", "m1.mk": "include m2.mk\n", "m2.mk": "check:\n\t@true\n"}
    ws = _repo(tmp_path, files)
    assert worker._makefile_has_check_target(ws) is True
    monkeypatch.setattr(worker, "_MAKEFILE_READ_LIMIT", 2)
    assert worker._makefile_has_check_target(ws) is False


# ------------------------------------------------ review: exit code 5 after the tests were removed
def test_pytest_no_tests_after_deleting_the_suite_fails(daemon, tmp_path, monkeypatch):
    """An agent that deletes every test must not get PASSED.

    pytest exits 5 when it collects nothing. That was accepted whenever the
    workspace had changes, and deleting the tests is a change.
    """
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n", "tests/test_x.py": "def test_x():\n    assert False\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", sys.executable)
    doc = _doc()
    daemon._tasks["task-del"] = {}
    daemon._record_turn_baseline("task-del", ws, doc)  # the turn starts with a test suite
    assert daemon._tasks["task-del"]["python_test_suite"] is True
    shutil.rmtree(ws / "tests")  # the agent removes every test
    ok = daemon._verify(ws, "task-del", doc)
    report = (daemon.state_dir / "tasks" / "task-del" / "verification.txt").read_text(encoding="utf-8")
    assert ok is False
    assert "pytest collected no tests although the project had a Python test suite" in report
    assert "pytest found no tests to run" not in report


def test_pytest_no_tests_with_suite_recorded_directly_fails(daemon, tmp_path, monkeypatch):
    """The recorded turn-start value decides, not the workspace as the agent left it."""
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", sys.executable)
    (ws / "work.txt").write_text("done\n", encoding="utf-8")
    daemon._tasks["task-r"] = {"base_head": daemon._base_head(ws), "python_test_suite": True}
    assert daemon._verify(ws, "task-r", _doc()) is False


def test_python_test_suite_survives_a_daemon_restart(tmp_path, monkeypatch):
    """The turn-start value is kept in a claim, so a restarted daemon still uses it."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("MAESTRO_PYTHON", sys.executable)
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n", "tests/test_x.py": "def test_x():\n    pass\n"})
    first = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        first._tasks["task-s"] = {}
        first._record_turn_baseline("task-s", ws, _doc())
        assert first.maestro._claims("task-s")["task_python_test_suite"] == "true"
    finally:
        first.stop()
    shutil.rmtree(ws / "tests")
    second = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        second._tasks["task-s"] = {}  # the in-memory record lost the value
        assert second._verify(ws, "task-s", _doc()) is False
    finally:
        second.stop()


def test_python_test_suite_claim_false_allows_no_tests(daemon, tmp_path, monkeypatch):
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", sys.executable)
    daemon._tasks["task-f"] = {}
    daemon._record_turn_baseline("task-f", ws, _doc())
    assert daemon.maestro._claims("task-f")["task_python_test_suite"] == "false"
    daemon._tasks["task-f"].pop("python_test_suite")  # only the claim is left
    (ws / "work.txt").write_text("done\n", encoding="utf-8")
    assert daemon._verify(ws, "task-f", _doc()) is True


def test_unknown_python_test_suite_counts_as_having_one(daemon, tmp_path, monkeypatch):
    """A task with no recorded value (started by an older Maestro) does not accept exit code 5."""
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", sys.executable)
    (ws / "work.txt").write_text("done\n", encoding="utf-8")
    daemon._tasks["task-u"] = {"base_head": daemon._base_head(ws)}
    assert daemon._verify(ws, "task-u", _doc()) is False


def test_turn_baseline_skips_the_test_suite_scan_for_other_modes(daemon, tmp_path, monkeypatch):
    """Only auto-detected verification uses the value, so other modes do not scan the project."""
    ws = _repo(tmp_path, {"tests/test_x.py": "def test_x():\n    pass\n"})

    def no_scan(_root):
        raise AssertionError("the test suite scan must not run")

    monkeypatch.setattr("maestro.daemon._has_python_test_suite", no_scan)
    for mode, extra in (("command", {"request": "true"}), ("none", {})):
        daemon._tasks["task-m"] = {}
        daemon._record_turn_baseline("task-m", ws, _doc(verification=mode, **extra))
        assert "python_test_suite" not in daemon._tasks["task-m"]
        assert daemon._tasks["task-m"]["base_head"]


def test_turn_baseline_without_a_record_still_writes_claims(daemon, tmp_path):
    ws = _repo(tmp_path, {})
    daemon._record_turn_baseline("task-none", ws, _doc())
    claims = daemon.maestro._claims("task-none")
    assert claims["task_base_head"] and claims["task_python_test_suite"] == "false"


def test_run_turn_records_the_python_test_suite(tmp_path, monkeypatch):
    """A real delegated turn records the value before the agent runs."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    ws = _repo(tmp_path, {"test/test_x.py": "def test_x():\n    pass\n"})
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        started = d.delegate(_doc(verification="auto", target_agent="nonexistent-agent-xyz"), str(ws))
        d.wait(started["task_id"], timeout=30)
        assert d._tasks[started["task_id"]]["python_test_suite"] is True
    finally:
        d.stop()


# ------------------------------------------------ review: test suite layouts that were missed
@pytest.mark.parametrize(
    "files",
    [
        {"test/test_x.py": "def test_x():\n    pass\n"},
        {"src/pkg/tests/__init__.py": ""},
        {"pkg/tests/helpers.py": ""},
        {"conftest.py": ""},
        {"src/pkg/test_core.py": ""},
        {"pkg/core_test.py": ""},
    ],
    ids=["test-dir", "src-package-tests", "package-tests", "root-conftest", "test-prefix-file", "test-suffix-file"],
)
def test_python_test_suite_layouts_are_found(tmp_path, files):
    ws = _repo(tmp_path, files)
    assert worker._has_python_test_suite(ws) is True
    plain = tmp_path / "plain"  # the same layout outside git uses the directory walk
    for name, text in files.items():
        path = plain / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert worker._has_python_test_suite(plain) is True


@pytest.mark.parametrize(
    "files",
    [
        {".hidden/tests/test_x.py": ""},
        {"node_modules/pkg/tests/test_x.py": ""},
        {"env1/pyvenv.cfg": "home = /usr\n", "env1/lib/tests/test_x.py": ""},
        {"venv/lib/tests/test_x.py": ""},
        {"lib/site-packages/pkg/test_x.py": ""},
        {"src/pkg/conftest.py": "", "src/pkg/core.py": "", "docs/testing.md": ""},
    ],
    ids=["hidden-dir", "node-modules", "pyvenv-cfg", "venv-name", "site-packages", "no-tests"],
)
def test_python_test_suite_ignores_environments(tmp_path, files):
    ws = _repo(tmp_path, files)  # every file is tracked, because there is no .gitignore
    assert worker._has_python_test_suite(ws) is False
    plain = tmp_path / "plain"
    for name, text in files.items():
        path = plain / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert worker._has_python_test_suite(plain) is False


def test_python_test_suite_sees_untracked_files(tmp_path):
    """git ls-files also lists new files that are not ignored."""
    ws = _repo(tmp_path, {})
    (ws / "pkg").mkdir()
    (ws / "pkg" / "test_new.py").write_text("", encoding="utf-8")
    assert worker._has_python_test_suite(ws) is True


def test_python_test_suite_walk_is_bounded(tmp_path, monkeypatch):
    """The directory walk outside git gives up after a fixed number of entries."""
    for i in range(10):
        (tmp_path / f"file{i}.txt").write_text("", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_x.py").write_text("", encoding="utf-8")
    assert worker._has_python_test_suite(tmp_path) is True
    monkeypatch.setattr(worker, "_TEST_SUITE_WALK_LIMIT", 5)
    assert worker._has_python_test_suite(tmp_path) is False


def test_python_test_suite_walk_skips_unreadable_directories(tmp_path, monkeypatch):
    (tmp_path / "locked").mkdir()
    real_scandir = os.scandir

    def fail_locked(path):
        if str(path).endswith("locked"):
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(worker.os, "scandir", fail_locked)
    assert worker._has_python_test_suite(tmp_path) is False


@pytest.mark.parametrize("failure", ["missing-git", "timeout"])
def test_python_test_suite_falls_back_to_the_walk_when_git_fails(tmp_path, monkeypatch, failure):
    ws = _repo(tmp_path, {"pkg/test_x.py": ""})

    def broken_git(*_a, **_k):
        if failure == "missing-git":
            raise FileNotFoundError("git")
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr(worker.subprocess, "run", broken_git)
    assert worker._has_python_test_suite(ws) is True


def test_missing_pytest_with_singular_test_dir_fails_verification(daemon, tmp_path, monkeypatch):
    """A test/ directory is a test suite, so a missing pytest fails verification instead of skipping it."""
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n", "test/test_x.py": "def test_x():\n    assert False\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", str(_python_without_pytest(tmp_path)))
    (ws / "work.py").write_text("x = 1\n", encoding="utf-8")
    ok, report = _verify(daemon, ws)
    assert ok is False
    assert "pytest was not found" in report


def test_turn_baseline_outside_git_records_only_the_test_suite(daemon, tmp_path):
    """A workspace that is not a git repository has no HEAD, but its test suite is still recorded."""
    plain = tmp_path / "plain"
    (plain / "tests").mkdir(parents=True)
    daemon._tasks["task-plain"] = {}
    daemon._record_turn_baseline("task-plain", plain, _doc())
    assert daemon._tasks["task-plain"] == {"python_test_suite": True}


# ------------------------------------------------ pytest missing after the tests were removed
@pytest.mark.parametrize(
    "files",
    [
        {"pyproject.toml": "[project]\nname = 'x'\n", "tests/test_x.py": "def test_x():\n    assert False\n"},
        {"tests/test_x.py": "def test_x():\n    assert False\n"},
    ],
    ids=["pyproject", "tests-dir-only"],
)
def test_missing_pytest_after_deleting_the_suite_fails(daemon, tmp_path, monkeypatch, files):
    """An agent that deletes every test must not fall back to `git diff --check` when pytest is missing.

    Detection used to look only at the workspace the agent left behind, so
    with the tests gone it chose the fallback, and the deletion counted as
    work. With only a tests/ directory, the project did not even look like a
    Python project any more.
    """
    ws = _repo(tmp_path, files)
    python = _python_without_pytest(tmp_path)
    monkeypatch.setenv("MAESTRO_PYTHON", str(python))
    doc = _doc()
    daemon._tasks["task-gone"] = {}
    daemon._record_turn_baseline("task-gone", ws, doc)
    shutil.rmtree(ws / "tests")  # the agent removes every test and commits the removal
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "remove tests")
    ok = daemon._verify(ws, "task-gone", doc)
    report = (daemon.state_dir / "tasks" / "task-gone" / "verification.txt").read_text(encoding="utf-8")
    assert ok is False
    assert f"verification command: {python} -m pytest" in report
    assert "pytest was not found" in report


def test_verification_command_uses_the_recorded_test_suite(tmp_path, monkeypatch):
    """The turn-start value decides when it is True; otherwise the workspace decides."""
    python = _python_without_pytest(tmp_path)
    monkeypatch.setenv("MAESTRO_PYTHON", str(python))
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    assert worker._verification_command(ws, had_test_suite=True)[0] == [str(python), "-m", "pytest"]
    assert worker._verification_command(ws, had_test_suite=False)[0] == FALLBACK
    assert worker._verification_command(ws)[0] == FALLBACK  # unknown: the workspace decides
    (ws / "tests").mkdir()
    (ws / "tests" / "test_new.py").write_text("def test_new():\n    pass\n", encoding="utf-8")
    # Tests the agent added are not skipped either.
    assert worker._verification_command(ws, had_test_suite=False)[0] == [str(python), "-m", "pytest"]


def test_recorded_suite_in_a_claim_is_used_for_missing_pytest(daemon, tmp_path, monkeypatch):
    """After a restart only the claim is left, and it still decides."""
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n", "tests/test_x.py": "def test_x():\n    pass\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", str(_python_without_pytest(tmp_path)))
    daemon._tasks["task-c"] = {}
    daemon._record_turn_baseline("task-c", ws, _doc())
    daemon._tasks["task-c"].pop("python_test_suite")
    shutil.rmtree(ws / "tests")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "remove tests")
    assert daemon._verify(ws, "task-c", _doc()) is False


def test_unknown_suite_keeps_the_workspace_decision_for_missing_pytest(daemon, tmp_path, monkeypatch):
    """With no recorded value, a project without tests now still uses the fallback when pytest is missing."""
    ws = _repo(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    monkeypatch.setenv("MAESTRO_PYTHON", str(_python_without_pytest(tmp_path)))
    (ws / "work.txt").write_text("done\n", encoding="utf-8")
    daemon._tasks["task-old"] = {"base_head": daemon._base_head(ws)}
    assert daemon._verify(ws, "task-old", _doc()) is True


# ------------------------------------------------ time limit and visibility
def test_verification_time_limit_stops_the_command_and_its_children(daemon, tmp_path):
    import threading
    import time as _time

    ws = _repo(tmp_path, {"README.md": "# repo\n"})
    pidfile = tmp_path / "child.pid"
    daemon.maestro.config["verification_timeout_s"] = 1
    # The command starts a background child that would outlive a plain kill.
    command = f"sh -c 'sleep 60 & echo $! > {pidfile}; wait'"
    started = _time.monotonic()
    ok, report = _verify(daemon, ws, verification="command", verification_command=command)
    assert not ok
    assert _time.monotonic() - started < 20
    assert "did not finish within 1 second" in report
    assert "[verification] timeout_s" in report
    child = int(pidfile.read_text().strip())
    deadline = _time.monotonic() + 5
    while _time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.05)
    else:
        pytest.fail("the verification command's child process was left running")


def test_verification_without_a_time_limit_when_timeout_is_zero(daemon, tmp_path):
    ws = _repo(tmp_path, {"README.md": "# repo\n"})
    daemon.maestro.config["verification_timeout_s"] = 0
    ok, report = _verify(daemon, ws, verification="command", verification_command="true")
    assert ok and "did not finish" not in report


def test_verifying_phase_and_output_lines_while_tests_run(daemon, tmp_path):
    import threading
    import time as _time

    ws = _repo(tmp_path, {"README.md": "# repo\n"})
    gate = tmp_path / "gate"
    task_id = "task-phase"
    doc = _doc(verification="command", verification_command=f"sh -c 'while [ ! -f {gate} ]; do sleep 0.05; done'")
    daemon._tasks[task_id] = {"state": "working"}
    daemon._record_turn_baseline(task_id, ws, doc)
    sub = daemon.bus.subscribe("output")
    result = {}
    worker_thread = threading.Thread(target=lambda: result.__setitem__("ok", daemon._verify(ws, task_id, doc)))
    worker_thread.start()
    try:
        deadline = _time.monotonic() + 10
        while daemon.maestro._claims(task_id).get("task_status") != "VERIFYING":
            assert _time.monotonic() < deadline, "the VERIFYING phase was never written"
            _time.sleep(0.05)
        line = sub.wait(predicate=lambda e: e.task_id == task_id and "[verify] running:" in (e.data.get("line") or ""), timeout=10)
        assert line is not None and "time limit 30 minutes" in line.data["line"]
    finally:
        gate.touch()
        worker_thread.join(timeout=30)
        sub.close()
    assert result["ok"] is True
    assert daemon.maestro._claims(task_id)["task_status"] == "IMPLEMENTING"  # back to the working phase


def test_verifying_phase_is_not_written_for_a_canceled_task(daemon, tmp_path):
    ws = _repo(tmp_path, {"README.md": "# repo\n"})
    task_id = "task-canceled"
    doc = _doc(verification="command", verification_command="true")
    daemon._tasks[task_id] = {"state": "canceled"}
    daemon._record_turn_baseline(task_id, ws, doc)
    daemon.maestro._write_claim(task_id, "task_status", "FAILED")
    daemon._verify(ws, task_id, doc)
    assert daemon.maestro._claims(task_id)["task_status"] == "FAILED"
