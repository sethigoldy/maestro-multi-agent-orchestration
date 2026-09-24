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


# ------------------------------------------------------------------ v0.9 release readiness


def test_version_is_090_and_single_sourced():
    data = _pyproject()
    assert data["project"]["version"] == "0.15.1"
    from maestro import VERSION

    assert VERSION == data["project"]["version"], (
        "maestro.VERSION and pyproject.toml must agree; the release workflow "
        "also enforces tag == pyproject == maestro.VERSION"
    )


def test_license_is_apache_2_and_file_present():
    data = _pyproject()
    assert data["project"]["license"] == "Apache-2.0"  # PEP 639 expression
    license_file = REPO_ROOT / "LICENSE"
    assert license_file.is_file(), "LICENSE file missing from repository root"
    text = license_file.read_text(encoding="utf-8")
    assert "Apache License" in text and "Version 2.0" in text


def test_readme_and_release_metadata_present():
    data = _pyproject()["project"]
    readme = data.get("readme")
    assert readme == "README.md" and (REPO_ROOT / "README.md").is_file()
    assert data.get("description"), "project.description must be set for package indexes"
    assert data.get("authors"), "project.authors must be set"
    urls = data.get("urls") or {}
    assert any("github.com/sethigoldy/maestro-multi-agent-orchestration" in v for v in urls.values())


def test_manifest_and_package_data_cover_license_and_web_assets():
    manifest = REPO_ROOT / "MANIFEST.in"
    assert manifest.is_file(), "MANIFEST.in missing from repository root"
    text = manifest.read_text(encoding="utf-8")
    assert "LICENSE" in text, "sdist must include LICENSE (pip install from sdist loses it otherwise)"
    data = _pyproject()["tool"]["setuptools"]["package-data"]
    assert data.get("maestro.web_dist"), "web_dist assets must be packaged with the wheel"


def test_web_console_assets_exist_and_are_nonempty():
    for asset in ("console.js", "index.html"):
        path = REPO_ROOT / "maestro" / "web_dist" / asset
        assert path.is_file(), f"maestro/web_dist/{asset} missing; run `node web/build.mjs`"
        assert path.stat().st_size > 0, f"maestro/web_dist/{asset} is empty"


def test_release_scripts_exist_and_are_executable():
    for script in (
        "smoke-fake-agent.sh",
        "validate-package.sh",
        "demo-v0.10.sh",
        "demo-gif-setup.sh",
        "demo-gif-build-vhs.sh",
    ):
        path = REPO_ROOT / "scripts" / script
        assert path.is_file(), f"scripts/{script} missing"
        if sys.platform != "win32":
            import os

            assert os.access(path, os.X_OK), f"scripts/{script} is not executable"


def test_demo_gif_build_vhs_keeps_recording_fixes():
    """scripts/demo-gif-build-vhs.sh must keep both patches that make the demo
    GIF re-recordable (see docs/assets/README.md): the ffmpeg context detach
    and the xterm.js renderer no-pause/refresh hooks. Without them, upstream
    VHS either exits without writing the GIF or drops lines nondeterministically."""
    path = REPO_ROOT / "scripts" / "demo-gif-build-vhs.sh"
    assert path.is_file(), "scripts/demo-gif-build-vhs.sh missing"
    text = path.read_text(encoding="utf-8")
    assert 'exec.Command(' in text, "script must patch the ffmpeg CommandContext bug"
    assert "_isPaused" in text, "script must neutralize the xterm.js renderer pause"
    assert "term.refresh(0, term.rows - 1)" in text, "script must refresh rows before each frame capture"


def test_demo_gif_tape_exists_and_keeps_storyboard():
    """The VHS tape in scripts/demo-gif.tape is the documented source of
    docs/assets/demo-v0.10.gif (see docs/assets/README.md). Guard its key
    properties so a re-record cannot regress the font fix or drop the story."""
    path = REPO_ROOT / "scripts" / "demo-gif.tape"
    assert path.is_file(), "scripts/demo-gif.tape missing"
    tape = path.read_text(encoding="utf-8")
    assert 'FontFamily Menlo' in tape, "tape must use the Menlo font"
    assert "LetterSpacing 0" in tape, "tape must not add letter spacing"
    assert ". scripts/demo-gif-setup.sh" in tape, "tape must source the hidden setup script"
    assert "Durable execution for coding agents." in tape, "tape must end on the tagline"

