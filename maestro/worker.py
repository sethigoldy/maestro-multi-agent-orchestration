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

# The names make looks for, in the order it looks for them. make reads only
# the first one that exists.
_MAKEFILE_NAMES = ("GNUmakefile", "makefile", "Makefile")

# At most this many makefiles (the main one plus the files it includes) are
# read when looking for a check target.
_MAKEFILE_READ_LIMIT = 50

# Words that start a make directive line. Such a line is never a rule.
_MAKE_DIRECTIVES = frozenset({
    "ifeq", "ifneq", "ifdef", "ifndef", "else", "endif", "include", "-include", "sinclude",
    "export", "unexport", "override", "private", "vpath", "undefine", "define", "endef", "load", "-load",
})

# The start of a "define NAME" block, which may carry the override, export or
# private prefixes. Everything up to the matching "endef" is variable text.
_MAKE_DEFINE = re.compile(r"(?:(?:override|export|private)\s+)*define(?:\s|$)")
_MAKE_ENDEF = re.compile(r"endef(?:\s|#|$)")

# An include directive and the file names that follow it.
_MAKE_INCLUDE = re.compile(r"(?:-include|sinclude|include)\s+(.*)")

# The part of a line after "target:" when it is a target-specific variable,
# such as "check: PYTEST_ARGS = -q" or "check: export PYTEST_ARGS=-q". Such a
# line sets a variable for the target; it does not define a rule.
_MAKE_TARGET_VARIABLE = re.compile(
    r"(?:(?:export|override|private)\s+)*[^\s:=#;]+\s*(?:=|:=|::=|:::=|\+=|\?=|!=)"
)

# Directory names that never hold the project's own tests: installed
# packages, caches and virtual environments. Hidden directories (names that
# start with ".") are skipped as well, and so is any directory that holds a
# pyvenv.cfg file, which marks a virtual environment.
_NOT_PROJECT_DIRS = frozenset({"node_modules", "site-packages", "__pycache__", "venv"})
_TEST_DIR_NAMES = frozenset({"tests", "test"})

# The directory walk used outside git gives up after this many entries, so a
# very large project cannot slow down every turn.
_TEST_SUITE_WALK_LIMIT = 20000


def _python_executable(root: Path) -> str:
    """Return the Python interpreter that verification should use for this project.

    The order is: the MAESTRO_PYTHON environment variable, the project's own
    virtual environment in ``.venv/`` or ``venv/``, the same in the main
    checkout of the repository (for a task running in a worktree), and finally
    the interpreter that is running Maestro itself.
    """
    env_python = os.environ.get("MAESTRO_PYTHON")
    if env_python and Path(env_python).is_file():
        return env_python

    from .worktrees import main_repo_root

    # A task running in a worktree has no virtual environment of its own;
    # the project's usually lives in the main checkout.
    main = main_repo_root(root)
    for base in (root,) + ((main,) if main is not None and main != root else ()):
        for venv in (".venv", "venv"):
            repo_python = base / venv / "bin" / "python"
            if repo_python.is_file() and os.access(repo_python, os.X_OK):
                return str(repo_python)

    return sys.executable


def _file_contains(path: Path, text: str) -> bool:
    """Return True when the file exists, can be read, and contains the text."""
    try:
        return text in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _is_test_file(name: str) -> bool:
    """Return True for a file name that pytest collects by default."""
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _skipped_dir(name: str) -> bool:
    return name.startswith(".") or name in _NOT_PROJECT_DIRS


def _git_lists_test_suite(root: Path) -> bool | None:
    """Look for tests among the files git knows about, or return None when git cannot list them.

    The list holds tracked files and new files that are not ignored.
    """
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listing.returncode != 0:
        return None
    paths = [p.decode("utf-8", "replace").split("/") for p in listing.stdout.split(b"\0") if p]
    venvs = {tuple(parts[:-1]) for parts in paths if parts[-1] == "pyvenv.cfg"}
    for parts in paths:
        dirs = parts[:-1]
        if any(_skipped_dir(d) for d in dirs) or any(tuple(dirs[:i]) in venvs for i in range(1, len(dirs) + 1)):
            continue
        if any(d in _TEST_DIR_NAMES for d in dirs) or _is_test_file(parts[-1]):
            return True
    return False


