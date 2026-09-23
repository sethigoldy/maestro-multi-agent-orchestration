"""The local HTTP API refuses browser-driven requests, and tokens stay private."""

from __future__ import annotations

import http.client
import json
import os
import stat

import pytest

from maestro import mcp_server
from maestro.agents import AgentRegistry, AgentSpec
from maestro import agents as agents_module
from maestro.daemon import MaestroDaemon, _host_name, _normalize_origin, _write_private


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("MAESTRO_DISCOVERY_TTL", "0")
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    d.start_http(0)
    yield d
    d.stop()


def _request(daemon, method, path="/", *, body=None, headers=None, host=None):
    """Send one request; ``host=None`` sends the normal 127.0.0.1 Host header,
    ``host=""`` sends none at all."""
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True)
        if host != "":
            conn.putheader("Host", host if host is not None else f"127.0.0.1:{daemon.port}")
        data = json.dumps(body).encode() if body is not None else b""
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "task-20250101-000000-abcdef"}}
_JSON = {"Content-Type": "application/json"}


# ------------------------------------------------------------ browser requests
def test_post_without_json_content_type_is_refused(daemon):
    """A web page can POST text/plain cross-site without a preflight; that must not reach the dispatcher."""
    status, body = _request(daemon, "POST", body=_LIST, headers={"Content-Type": "text/plain"})
    assert status == 403 and "application/json" in body["error"]
    status, _ = _request(daemon, "POST", body=_LIST)  # no Content-Type at all
    assert status == 403
    status, _ = _request(daemon, "POST", body=_LIST, headers={"Content-Type": "application/json; charset=utf-8"})
    assert status == 200  # reached the dispatcher


def test_post_from_a_foreign_origin_is_refused(daemon):
    status, body = _request(daemon, "POST", body=_LIST, headers={**_JSON, "Origin": "https://evil.example"})
    assert status == 403 and "cross-origin" in body["error"]
    status, _ = _request(daemon, "POST", body=_LIST, headers={**_JSON, "Origin": "null"})
    assert status == 403
    same = f"http://127.0.0.1:{daemon.port}"
    status, _ = _request(daemon, "POST", body=_LIST, headers={**_JSON, "Origin": same})
    assert status == 200  # same origin is allowed through to the dispatcher
    # The rule is exact: same host but a different port is another origin.
    status, _ = _request(daemon, "POST", body=_LIST, headers={**_JSON, "Origin": f"http://127.0.0.1:{daemon.port + 1}"})
    assert status == 403


