"""Login-shell environment inheritance for spawned agents.

The daemon may be started from a context (GUI app, launchd, an old terminal)
that lacks variables the user's shell profile exports — API keys above all.
capture_login_env() snapshots ``$SHELL -lc env`` once per process and
worker_environment() layers it under the daemon's own environment so agents
see the same defaults as an interactive session.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from maestro.adapters import base as adapter_base
from maestro.adapters.base import (
    MAESTRO_CONTEXT_ENV,
    capture_login_env,
    worker_environment,
)


def _env_listing(*entries: str) -> str:
    """Shell code printing what `$SHELL -lc` prints for the real command: a
    marker, then NUL-separated NAME=value entries (the `env -0` format)."""
    return "printf '\\0__MAESTRO_ENV__\\0'\n" + "\n".join(f"printf '%s\\0' '{e}'" for e in entries)


def _fake_shell(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture(autouse=True)
def _reset_login_env_cache(monkeypatch):
    """Each test starts with no cached snapshot so it controls the capture."""
    monkeypatch.setattr(adapter_base, "_LOGIN_ENV_CACHE", None)
    yield


def test_capture_parses_key_value_lines(monkeypatch, tmp_path):
    shell = tmp_path / "fake-shell"
    # Profile output before the marker (a banner, even one shaped like a
    # variable) is ignored; entries that are not NAME=value are skipped.
    _fake_shell(shell, 'echo "hello world"\necho "BANNER=not-a-variable"\n' + _env_listing("MY_KEY=secret-value", "not a var line", "PATH=/usr/bin", "12345"))
    monkeypatch.setenv("SHELL", str(shell))
    env = capture_login_env()
    assert env == {"MY_KEY": "secret-value", "PATH": "/usr/bin"}


def test_capture_keeps_multi_line_values_whole(monkeypatch, tmp_path):
    shell = tmp_path / "fake-shell"
    _fake_shell(shell, _env_listing("PEM_KEY=-----BEGIN KEY-----\nFAKE=continuation\n-----END KEY-----", "OTHER=1"))
    monkeypatch.setenv("SHELL", str(shell))
    env = capture_login_env()
    assert env == {"PEM_KEY": "-----BEGIN KEY-----\nFAKE=continuation\n-----END KEY-----", "OTHER": "1"}


def test_capture_runs_env_through_the_real_shell(monkeypatch, tmp_path):
    shell = tmp_path / "login-shell"
    # A login shell that prints a banner, then runs the command it was given.
    _fake_shell(shell, 'echo "Welcome"\nexec /bin/sh -c "$2"')
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setenv("MULTI_LINE_VALUE", "first\nsecond")
    env = capture_login_env()
    assert env["MULTI_LINE_VALUE"] == "first\nsecond"
    assert "Welcome" not in "".join(env)


def test_capture_without_the_marker_returns_empty(monkeypatch, tmp_path):
    shell = tmp_path / "fake-shell"
    _fake_shell(shell, 'echo "K=1"')  # a shell that ignored the command
    monkeypatch.setenv("SHELL", str(shell))
    assert capture_login_env() == {}


def test_capture_missing_shell_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL", str(tmp_path / "nope"))
    assert capture_login_env() == {}


def test_capture_timeout_returns_empty(monkeypatch, tmp_path):
    shell = tmp_path / "slow-shell"
    _fake_shell(shell, "sleep 5")
    monkeypatch.setenv("SHELL", str(shell))
    assert capture_login_env(timeout_s=0.3) == {}


def test_capture_bad_timeout_value_returns_empty(monkeypatch, tmp_path):
    shell = tmp_path / "fake-shell"
    _fake_shell(shell, 'echo "K=1"')
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setenv("MAESTRO_LOGIN_ENV_TIMEOUT_S", "not-a-number")
    assert capture_login_env() == {}


def test_capture_disabled_by_env_flag(monkeypatch):
    monkeypatch.setenv("MAESTRO_LOGIN_ENV", "0")
    monkeypatch.setenv("SHELL", "/bin/sh")
    assert capture_login_env() == {}


def test_capture_caches_result(monkeypatch, tmp_path):
    counter = tmp_path / "runs"
    shell = tmp_path / "counting-shell"
    _fake_shell(shell, _env_listing("K=1") + '\necho run >> "$COUNTER_FILE"')
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setenv("COUNTER_FILE", str(counter))
    first = capture_login_env()
    second = capture_login_env()
    assert first == second == {"K": "1"}
    assert counter.read_text(encoding="utf-8").count("run") == 1  # shell ran exactly once


def test_worker_env_layers_login_defaults_under_daemon_env(monkeypatch, tmp_path):
    shell = tmp_path / "fake-shell"
    _fake_shell(shell, _env_listing("GROVE_API_KEY=from-profile", "PATH=/profile/bin"))
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setenv("PATH", "/daemon/bin")  # daemon value wins on conflict
    env = worker_environment("t-1")
    assert env["GROVE_API_KEY"] == "from-profile"  # profile default flows through
    assert env["PATH"] == "/daemon/bin"            # daemon env wins on conflict
    assert env[MAESTRO_CONTEXT_ENV] == "1"         # markers still authoritative


def test_spawn_agent_sees_profile_variable(monkeypatch, tmp_path):
    """End-to-end: a fake agent echoes a variable that only the profile exports."""
    from maestro.adapters.generic import GenericAdapter
    from maestro.agents import AgentSpec

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "probe-agent").write_text('#!/bin/sh\necho "GROVE=$GROVE_API_KEY"\n', encoding="utf-8")
    (bindir / "probe-agent").chmod(0o755)
    shell = tmp_path / "fake-shell"
    _fake_shell(shell, _env_listing("GROVE_API_KEY=profile-secret"))
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    result = GenericAdapter(AgentSpec(name="probe", kind="generic", command="probe-agent {prompt}")).run(
        "x", tmp_path, "t-2", log_dir=tmp_path / "logs"
    )
    assert result.ok is True
    assert "GROVE=profile-secret" in Path(result.output_path).read_text(encoding="utf-8")
