"""The local HTTP API refuses browser-driven requests, and tokens stay private."""

from __future__ import annotations

import http.client
import json
import os
import stat

import pytest

from maestro import mcp_server
from maestro.agents import AgentRegistry, AgentSpec
from maestro.daemon import MaestroDaemon, _host_name, _write_private


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
