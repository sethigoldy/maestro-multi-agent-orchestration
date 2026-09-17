from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def _req_name(requirement: str) -> str:
    for i, ch in enumerate(requirement):
        if not (ch.isalnum() or ch in "._-"):
            break
    else:
        i = len(requirement)
    return requirement[:i]


def _version_key(v: str) -> tuple:
    parts = []
    for seg in v.split("."):
        num = "".join(c for c in seg if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _satisfies(requirement: str, version: str) -> bool:
    """Evaluate >=, <=, ==, !=, >, <, ~= clauses of a PEP 508 requirement."""
    name = _req_name(requirement)
    spec = requirement[len(name):].strip()
    if not spec:
        return True
    v = _version_key(version)
    for clause in spec.split(","):
        clause = clause.strip()
        for op in (">=", "<=", "==", "!=", "~=", ">", "<"):
            if clause.startswith(op):
                target = _version_key(clause[len(op):].strip())
                if op == ">=" and not v >= target: return False
                if op == "<=" and not v <= target: return False
                if op == "==" and not v == target: return False
                if op == "!=" and not v != target: return False
                if op == ">" and not v > target: return False
                if op == "<" and not v < target: return False
                if op == "~=":
                    base = target[:-1] or target
                    if not (v >= target and v[:len(base)] == base): return False
                break
    return True


def test_maestro_mcp_console_script_declared():
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["maestro-mcp"] == "maestro.mcp_server:main"
    assert scripts["maestro"] == "maestro.cli:main"


def test_console_script_targets_resolve_to_callables():
    for name, target in _pyproject()["project"]["scripts"].items():
        module_name, attr = target.split(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attr)), f"{name} -> {target} is not callable"


def test_mcp_dependency_allows_1_x_and_excludes_2_x():
    deps = _pyproject()["project"]["dependencies"]
    mcp_req = next(d for d in deps if _req_name(d) == "mcp")
    assert _satisfies(mcp_req, "1.30.0"), f"{mcp_req!r} must allow mcp 1.x"
    assert not _satisfies(mcp_req, "2.0.0"), (
        f"{mcp_req!r} must exclude mcp 2.x: FastMCP was removed and the server breaks"
    )


def test_installed_maestro_mcp_speaks_mcp_over_stdio():
    # No .resolve(): in a venv, bin/python is a symlink to the base interpreter,
    # and resolving it would point at the wrong bin directory.
    exe = Path(sys.executable).parent / "maestro-mcp"
    if not exe.exists():
        import pytest
        pytest.skip("maestro is not installed in this interpreter; console script absent")
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "smoke", "version": "0"}}}
    proc = subprocess.run([str(exe)], input=(json.dumps(request) + "\n").encode(),
                          capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode()
    response = json.loads(proc.stdout.decode().splitlines()[0])
    assert response["result"]["serverInfo"]["name"] == "maestro"