def _walk_finds_test_suite(root: Path) -> bool:
    """Look for tests with a directory walk that stops after a fixed number of entries."""
    seen = 0
    pending = [root]
    while pending:
        directory = pending.pop(0)
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > _TEST_SUITE_WALK_LIMIT:
                return False
            if entry.is_dir(follow_symlinks=False):
                if _skipped_dir(entry.name) or (Path(entry.path) / "pyvenv.cfg").exists():
                    continue
                if entry.name in _TEST_DIR_NAMES:
                    return True
                pending.append(Path(entry.path))
            elif _is_test_file(entry.name):
                return True
    return False


def _has_python_test_suite(root: Path) -> bool:
    """Return True when the project clearly has pytest tests that must run.

    That is the case when the project has any of these:

    - a ``tests/`` or ``test/`` directory anywhere, outside hidden
      directories, virtual environments and ``node_modules``;
    - a ``conftest.py`` file at the project root;
    - a file named ``test_*.py`` or ``*_test.py`` in the same places;
    - a pytest configuration: a ``pytest.ini`` file, a ``[tool.pytest...]``
      table in ``pyproject.toml``, a ``[tool:pytest]`` section in
      ``setup.cfg``, or a ``[pytest]`` section in ``tox.ini``.

    In a git repository the files come from ``git ls-files``. Otherwise a
    directory walk looks for them and stops after a fixed number of entries.
    """
    if (
        (root / "tests").is_dir()
        or (root / "test").is_dir()
        or (root / "conftest.py").is_file()
        or (root / "pytest.ini").exists()
        or _file_contains(root / "pyproject.toml", "[tool.pytest")
        or _file_contains(root / "setup.cfg", "[tool:pytest]")
        or _file_contains(root / "tox.ini", "[pytest]")
    ):
        return True
    found = _git_lists_test_suite(root)
    if found is None:
        found = _walk_finds_test_suite(root)
    return found


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


def _makefile_path(root: Path) -> Path | None:
    """Return the makefile that ``make`` would read in this directory, or None."""
    for name in _MAKEFILE_NAMES:
        path = root / name
        if path.is_file():
            return path
    return None


def _makefile_lines(text: str):
    """Yield the logical lines of a makefile.

    A line that ends with a backslash continues on the next line, so the two
    are joined into one.
    """
    pending = ""
    for line in text.splitlines():
        stripped = line.rstrip("\\")
        if (len(line) - len(stripped)) % 2 == 1:
            pending += line[:-1] + " "
            continue
        yield pending + line
        pending = ""
    if pending:
        yield pending


def _strip_make_comment(line: str) -> str:
    """Remove a "#" comment from a makefile line. An escaped "\\#" is kept."""
    index = 0
    while True:
        index = line.find("#", index)
        if index == -1:
            return line
        if index == 0 or line[index - 1] != "\\":
            return line[:index]
        index += 1


def _make_rule_targets(line: str) -> list[str]:
    """Return the targets of an explicit rule line, or an empty list when it is not a rule.

    Variable assignments ("X = a:b", "X := 1", "check:=1") and target-specific
    variables ("check: X = 1") are not rules.
    """
    colon = line.find(":")
    if colon <= 0:
        return []
    targets = line[:colon]
    if "=" in targets:
        return []
    after = line[colon:]
    if re.match(r":{1,3}=", after):
        return []
    rest = after[2:] if after.startswith("::") else after[1:]
    if _MAKE_TARGET_VARIABLE.match(rest.split(";", 1)[0].strip()):
        return []
    targets = targets.rstrip()
    if targets.endswith("&"):  # grouped targets, "a b &: prerequisites"
        targets = targets[:-1]
    return targets.split()


