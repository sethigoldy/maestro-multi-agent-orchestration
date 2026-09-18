"""Shared verification helpers.

The legacy 0.8.x worker subprocess (which spawned ``codex exec`` directly with
Claude-supervised prompt text) has been removed: all delegation now runs through
the daemon and its adapters. What remains here is the deterministic test-runner
auto-detection that the daemon's verification step reuses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _python_executable(root: Path) -> str:
    """Return the same interpreter that is running Maestro when possible."""
    env_python = os.environ.get("MAESTRO_PYTHON")
    if env_python and Path(env_python).is_file():
        return env_python

    repo_python = root / ".venv" / "bin" / "python"
    if repo_python.is_file() and os.access(repo_python, os.X_OK):
        return str(repo_python)

    return sys.executable


def _verification_command(root: Path, configured: list[str] | None = None) -> tuple[list[str], str | None]:
    if configured:
        return list(configured), None
    if (root / "Makefile").exists():
        return ["make", "check"], None
    package_json = root / "package.json"
    if package_json.exists():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            if isinstance(scripts, dict) and scripts.get("test"):
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
        probe = subprocess.run(
            [python, "-c", "import pytest"], cwd=root, text=True, capture_output=True,
        )
        if probe.returncode == 0:
            return [python, "-m", "pytest"], None
        return ["git", "diff", "--check"], "pytest is not installed in the selected Python environment; skipped test runner"
    return ["git", "diff", "--check"], "no project test runner detected; using git diff --check only"
