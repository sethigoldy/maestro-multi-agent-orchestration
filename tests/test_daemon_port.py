"""The daemon's port: one default port, and ways to choose another."""

from __future__ import annotations

import json
import socket

import pytest

from maestro import daemonctl


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("MAESTRO_HOME", str(path))
    monkeypatch.delenv("MAESTRO_DAEMON_PORT", raising=False)
    return path


def test_default_port_is_9785(home):
    assert daemonctl.DEFAULT_DAEMON_PORT == 9785
    assert daemonctl.resolve_port(None, home) == 9785


def test_port_setting_order_flag_then_environment_then_config(home, monkeypatch):
    (home / "config.toml").write_text("[daemon]\nport = 9900\n", encoding="utf-8")
    assert daemonctl.resolve_port(None, home) == 9900  # config
    monkeypatch.setenv("MAESTRO_DAEMON_PORT", "9901")
    assert daemonctl.resolve_port(None, home) == 9901  # environment beats config
    assert daemonctl.resolve_port(9902, home) == 9902  # flag beats both
    assert daemonctl.resolve_port(0, home) == 0  # 0 means any free port


@pytest.mark.parametrize("value", ["-1", "65536", "abc", ""])
def test_invalid_port_in_the_environment(home, monkeypatch, value):
    monkeypatch.setenv("MAESTRO_DAEMON_PORT", value)
    with pytest.raises(ValueError, match="MAESTRO_DAEMON_PORT must be a port number from 0 to 65535"):
        daemonctl.resolve_port(None, home)


@pytest.mark.parametrize("value", ["-1", "70000", '"9785"', "true"])
def test_invalid_port_in_the_config(home, value):
    (home / "config.toml").write_text(f"[daemon]\nport = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[daemon\] port .* must be a port number from 0 to 65535"):
        daemonctl.resolve_port(None, home)


def test_invalid_flag_port(home):
    with pytest.raises(ValueError, match="--port must be a port number from 0 to 65535"):
        daemonctl.resolve_port(70000, home)


def test_unreadable_config_is_ignored_for_the_port(home):
    (home / "config.toml").write_text("this is not toml [", encoding="utf-8")
    assert daemonctl.resolve_port(None, home) == 9785


def test_background_daemon_is_started_on_the_resolved_port(home):
    command = daemonctl._spawn_command(home, 9785)
    assert command[-2:] == ["--port", "9785"]


def test_start_on_a_busy_port_says_which_port_and_how_to_choose_another(home):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(RuntimeError) as info:
            daemonctl.start(home, port=port, ready_timeout_s=20)
    message = str(info.value)
    assert f"port {port} is already in use" in message
    assert "--port" in message and "MAESTRO_DAEMON_PORT" in message and "[daemon] port" in message


def test_daemon_main_uses_the_resolved_port_when_no_flag_is_given(home, monkeypatch, capsys):
    import maestro.daemon_main as dm

    seen = {}

    def fake_run(state_dir=None, port=0, bind="127.0.0.1", allowed_origins=None):
        seen["port"] = port
        raise SystemExit(0)

    monkeypatch.setattr(dm, "run_daemon", fake_run)
    monkeypatch.setenv("MAESTRO_DAEMON_PORT", "9911")
    with pytest.raises(SystemExit):
        dm.main(["--state-dir", str(home)])
    assert seen["port"] == 9911


def test_daemon_main_reports_a_busy_port_plainly(home, capsys):
    import maestro.daemon_main as dm

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        rc = dm.main(["--state-dir", str(home), "--port", str(port)])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"port {port} is already in use" in err


def test_cli_daemon_start_and_restart_accept_port(home, monkeypatch, capsys):
    from maestro import cli

    calls = []

    class Info:
        running = True
        already_running = False
        pid = 1
        port = 9955
        url = "http://127.0.0.1:9955"
        state_dir = home
        detail = ""

    def fake_start(state_dir=None, *, port=None, **kw):
        calls.append(("start", port))
        return Info()

    def fake_restart(state_dir=None, *, port=None, **kw):
        calls.append(("restart", port))
        return Info()

    monkeypatch.setattr(daemonctl, "start", fake_start)
    monkeypatch.setattr(daemonctl, "restart", fake_restart)
    monkeypatch.setattr(cli, "_format_daemon_info", lambda info, verb: verb)
    assert cli.main(["daemon", "start", "--port", "9955"]) == 0
    assert cli.main(["daemon", "restart", "--port", "9956"]) == 0
    assert cli.main(["daemon", "start"]) == 0
    assert calls == [("start", 9955), ("restart", 9956), ("start", None)]


def test_daemon_main_refuses_an_invalid_port_setting(home, monkeypatch, capsys):
    import maestro.daemon_main as dm

    monkeypatch.setenv("MAESTRO_DAEMON_PORT", "nope")
    assert dm.main(["--state-dir", str(home)]) == 2
    assert "MAESTRO_DAEMON_PORT must be a port number" in capsys.readouterr().err


def test_daemon_main_does_not_hide_other_bind_errors(home, monkeypatch):
    import errno

    import maestro.daemon_main as dm

    def fake_run(**kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(dm, "run_daemon", fake_run)
    with pytest.raises(OSError, match="Permission denied"):
        dm.main(["--state-dir", str(home), "--port", "80"])