def _makefile_has_check_target(root: Path) -> bool:
    """Return True when the project's makefile defines an explicit ``check`` rule.

    The decision is made from the makefile text alone; make is never run.
    Running make, even as a dry run (``make -n``), can change the workspace:
    GNU make remakes included makefiles, runs recipe lines that use
    ``$(MAKE)`` or start with ``+``, and runs ``$(shell ...)``. A dry run
    also accepts targets that make builds from its built-in rules (such as
    ``%: %.sh``) or from a catch-all rule, and ``make check`` then tests
    nothing.

    The makefile is the first of ``GNUmakefile``, ``makefile`` and
    ``Makefile`` that exists, which is the one make reads. Files it includes
    are read too, when the include names a literal path (no variables or
    wildcards) to a file inside the workspace. A rule counts when ``check``
    is one of its targets, as in ``check:``, ``check::`` or ``lint check:``.
    These lines do not count: lines inside ``define ... endef`` blocks,
    recipe lines (which start with a tab), comments, variable assignments,
    and target-specific variables such as ``check: PYTEST_ARGS = -q``.
    Conditionals (``ifeq`` and similar) are not evaluated, so a rule inside
    a conditional counts whichever branch make would take.
    """
    first = _makefile_path(root)
    if first is None:
        return False
    workspace = root.resolve()
    pending = [first]
    visited: set[Path] = set()
    while pending and len(visited) < _MAKEFILE_READ_LIMIT:
        path = pending.pop(0).resolve()
        if path in visited:
            continue
        visited.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        define_depth = 0
        for line in _makefile_lines(text):
            if define_depth:
                bare = line.strip()
                if _MAKE_DEFINE.match(bare):
                    define_depth += 1
                elif _MAKE_ENDEF.match(bare):
                    define_depth -= 1
                continue
            if line.startswith("\t"):
                continue  # a recipe line
            line = _strip_make_comment(line).strip()
            if _MAKE_DEFINE.match(line):
                define_depth = 1
                continue
            include = _MAKE_INCLUDE.fullmatch(line)
            if include:
                for name in include.group(1).split():
                    if any(ch in name for ch in "$*?["):
                        continue  # only literal paths are followed
                    target = (root / name).resolve()
                    if target.is_file() and target.is_relative_to(workspace):
                        pending.append(target)
                continue
            words = line.split()
            if not words or words[0] in _MAKE_DIRECTIVES:
                continue
            if "check" in _make_rule_targets(line):
                return True
    return False


def _pytest_found_no_tests(command: list[str], returncode: int) -> bool:
    """Return True when an auto-detected pytest run exited because it found no tests."""
    return command[1:] == ["-m", "pytest"] and returncode == PYTEST_NO_TESTS_EXIT_CODE


def _verification_command(
    root: Path, configured: list[str] | None = None, had_test_suite: bool | None = None,
) -> tuple[list[str], str | None]:
    """Return the verification command for the workspace and an optional note for the report.

    ``had_test_suite`` says whether the project had a Python test suite when
    the turn started, before the agent ran. When it is True, the project is
    verified with pytest even if the tests are gone now, so an agent cannot
    avoid a failing check by deleting every test. When it is False or None
    (not known, for example outside a turn), the workspace as it is now
    decides.
    """
    if configured:
        return list(configured), None
    if _makefile_has_check_target(root):
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
    if python_root.exists() or pytest_configured or has_tests or had_test_suite:
        python = _python_executable(root)
        if _pytest_importable(python, root):
            return [python, "-m", "pytest"], None
        if had_test_suite or _has_python_test_suite(root):
            # Running pytest here fails with "No module named pytest", so the
            # task fails verification and the note explains how to fix it.
            # Falling back to `git diff --check` would skip the tests silently.
            return [python, "-m", "pytest"], _missing_pytest_note(python)
        return ["git", "diff", "--check"], (
            "pytest is not installed in the selected Python environment, and this project has no "
            "Python test suite (no tests/ or test/ directory, no test_*.py or *_test.py file, no "
            "conftest.py at the root and no pytest configuration), so there were no tests to skip; "
            "using git diff --check only"
        )
    return ["git", "diff", "--check"], "no project test runner detected; using git diff --check only"
