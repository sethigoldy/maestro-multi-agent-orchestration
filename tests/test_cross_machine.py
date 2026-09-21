"""Cross-machine daemon support: --bind, advertised host, bearer-token auth.

Hermetic: every bind target is a loopback address (127.0.0.1 / 127.0.0.2) or
0.0.0.0 with the LAN-IP picker monkeypatched to a fixed value, so no real
network interface is ever touched.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from maestro.daemon import MaestroDaemon


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(h))
    return h


def _http(url: str, method: str = "GET", headers: dict | None = None, body: bytes | None = None):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _marker(home):
    return json.loads((home / "daemon.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ bind + token resolution

def test_loopback_bind_needs_no_token(home, monkeypatch):
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        assert d.token is None
        assert d.advertised_host == "127.0.0.1"
        marker = _marker(home)
        assert marker["host"] == "127.0.0.1" and "token" not in marker
        status, body = _http(f"http://127.0.0.1:{d.port}/tasks")
        assert status == 200 and json.loads(body)["tasks"] == []
    finally:
        d.stop()


def test_all_interfaces_bind_generates_token_and_picks_lan_ip(home, monkeypatch):
    monkeypatch.setattr("maestro.discovery.pick_lan_ip", lambda: "192.0.2.10")
    monkeypatch.delenv("MAESTRO_DAEMON_TOKEN", raising=False)
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, bind="0.0.0.0", max_retries=0, backoff_s=0)
    try:
        assert d.token and len(d.token) >= 20
        assert d.advertised_host == "192.0.2.10"
        marker = _marker(home)
        assert marker["host"] == "127.0.0.1"  # locally reachable via loopback
        assert marker["token"] == d.token
    finally:
        d.stop()


def test_env_token_wins_over_generation(home, monkeypatch):
    monkeypatch.setattr("maestro.discovery.pick_lan_ip", lambda: "192.0.2.10")
    monkeypatch.setenv("MAESTRO_DAEMON_TOKEN", "explicit-token-123")
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, bind="0.0.0.0", max_retries=0, backoff_s=0)
    try:
        assert d.token == "explicit-token-123"
        assert _marker(home)["token"] == "explicit-token-123"
    finally:
        d.stop()


def test_resolve_bind_explicit_ip(home, monkeypatch):
    # explicit non-loopback bind: advertised as-is, locally reachable at that IP, token required
    monkeypatch.delenv("MAESTRO_DAEMON_TOKEN", raising=False)
    d = MaestroDaemon(state_dir=home, start_http=False, bind="10.0.0.5")
    d._resolve_bind()
    assert d.advertised_host == "10.0.0.5" and d.local_host == "10.0.0.5"
    assert d.token is not None


def test_resolve_bind_loopback_needs_nothing(home, monkeypatch):
    monkeypatch.delenv("MAESTRO_DAEMON_TOKEN", raising=False)
    d = MaestroDaemon(state_dir=home, start_http=False, bind="127.0.0.1")
    d._resolve_bind()
    assert d.advertised_host == "127.0.0.1" and d.local_host == "127.0.0.1" and d.token is None


def test_resolve_bind_all_interfaces_picks_lan_ip(home, monkeypatch):
    monkeypatch.setattr("maestro.discovery.pick_lan_ip", lambda: "192.0.2.10")
    d = MaestroDaemon(state_dir=home, start_http=False, bind="0.0.0.0")
    d._resolve_bind()
    assert d.advertised_host == "192.0.2.10" and d.local_host == "127.0.0.1" and d.token is not None


def test_card_reports_advertised_url(home, monkeypatch):
    monkeypatch.setattr("maestro.discovery.pick_lan_ip", lambda: "192.0.2.10")
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, bind="0.0.0.0", max_retries=0, backoff_s=0)
    try:
        assert d.card()["url"] == f"http://192.0.2.10:{d.port}"
    finally:
        d.stop()


# ------------------------------------------------------------------ auth behavior

def _authed_daemon(home, monkeypatch):
    monkeypatch.setattr("maestro.discovery.pick_lan_ip", lambda: "192.0.2.10")
    monkeypatch.delenv("MAESTRO_DAEMON_TOKEN", raising=False)
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, bind="0.0.0.0", max_retries=0, backoff_s=0)
    return d


def test_data_endpoints_require_token(home, monkeypatch):
    d = _authed_daemon(home, monkeypatch)
    try:
        base = f"http://127.0.0.1:{d.port}"
        for url in (f"{base}/tasks", f"{base}/events", f"{base}/.well-known/agent.json"):
            status, body = _http(url)
            assert status == 401 and json.loads(body) == {"error": "unauthorized"}, url
        payload = b'{"jsonrpc":"2.0","id":1,"method":"tasks/list","params":{}}'
        status, _ = _http(f"{base}/", method="POST", headers={"Content-Type": "application/json"}, body=payload)
        assert status == 401
        status, _ = _http(f"{base}/tasks?token=wrong")
        assert status == 401
    finally:
        d.stop()


def test_bearer_header_authorizes(home, monkeypatch):
    d = _authed_daemon(home, monkeypatch)
    try:
        base = f"http://127.0.0.1:{d.port}"
        status, body = _http(f"{base}/tasks", headers={"Authorization": f"Bearer {d.token}"})
        assert status == 200 and json.loads(body)["tasks"] == []
        status, body = _http(f"{base}/.well-known/agent.json", headers={"Authorization": f"Bearer {d.token}"})
        assert status == 200 and json.loads(body)["url"] == f"http://192.0.2.10:{d.port}"
    finally:
        d.stop()


def test_query_param_authorizes(home, monkeypatch):
    d = _authed_daemon(home, monkeypatch)
    try:
        base = f"http://127.0.0.1:{d.port}"
        status, body = _http(f"{base}/tasks?token={d.token}")
        assert status == 200 and json.loads(body)["tasks"] == []
    finally:
        d.stop()


def test_static_assets_stay_public(home, monkeypatch):
    d = _authed_daemon(home, monkeypatch)
    try:
        base = f"http://127.0.0.1:{d.port}"
        for path in ("/", "/index.html"):
            status, body = _http(f"{base}{path}")
            assert status == 200 and len(body) > 0, path
    finally:
        d.stop()


def test_wrong_bearer_is_rejected(home, monkeypatch):
    d = _authed_daemon(home, monkeypatch)
    try:
        status, body = _http(f"http://127.0.0.1:{d.port}/tasks", headers={"Authorization": "Bearer nope"})
        assert status == 401 and json.loads(body) == {"error": "unauthorized"}
    finally:
        d.stop()


# ------------------------------------------------------------------ daemon_main wiring

def test_run_daemon_forwards_bind_and_reports_token(home, monkeypatch):
    monkeypatch.setattr("maestro.discovery.pick_lan_ip", lambda: "192.0.2.10")
    from maestro.daemon_main import run_daemon

    info = run_daemon(state_dir=home, port=0, bind="0.0.0.0")
    try:
        assert info["bind"] == "0.0.0.0" and info["advertised_host"] == "192.0.2.10"
        assert info["token"] == info["daemon"].token
    finally:
        info["daemon"].stop()


def test_parse_args_defaults_and_bind():
    from maestro.daemon_main import _parse_args

    args = _parse_args([])
    assert args.bind == "127.0.0.1" and args.port == 0
    args = _parse_args(["--bind", "0.0.0.0", "--port", "8790"])
    assert args.bind == "0.0.0.0" and args.port == 8790


# ------------------------------------------------------------------ end-to-end: daemon -> daemon over token

def _fake_bin(dirpath, name: str, body: str) -> None:
    import stat

    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_daemon_to_daemon_delegation_with_token(tmp_path, monkeypatch):
    """Flagship cross-machine flow: B delegates to A (bound 0.0.0.0, token-auth);
    A re-targets the hop name to its own default agent and runs it."""
    import os
    import stat
    import subprocess

    from maestro.agents import AgentSpec
    from maestro.handoff import HandoffDoc

    monkeypatch.setattr("maestro.discovery.pick_lan_ip", lambda: "192.0.2.10")
    monkeypatch.delenv("MAESTRO_DAEMON_TOKEN", raising=False)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_bin(bin_dir, "codex", 'cat > /dev/null\necho remote-work-done\nexit 0')
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    ws_a = tmp_path / "ws-a"
    ws_a.mkdir()
    subprocess.run(["git", "-C", str(ws_a), "init", "-q"], check=True, env=env)

    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    a = MaestroDaemon(state_dir=home_a, start_http=True, port=0, bind="0.0.0.0", max_retries=0, backoff_s=0)
    b = MaestroDaemon(state_dir=home_b, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        assert a.token is not None and b.token is None
        # A runs codex locally; B knows A as the "remote-a" hop (URL + token).
        a.registry.save(AgentSpec(name="codex", kind="codex"))
        b.registry.save(AgentSpec(name="remote-a", kind="a2a_remote", command=f"http://127.0.0.1:{a.port}", token=a.token))

        doc = HandoffDoc(title="Cross-machine work", request="Implement it", target_agent="remote-a", explicit_target=True, verification="none", commit_policy="no-commit")
        started = b.delegate(doc, ws_a)
        assert started["queued"] is False
        final = b.wait(started["task_id"], timeout=60)
        assert final["status"]["state"] == "completed", final

        # the remote task ran on A under its own default agent (re-targeted hop name)
        a_tasks = a.list_tasks()
        assert len(a_tasks) == 1
        assert a_tasks[0]["metadata"]["target_agent"] == "codex"
        assert a_tasks[0]["status"]["state"] == "completed"
    finally:
        b.stop()
        a.stop()


def test_daemon_to_daemon_wrong_token_fails_fast(tmp_path, monkeypatch):
    from maestro.agents import AgentSpec
    from maestro.handoff import HandoffDoc

    monkeypatch.setattr("maestro.discovery.pick_lan_ip", lambda: "192.0.2.10")
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    a = MaestroDaemon(state_dir=home_a, start_http=True, port=0, bind="0.0.0.0", max_retries=0, backoff_s=0)
    b = MaestroDaemon(state_dir=home_b, start_http=True, port=0, max_retries=0, backoff_s=0)
    try:
        b.registry.save(AgentSpec(name="remote-a", kind="a2a_remote", command=f"http://127.0.0.1:{a.port}", token="wrong-token"))
        doc = HandoffDoc(title="t", request="r", target_agent="remote-a", explicit_target=True, verification="none", commit_policy="no-commit")
        started = b.delegate(doc, tmp_path)
        final = b.wait(started["task_id"], timeout=30)
        assert final["status"]["state"] == "failed"
        error_text = str(final.get("error") or "") + str(final["metadata"].get("error") or "")
        assert "401" in error_text
    finally:
        b.stop()
        a.stop()
