"""Shared verification helpers.

The legacy 0.8.x worker subprocess (which spawned ``codex exec`` directly with
Claude-supervised prompt text) has been removed: all delegation now runs through
the daemon and its adapters. What remains here is the deterministic test-runner
auto-detection that the daemon's verification step reuses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# pytest exits with this code when it finds no tests to run.
PYTEST_NO_TESTS_EXIT_CODE = 5

# The test script that `npm init` writes into a new package.json. It always
# exits 1, so it means "this package has no tests", not "the tests failed".
_NPM_DEFAULT_TEST_SCRIPT = re.compile(r"""echo\s+(["'])Error: no test specified\1\s*&&\s*exit\s+1""")

# A Makefile rule line: one or more target names at the start of the line,
# followed by ":" or "::". Variable assignments such as "X := 1" and "X ::= 1"
# are excluded, and so are recipe lines, which start with a tab.
_MAKE_RULE_LINE = re.compile(r"^([^\s:#=][^:#=]*?)\s*::?(?![:=])")


def _python_executable(root: Path) -> str:
    """Return the Python interpreter that verification should use for this project.

    The order is: the MAESTRO_PYTHON environment variable, the project's own
    virtual environment in ``.venv/`` or ``venv/``, and finally the interpreter
    that is running Maestro itself.
    """
    env_python = os.environ.get("MAESTRO_PYTHON")
    if env_python and Path(env_python).is_file():
        return env_python

    for venv in (".venv", "venv"):
        repo_python = root / venv / "bin" / "python"
        if repo_python.is_file() and os.access(repo_python, os.X_OK):
            return str(repo_python)

    return sys.executable


def _file_contains(path: Path, text: str) -> bool:
    """Return True when the file exists, can be read, and contains the text."""
    try:
        return text in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _has_python_test_suite(root: Path) -> bool:
    """Return True when the project clearly has pytest tests that must run.

    That is the case when it has a ``tests/`` directory or a pytest
    configuration: a ``pytest.ini`` file, a ``[tool.pytest...]`` table in
    ``pyproject.toml``, a ``[tool:pytest]`` section in ``setup.cfg``, or a
    ``[pytest]`` section in ``tox.ini``.
    """
    return (
        (root / "tests").is_dir()
        or (root / "pytest.ini").exists()
        or _file_contains(root / "pyproject.toml", "[tool.pytest")
        or _file_contains(root / "setup.cfg", "[tool:pytest]")
        or _file_contains(root / "tox.ini", "[pytest]")
    )


def _pytest_importable(python: str, root: Path) -> bool:
    """Return True when the interpreter starts and can import pytest."""
    try:
        probe = subprocess.run([python, "-c", "import pytest"], cwd=root, text=True, capture_output=True)
    except OSError:
        return False
    return probe.returncode == 0


def _missing_pytest_note(python: str) -> str:
    return (
        f"pytest was not found in the selected Python interpreter ({python}). "
        "This project has a Python test suite, so verification fails instead of skipping the tests. "
        "To fix this, set the MAESTRO_PYTHON environment variable to the Python interpreter of the "
        "project's environment (the one where pytest is installed), or give the handoff an explicit "
        "verification command (verification = \"command\")."
    )


def _makefile_has_check_target(root: Path) -> bool:
    """Return True when the project's Makefile defines a ``check`` target.

    A check target written directly in the Makefile is found by reading the
    file, without running make. Otherwise make itself is asked with a dry run
    (``make -n check``), which also finds targets defined in included files or
    by pattern rules. A dry run that reports "Nothing to be done" is not a
    check target: it happens when a file named ``check`` exists but no rule
    builds it, and ``make check`` would then pass without checking anything.
    """
    try:
        text = (root / "Makefile").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        match = _MAKE_RULE_LINE.match(line)
        if match and "check" in match.group(1).split():
            return True
    try:
        probe = subprocess.run(
            ["make", "-n", "check"], cwd=root, text=True, capture_output=True, timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0 and "Nothing to be done" not in probe.stdout + probe.stderr


def _pytest_found_no_tests(command: list[str], returncode: int) -> bool:
    """Return True when an auto-detected pytest run exited because it found no tests."""
    return command[1:] == ["-m", "pytest"] and returncode == PYTEST_NO_TESTS_EXIT_CODE


def _verification_command(root: Path, configured: list[str] | None = None) -> tuple[list[str], str | None]:
    if configured:
        return list(configured), None
    if (root / "Makefile").exists() and _makefile_has_check_target(root):
        return ["make", "check"], None
    package_json = root / "package.json"
    if package_json.exists():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            test_script = scripts.get("test") if isinstance(scripts, dict) else None
            if test_script and not _NPM_DEFAULT_TEST_SCRIPT.fullmatch(str(test_script).strip()):
                if (root / "pnpm-lock.yaml").exists():
                    return ["pnpm", "test"], None
                if (root / "yarn.lock").exists():
                    return ["yarn", "test"], None
                return ["npm", "test"], None
        except (OSError, json.JSONDecodeError):
            pass
    if (root / "go.mod").exists():
        return ["go", "test", "./..."], None
    if (root / "Cargo.toml").exists():
        return ["cargo", "test"], None
    python_root = root / "pyproject.toml"
    pytest_configured = (root / "pytest.ini").exists() or (root / "tox.ini").exists() or (root / "setup.cfg").exists()
    has_tests = (root / "tests").is_dir()
    if python_root.exists() or pytest_configured or has_tests:
        python = _python_executable(root)
        if _pytest_importable(python, root):
            return [python, "-m", "pytest"], None
        if _has_python_test_suite(root):
            # Running pytest here fails with "No module named pytest", so the
            # task fails verification and the note explains how to fix it.
            # Falling back to `git diff --check` would skip the tests silently.
            return [python, "-m", "pytest"], _missing_pytest_note(python)
        return ["git", "diff", "--check"], (
            "pytest is not installed in the selected Python environment, and this project has no "
            "tests/ directory and no pytest configuration, so there were no tests to skip; "
            "using git diff --check only"
        )
    return ["git", "diff", "--check"], "no project test runner detected; using git diff --check only"