def test_allowed_origins_let_a_reverse_proxy_through(tmp_path, monkeypatch):
    """A proxy that rewrites Host to the daemon's own address still passes the page's Origin on."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.setenv("MAESTRO_DISCOVERY_TTL", "0")
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0, allowed_origins=["https://Maestro.Example.com/"])
    d.start_http(0)
    try:
        assert d.allowed_origins == frozenset({"https://maestro.example.com"})
        status, _ = _request(d, "POST", body=_LIST, headers={**_JSON, "Origin": "https://maestro.example.com"})
        assert status == 200
        status, _ = _request(d, "POST", body=_LIST, headers={**_JSON, "Origin": "http://maestro.example.com"})
        assert status == 403  # another scheme is another origin
        status, _ = _request(d, "POST", body=_LIST, headers={**_JSON, "Origin": "https://evil.example"})
        assert status == 403
        status, _ = _request(d, "POST", body=_LIST, headers=_JSON)
        assert status == 200  # no Origin at all: a non-browser client
    finally:
        d.stop()


def test_allowed_origins_come_from_the_environment_when_not_passed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))  # the daemon sets it; this restores it afterwards
    monkeypatch.setenv("MAESTRO_DAEMON_ALLOWED_ORIGINS", " https://a.example , ,http://b.example:8443")
    seen = []
    for kwargs in ({}, {"allowed_origins": []}):
        d = MaestroDaemon(state_dir=home, start_http=False, **kwargs)
        seen.append(d.allowed_origins)
        d.stop()
    monkeypatch.delenv("MAESTRO_DAEMON_ALLOWED_ORIGINS")
    d = MaestroDaemon(state_dir=home, start_http=False)
    seen.append(d.allowed_origins)
    d.stop()
    assert seen[0] == frozenset({"https://a.example", "http://b.example:8443"})
    assert seen[1] == frozenset()  # an explicit list wins over the environment
    assert seen[2] == frozenset()


@pytest.mark.parametrize("value", ["maestro.example.com", "ftp://maestro.example.com", "https://", "https://x.example/app", "https://x.example?a=1", "https://x.example#f", "null"])
def test_an_invalid_allowed_origin_is_an_error(value):
    with pytest.raises(ValueError, match="allowed origin"):
        _normalize_origin(value)


def test_daemon_main_passes_allow_origin(tmp_path, monkeypatch):
    from maestro.daemon_main import _parse_args, run_daemon

    assert _parse_args([]).allow_origin is None
    args = _parse_args(["--allow-origin", "https://a.example", "--allow-origin", "https://b.example"])
    assert args.allow_origin == ["https://a.example", "https://b.example"]
    monkeypatch.setenv("MAESTRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MAESTRO_DISCOVERY_TTL", "0")
    info = run_daemon(state_dir=str(tmp_path / "home"), port=0, allowed_origins=args.allow_origin)
    try:
        assert info["daemon"].allowed_origins == frozenset({"https://a.example", "https://b.example"})
    finally:
        info["daemon"].stop()


def test_dns_rebinding_host_is_refused_on_a_loopback_daemon(daemon):
    for method, path, body, headers in (("GET", "/tasks", None, {}), ("GET", "/", None, {}), ("POST", "/", _LIST, _JSON)):
        status, reply = _request(daemon, method, path, body=body, headers=headers, host=f"rebind.evil.example:{daemon.port}")
        assert status == 403 and reply["error"] == "forbidden host", (method, path)


@pytest.mark.parametrize("host", ["localhost:{port}", "LOCALHOST", "127.0.0.1", "[::1]:{port}", "::1", ""])
def test_loopback_host_names_are_allowed(daemon, host):
    status, reply = _request(daemon, "GET", "/tasks", host=host.format(port=daemon.port))
    assert status == 200 and "tasks" in reply


def test_a_token_daemon_accepts_any_host_but_needs_the_token(daemon):
    daemon.token = "s3cret"  # what a non-loopback bind sets up
    lan_host = f"192.168.1.20:{daemon.port}"
    assert _request(daemon, "GET", "/tasks", host=lan_host)[0] == 401
    assert _request(daemon, "GET", "/tasks", host=lan_host, headers={"Authorization": "Bearer wrong"})[0] == 401
    assert _request(daemon, "GET", "/tasks", host=lan_host, headers={"Authorization": "Bearer s3cret"})[0] == 200
    assert _request(daemon, "GET", "/tasks?token=s3cret", host=lan_host)[0] == 200
    assert _request(daemon, "GET", "/tasks?token=nope", host=lan_host)[0] == 401
    # The token does not excuse a plain-text POST.
    status, _ = _request(daemon, "POST", body=_LIST, host=lan_host, headers={"Authorization": "Bearer s3cret", "Content-Type": "text/plain"})
    assert status == 403


def test_host_name_parsing():
    assert _host_name("localhost:8790") == "localhost"
    assert _host_name("[::1]:8790") == "::1"
    assert _host_name("[::1") == "[::1"
    assert _host_name("::1") == "::1"
    assert _host_name(" Example.COM ") == "example.com"


# ------------------------------------------------------------ token files
def test_daemon_marker_is_private(daemon):
    marker = daemon.state_dir / "daemon.json"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_write_private_tightens_an_existing_world_readable_file(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)
    _write_private(path, '{"token": "x"}')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"token": "x"}


def test_write_private_replaces_the_file_so_an_old_handle_cannot_read_the_new_token(tmp_path):
    """Unix checks permissions only at open, so a handle opened while the file was 0644 must never see the new token."""
    path = tmp_path / "daemon.json"
    path.write_text('{"token": "old"}', encoding="utf-8")
    os.chmod(path, 0o644)
    old_inode = path.stat().st_ino
    with path.open(encoding="utf-8") as old_handle:  # another local user opened it earlier
        _write_private(path, '{"token": "new"}')
        assert old_handle.read() == '{"token": "old"}'
    assert path.stat().st_ino != old_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"token": "new"}
    assert [p.name for p in tmp_path.iterdir()] == ["daemon.json"]  # no temporary file left behind


def test_write_private_removes_its_temporary_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "daemon.json"
    path.write_text("{}", encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(agents_module.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        _write_private(path, '{"token": "x"}')
    assert [p.name for p in tmp_path.iterdir()] == ["daemon.json"]
    assert path.read_text(encoding="utf-8") == "{}"  # the old file is untouched


def test_registry_entry_is_private_and_display_is_redacted(tmp_path):
    registry = AgentRegistry(tmp_path)
    registry.save(AgentSpec(name="remote-b", kind="a2a_remote", command="http://10.0.0.5:8790", token="sekrit"))
    path = tmp_path / "agents" / "remote-b.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    spec = registry.get("remote-b")
    assert spec.token == "sekrit"  # the adapter still gets the real token
    assert spec.to_dict()["token"] == "sekrit"
    assert spec.to_dict(redact=True)["token"] == "<redacted>"
    assert "token" not in AgentSpec(name="plain", kind="codex").to_dict(redact=True)


def test_registry_save_replaces_the_entry_file(tmp_path):
    registry = AgentRegistry(tmp_path)
    registry.save(AgentSpec(name="remote-b", kind="a2a_remote", command="http://10.0.0.5:8790", token="old"))
    path = tmp_path / "agents" / "remote-b.toml"
    os.chmod(path, 0o644)  # as an older version left it
    old_inode = path.stat().st_ino
    with path.open(encoding="utf-8") as old_handle:
        registry.save(AgentSpec(name="remote-b", kind="a2a_remote", command="http://10.0.0.5:8790", token="new"))
        assert "new" not in old_handle.read()
    assert path.stat().st_ino != old_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert registry.get("remote-b").token == "new"
    assert sorted(p.name for p in path.parent.iterdir()) == ["remote-b.toml"]


def test_loading_the_registry_tightens_entries_written_by_older_versions(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    old = agents_dir / "remote-b.toml"
    old.write_text('name = "remote-b"\nkind = "a2a_remote"\ncommand = "http://10.0.0.5:8790"\ntoken = "sekrit"\n', encoding="utf-8")
    os.chmod(old, 0o644)
    private = agents_dir / "codex.toml"
    private.write_text('name = "codex"\nkind = "codex"\n', encoding="utf-8")
    os.chmod(private, 0o600)
    target = tmp_path / "elsewhere.toml"
    target.write_text("x", encoding="utf-8")
    os.chmod(target, 0o644)
    (agents_dir / "link.toml").symlink_to(target)  # a symlink is not followed
    AgentRegistry(tmp_path)
    assert stat.S_IMODE(old.stat().st_mode) == 0o600
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_loading_the_registry_survives_an_entry_it_cannot_chmod(tmp_path, monkeypatch):
    """An entry owned by another user cannot be chmodded; loading must carry on."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    entry = agents_dir / "codex.toml"
    entry.write_text('name = "codex"\nkind = "codex"\n', encoding="utf-8")
    os.chmod(entry, 0o644)

    def refuse(path, mode, **kwargs):
        raise PermissionError("not the owner")

    monkeypatch.setattr(agents_module.os, "chmod", refuse)
    registry = AgentRegistry(tmp_path)
    assert registry.get("codex").kind == "codex"


def test_mcp_agents_list_never_shows_a_token(monkeypatch, tmp_path):
    registry = AgentRegistry(tmp_path)
    registry.save(AgentSpec(name="remote-b", kind="a2a_remote", command="http://10.0.0.5:8790", token="sekrit"))

    class FakeDaemon:
        def __init__(self):
            self.registry = registry

    monkeypatch.setattr(registry, "status", lambda name: {"available": True})
    monkeypatch.setattr(mcp_server, "get_daemon", lambda: FakeDaemon())
    out = mcp_server.agents_list()
    assert "sekrit" not in out
    assert json.loads(out)[0]["token"] == "<redacted>"
