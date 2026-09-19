"""``maestro doctor``: report structure, blocking rules, formatting, CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from maestro import VERSION, cli
from maestro.agents import KNOWN_CLIS
from maestro.doctor import format_doctor, run_doctor


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git_repo(tmp_path: Path, name: str = "ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(ws), *args], text=True, capture_output=True, env=env)

    git("init", "-q")
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "initial")
    return ws


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(h))
    return h


class _Handler(BaseHTTPRequestHandler):
    status_code = 200

    def do_GET(self):
        self.send_response(type(self).status_code)
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, *args):
        pass


def _start_server(status_code: int) -> tuple[ThreadingHTTPServer, str]:
    handler = type(f"H{status_code}", (_Handler,), {"status_code": status_code})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ------------------------------------------------------------------ report

def test_doctor_healthy_environment(home, tmp_path, monkeypatch):
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _fake_bin(fakes, "codex", 'echo "codex-cli 9.9.9"')
    _fake_bin(fakes, "pi", 'echo "pi 0.4.2" >&2')  # stderr-only version output
    monkeypatch.setenv("PATH", f"{fakes}{os.pathsep}/usr/bin{os.pathsep}/bin")

    ws = _git_repo(tmp_path)
    (ws / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")

    report = run_doctor(ws)
    assert report["ok"] is True and "problems" not in report
    assert report["maestro"]["version"] == VERSION
    assert report["state"]["exists"] is True and report["state"]["writable"] is True
    assert report["git"]["installed"] is True and report["git"]["version"]

    agents = {a["name"]: a for a in report["agents"]}
    assert agents["codex"]["found"] is True and agents["codex"]["version"] == "codex-cli 9.9.9"
    assert agents["pi"]["found"] is True and agents["pi"]["version"] == "pi 0.4.2"  # from stderr
    assert agents["hermes"]["found"] is False and agents["hermes"]["status"] == "not found"

    w = report["workspace"]
    assert w["is_git_repo"] is True and w["git_root"] == str(ws)
    assert w["project_type"] == "python"
    assert w["verification_command"] is not None

    assert report["daemon"]["reachable"] is False and report["daemon"]["status"] == "no daemon configured"
    assert report["budget"]["per_agent_usd"] is None and report["budget"]["daily_usd"] is None

    text = format_doctor(report)
    assert text.startswith("Maestro doctor")
    assert f"version {VERSION}" in text
    assert "Codex (codex) — codex-cli 9.9.9 — available" in text
    assert "not a git repository" not in text
    assert "environment is usable" in text


def test_doctor_fresh_install_state_dir_created_by_maestro(tmp_path, monkeypatch):
    # Fresh install: MAESTRO_HOME does not exist yet. Maestro() creates it and
    # writes state files into it, so the writability probe must re-run AFTER
    # construction — otherwise a brand-new install is blocked as "not writable"
    # (regression).
    fresh = tmp_path / "does-not-exist-yet"
    monkeypatch.setenv("MAESTRO_HOME", str(fresh))
    ws = _git_repo(tmp_path)

    report = run_doctor(ws)
    assert fresh.is_dir()  # created by the doctor's Maestro instance
    assert report["state"]["exists"] is True and report["state"]["writable"] is True
    assert report["ok"] is True and "problems" not in report


def test_doctor_missing_git_is_not_blocking(home, tmp_path, monkeypatch):
    fakes = tmp_path / "fakes"
    fakes.mkdir()  # empty: no git anywhere on PATH
    monkeypatch.setenv("PATH", str(fakes))
    ws = tmp_path / "plain"
    ws.mkdir()

    report = run_doctor(ws)
    assert report["ok"] is True  # missing optional tooling never blocks
    assert report["git"]["installed"] is False and report["git"]["version"] is None
    assert report["workspace"]["is_git_repo"] is False
    assert report["workspace"]["project_type"] == "unknown"
    text = format_doctor(report)
    assert "✗ git not found on PATH" in text


def test_doctor_git_probe_edge_cases(home, tmp_path, monkeypatch):
    # Broken executable: found on PATH but fails to run -> installed, no version.
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "git").write_text("not a real script\n", encoding="utf-8")
    (broken / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(broken))
    assert run_doctor(tmp_path)["git"] == {"installed": True, "version": None}

    # Executable that prints nothing: installed, version stays None.
    quiet = tmp_path / "quiet"
    quiet.mkdir()
    _fake_bin(quiet, "git", "exit 0")
    monkeypatch.setenv("PATH", str(quiet))
    assert run_doctor(tmp_path)["git"] == {"installed": True, "version": None}

    # Agent binary found on PATH but failing to execute: available, no version.
    badagent = tmp_path / "badagent"
    badagent.mkdir()
    (badagent / "codex").write_text("not a real script\n", encoding="utf-8")
    (badagent / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", str(badagent))
    agents = {a["name"]: a for a in run_doctor(tmp_path)["agents"]}
    assert agents["codex"]["found"] is True and agents["codex"]["version"] is None


def test_doctor_daemon_status_variants(home, tmp_path, monkeypatch):
    # Live server, no token: reachable, auth none.
    server, url = _start_server(200)
    try:
        monkeypatch.setenv("MAESTRO_DAEMON_URL", url)
        d = run_doctor(tmp_path)["daemon"]
        assert d["reachable"] is True and d["auth"] == "none" and d["status"] == "ok"
        assert "— auth:" not in format_doctor({"daemon": d})

        # Live server, with token: auth reported.
        monkeypatch.setenv("MAESTRO_DAEMON_TOKEN", "sekret")
        d = run_doctor(tmp_path)["daemon"]
        assert d["auth"] == "token" and d["status"] == "ok"
    finally:
        server.shutdown()

    # 401 with token -> reachable but unauthorized.
    server, url = _start_server(401)
    try:
        monkeypatch.setenv("MAESTRO_DAEMON_URL", url)
        monkeypatch.setenv("MAESTRO_DAEMON_TOKEN", "wrong")
        d = run_doctor(tmp_path)["daemon"]
        assert d["reachable"] is True and d["auth"] == "token" and d["status"] == "unauthorized"

        # 401 without token -> auth "missing".
        monkeypatch.delenv("MAESTRO_DAEMON_TOKEN")
        d = run_doctor(tmp_path)["daemon"]
        assert d["auth"] == "missing" and d["status"] == "unauthorized"
    finally:
        server.shutdown()

    # Other HTTP errors are reported with the code.
    server, url = _start_server(500)
    try:
        monkeypatch.setenv("MAESTRO_DAEMON_URL", url)
        d = run_doctor(tmp_path)["daemon"]
        assert d["reachable"] is False and d["status"] == "http 500"
    finally:
        server.shutdown()

    # Closed port: unreachable.
    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://127.0.0.1:1")
    d = run_doctor(tmp_path)["daemon"]
    assert d["reachable"] is False and d["status"] == "unreachable"
    assert "✗ not reachable at http://127.0.0.1:1 (unreachable)" in format_doctor({"daemon": d})

    # Stale marker (dead pid) is rejected like no marker.
    monkeypatch.delenv("MAESTRO_DAEMON_URL")
    child = subprocess.Popen(["true"])
    child.wait()
    dead_pid = child.pid  # reaped: guaranteed dead
    (home / "daemon.json").write_text(json.dumps({"pid": dead_pid, "port": 9, "host": "127.0.0.1"}), encoding="utf-8")
    assert run_doctor(tmp_path)["daemon"]["status"] == "no daemon configured"

    # Malformed marker: also rejected, never a crash.
    (home / "daemon.json").write_text("not json", encoding="utf-8")
    assert run_doctor(tmp_path)["daemon"]["status"] == "no daemon configured"

    # Live marker (own pid) with a closed port: resolved but unreachable.
    (home / "daemon.json").write_text(json.dumps({"pid": os.getpid(), "port": 9}), encoding="utf-8")
    d = run_doctor(tmp_path)["daemon"]
    assert d["reachable"] is False and d["url"] == "http://127.0.0.1:9" and d["status"] == "unreachable"

    # Live marker with a token against a live server: auth from the marker.
    server, url = _start_server(200)
    try:
        port = int(url.rsplit(":", 1)[1])
        (home / "daemon.json").write_text(json.dumps({"pid": os.getpid(), "port": port, "host": "127.0.0.1", "token": "tok"}), encoding="utf-8")
        d = run_doctor(tmp_path)["daemon"]
        assert d["reachable"] is True and d["auth"] == "token" and d["status"] == "ok"
    finally:
        server.shutdown()


def test_doctor_invalid_configuration_is_blocking(home, tmp_path):
    # Malformed TOML is tolerated (skipped); a semantically invalid value blocks.
    (home / "config.toml").write_text('[storage]\nbackend = "sqlite"\n', encoding="utf-8")
    ws = _git_repo(tmp_path)
    report = run_doctor(ws)
    assert report["ok"] is False
    assert any(p.startswith("invalid Maestro configuration") for p in report["problems"])
    assert report["state"]["error"].startswith("invalid configuration:")
    text = format_doctor(report)
    assert "✗ invalid Maestro configuration" in text and "✗ invalid Maestro configuration" in text


def test_doctor_unwritable_state_dir_is_blocking(home, tmp_path):
    try:
        home.chmod(stat.S_IREAD | stat.S_IEXEC)  # r-x: not writable
        ws = _git_repo(tmp_path)
        report = run_doctor(ws)
        assert report["ok"] is False
        assert any("state directory unusable" in p for p in report["problems"])
        assert report["state"]["error"].startswith("state directory unusable:")
    finally:
        home.chmod(0o755)


def test_doctor_state_dir_write_check_flag(home, tmp_path, monkeypatch):
    # State dir exists and Maestro works, but the writability probe says no.
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    ws = _git_repo(tmp_path)
    report = run_doctor(ws)
    assert report["ok"] is False
    assert any("state directory is not writable" in p for p in report["problems"])


def test_doctor_missing_explicit_workspace_is_blocking(home, tmp_path):
    report = run_doctor(tmp_path / "does-not-exist")
    assert report["ok"] is False
    assert any("workspace does not exist" in p for p in report["problems"])
    # The message appears in both the Workspace section and the Result problems.
    assert format_doctor(report).count("✗ workspace does not exist") >= 1

    # Without an explicit workspace (cwd default) this never blocks.
    report = run_doctor(None)
    assert "workspace does not exist" not in " ".join(report.get("problems", []))


def test_doctor_workspace_project_types(home, tmp_path):
    # Detection is priority-ordered, so each marker gets its own fresh workspace.
    cases = [
        ("pyproject.toml", "[project]\n", "python"),
        ("package.json", "{}\n", "node"),
        ("go.mod", "module m\n", "go"),
        ("Cargo.toml", "[package]\n", "rust"),
        ("Makefile", "all:\n\t@true\n", "make"),
    ]
    for i, (marker, content, expected) in enumerate(cases):
        ws = tmp_path / f"proj-{i}"
        ws.mkdir()
        (ws / marker).write_text(content, encoding="utf-8")
        assert run_doctor(ws)["workspace"]["project_type"] == expected

    bare = tmp_path / "bare"
    bare.mkdir()
    assert run_doctor(bare)["workspace"]["project_type"] == "unknown"


def test_doctor_verification_command_failure_degrades(home, tmp_path, monkeypatch):
    import maestro.doctor as doctor_mod

    def boom(_ws):
        raise ValueError("no verification possible")

    monkeypatch.setattr(doctor_mod, "_verification_command", boom)
    ws = _git_repo(tmp_path)
    assert run_doctor(ws)["workspace"]["verification_command"] is None


def test_doctor_registered_agent_entries(home, tmp_path, monkeypatch):
    from maestro.agents import AgentRegistry, AgentSpec

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    good = fakes / "myagent"
    _fake_bin(fakes, "myagent", 'echo "myagent 1.0"')
    monkeypatch.setenv("PATH", str(fakes))

    registry = AgentRegistry(home)
    registry.save(AgentSpec(name="myagent", kind="generic", command=f"{good} {{prompt}}", input_mode="arg", output_format="jsonl"))
    registry.save(AgentSpec(name="remote-agent", kind="a2a_remote", command="http://127.0.0.1:1/card.json"))
    # A registered spec duplicating a builtin row (name == kind) is skipped.
    registry.save(AgentSpec(name="pi", kind="pi", display_name="Pi (custom)"))

    report = run_doctor(tmp_path)
    entries = {a["name"]: a for a in report["agents"]}
    assert entries["myagent"]["found"] is True and entries["myagent"]["version"] == "myagent 1.0"
    remote = entries["remote-agent"]
    assert remote["url"] == "http://127.0.0.1:1/card.json" and remote["status"] == "unreachable"
    assert sum(1 for a in report["agents"] if a["name"] == "pi") == 1  # builtin row only

    # A registry entry whose status() raises is reported as an error, not fatal.
    registry.save(AgentSpec(name="broken-agent", kind="generic", command=f"{good} {{prompt}}", input_mode="arg", output_format="jsonl"))
    original = AgentRegistry.status

    def flaky(self, name):
        if name == "broken-agent":
            raise RuntimeError("boom")
        return original(self, name)

    monkeypatch.setattr(AgentRegistry, "status", flaky)
    report = run_doctor(tmp_path)
    entries = {a["name"]: a for a in report["agents"]}
    assert entries["broken-agent"]["status"] == "error"
    assert format_doctor(report).count("registry entry error") == 1

    # An unreadable registry degrades to the builtin rows only.
    monkeypatch.setattr(AgentRegistry, "status", original)
    import maestro.doctor as doctor_mod

    def no_registry(_state_dir):
        raise OSError("registry unavailable")

    monkeypatch.setattr(doctor_mod, "AgentRegistry", no_registry)
    report = run_doctor(tmp_path)
    builtin_kinds = {kind for _, kind, _ in KNOWN_CLIS}
    assert all(a["name"] in builtin_kinds for a in report["agents"])


# ------------------------------------------------------------------ budget

def test_doctor_budget_sections(home, tmp_path, monkeypatch):
    from maestro.core import Maestro

    ws = _git_repo(tmp_path)

    # No caps: reported as such.
    report = run_doctor(ws)
    assert report["budget"]["per_agent_usd"] is None and report["budget"]["daily_usd"] is None
    assert "no budget caps configured" in format_doctor(report)

    # Per-agent cap with recorded spend for today (UTC).
    m = Maestro(ws)
    m._write_claim(
        "task-20260101-000000-budgetaa",
        "task_runtime",
        json.dumps({
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "attempts": [{"agent": "codex", "ok": True, "usage": {"cost_usd": 1.23}}],
        }),
    )
    m.close()
    monkeypatch.setenv("MAESTRO_BUDGET_PER_AGENT_USD", "25")
    report = run_doctor(ws)
    assert report["budget"]["per_agent_usd"] == 25.0
    assert report["budget"]["spent_today_usd"] == pytest.approx(1.23)
    assert report["budget"]["spent_by_agent"] == {"codex": 1.23}
    text = format_doctor(report)
    assert "per-agent cap: $25.00" in text and "codex $1.23" in text

    # Daily cap only: the per-agent line is absent.
    monkeypatch.delenv("MAESTRO_BUDGET_PER_AGENT_USD")
    monkeypatch.setenv("MAESTRO_BUDGET_DAILY_USD", "50")
    report = run_doctor(ws)
    text = format_doctor(report)
    assert "daily cap: $50.00" in text and "per-agent cap" not in text


def test_doctor_budget_zero_spend_rendering(home, tmp_path, monkeypatch):
    # Cap configured but nothing spent yet: the join falls back to $0.00.
    monkeypatch.setenv("MAESTRO_BUDGET_PER_AGENT_USD", "10")
    report = run_doctor(tmp_path)
    assert "spent so far: $0.00" in format_doctor(report)


# -------------------------------------------------------------------- CLI

def test_cli_doctor_human_and_json(home, tmp_path, capsys):
    ws = _git_repo(tmp_path)  # build the repo before touching PATH

    assert cli.main(["doctor", "--workspace", str(ws)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Maestro doctor") and "environment is usable" in out

    assert cli.main(["doctor", "--json", "--workspace", str(ws)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["maestro"]["version"] == VERSION

    # The --project alternative resolves the same way.
    assert cli.main(["doctor", "--json", "--project", str(ws)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # Blocking problem: exit code 1, problems visible in both renderings.
    (home / "config.toml").write_text('[storage]\nbackend = "sqlite"\n', encoding="utf-8")
    assert cli.main(["doctor", "--workspace", str(ws)]) == 1
    assert "✗ invalid Maestro configuration" in capsys.readouterr().out
    assert cli.main(["doctor", "--json", "--workspace", str(ws)]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["problems"]


# ------------------------------------------------------- format (synthetic)

def test_format_doctor_synthetic_branches():
    base = {
        "maestro": {"version": "0.9.0"},
        "python": {"version": "3.11", "executable": "/x/python"},
        "system": {"os": "darwin", "arch": "arm64"},
        "state": {"dir": "/s", "exists": True, "writable": True, "config_paths": ["/s/config.toml"], "storage_backend": None},
        "daemon": {"reachable": False, "url": None, "auth": "none", "status": "no daemon configured"},
        "git": {"installed": True, "version": None},  # version None: no suffix
        "agents": [
            {"name": "codex", "kind": "codex", "display_name": "Codex", "binary": "codex", "status": "available"},  # no version
            {"name": "hermes", "kind": "hermes", "display_name": "Hermes Agent", "binary": "hermes", "status": "not found"},
        ],
        "workspace": {
            "path": "/w", "exists": True, "is_git_repo": False, "git_root": None,
            "readable": True, "writable": False, "project_type": "unknown", "verification_command": None,
        },
        "budget": {"per_agent_usd": None, "daily_usd": 5.0},
        "ok": False,
        "problems": ["state directory is not writable: /s"],
    }
    text = format_doctor(base)
    assert "missing (created on first use)" not in text
    assert "NOT WRITABLE" not in text  # exists+writable True here
    assert "git — available" not in text  # version-less rendering has no dash
    assert "Hermes Agent (hermes) — not found" in text
    assert "not a git repository" in text
    assert "read-only" in text
    assert "project type: unknown" in text
    assert "daily cap: $5.00" in text and "per-agent cap" not in text
    assert "✗ state directory is not writable: /s" in text

    # State missing (no error): the created-on-first-use detail.
    missing = dict(base)
    missing["state"] = {"dir": "/s", "exists": False, "writable": False, "config_paths": []}
    assert "missing (created on first use)" in format_doctor(missing)

    # State exists but not writable.
    nowrite = dict(base)
    nowrite["state"] = {"dir": "/s", "exists": True, "writable": False, "config_paths": []}
    assert "NOT WRITABLE" in format_doctor(nowrite)

    # Storage backend + present/absent config files.
    cfg = dict(base)
    cfg["state"] = {"dir": "/s", "exists": True, "writable": True, "config_paths": ["/s/config.toml", "/s/absent.toml"], "storage_backend": "journal"}
    t = format_doctor(cfg)
    assert "storage backend: journal" in t and "(not present)" in t

    # Daemon reachable with token auth.
    live = dict(base)
    live["daemon"] = {"reachable": True, "url": "http://127.0.0.1:9", "auth": "token", "status": "ok"}
    assert "reachable at http://127.0.0.1:9 — auth: token" in format_doctor(live)

    # Git not installed.
    nogit = dict(base)
    nogit["git"] = {"installed": False, "version": None}
    assert "✗ git not found on PATH" in format_doctor(nogit)

    # Agent with URL and agent error rows.
    agents = dict(base)
    agents["agents"] = [
        {"name": "remote", "kind": "a2a_remote", "display_name": "Remote", "url": "http://x", "status": "unreachable"},
        {"name": "broken", "kind": "generic", "display_name": "Broken", "status": "error"},
    ]
    t = format_doctor(agents)
    assert "Remote (?) — http://x — unreachable" in t and "Broken — registry entry error" in t

    # Workspace missing entirely.
    nows = dict(base)
    nows["workspace"] = {"path": "/w", "exists": False}
    assert "✗ workspace does not exist" in format_doctor(nows)

    # Not readable at all.
    noread = dict(base)
    noread["workspace"] = {**base["workspace"], "readable": False, "writable": False}
    assert "not readable" in format_doctor(noread)

    # Verification command rendered.
    vcmd = dict(base)
    vcmd["workspace"] = {**base["workspace"], "is_git_repo": True, "git_root": "/w", "readable": True, "writable": True,
                         "project_type": "python", "verification_command": ["make", "check"]}
    t = format_doctor(vcmd)
    assert "✓ git repository (root: /w)" in t and "verification command: make check" in t

    # Budget: per-agent with spend, daily absent.
    budget = dict(base)
    budget["budget"] = {"per_agent_usd": 10.0, "daily_usd": None, "spent_by_agent": {"codex": 2.5}}
    assert "per-agent cap: $10.00 — spent so far: codex $2.50" in format_doctor(budget)

    # All good.
    good = dict(base)
    good["ok"] = True
    good.pop("problems")
    assert "✓ environment is usable" in format_doctor(good)
