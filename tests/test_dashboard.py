"""M6: dashboard routes, global SSE stream, and the new CLI surface."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from maestro.daemon import MaestroDaemon
from maestro.events import TaskEvent
from maestro.handoff import HandoffDoc


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


def _doc(**kw) -> HandoffDoc:
    base = dict(title="Do the thing", request="Implement it", verification="none", commit_policy="no-commit")
    # Without a work mode, pin an explicit target: since 0.10 a handoff that
    # names no agent (and has no [defaults]) parks with a routing question.
    if not kw.get("mode"):
        base["target_agent"] = "codex"
        base["explicit_target"] = True
    base.update(kw)
    return HandoffDoc(**base)


@pytest.fixture
def live_daemon(tmp_path, monkeypatch):
    """Daemon with a real HTTP server on an ephemeral port."""
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=True, port=0, max_retries=0, backoff_s=0)
    yield d
    d.stop()


def _get(url: str) -> tuple[int, dict | bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read()
            return resp.status, body, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def test_dashboard_html_served(live_daemon):
    status, body, ctype = _get(f"http://127.0.0.1:{live_daemon.port}/")
    assert status == 200 and "text/html" in ctype
    html = body.decode("utf-8")
    assert '<div id="root">' in html
    assert "/console.js" in html


def test_console_js_served_reactive(live_daemon):
    status, body, ctype = _get(f"http://127.0.0.1:{live_daemon.port}/console.js")
    assert status == 200 and "javascript" in ctype
    js = body.decode("utf-8")
    assert "EventSource" in js  # reactive: events stream, no polling loop
    assert "setInterval" not in js
    assert '"/tasks"' in js or "/tasks" in js  # initial list comes from GET /tasks


def test_console_asset_module():
    from maestro.dashboard import console_asset, console_manifest

    asset = console_asset("/")
    assert asset is not None and "text/html" in asset[0]
    js = console_asset("/console.js")
    assert js is not None and "javascript" in js[0]
    assert console_asset("/nope.js") is None
    manifest = console_manifest()
    assert "/" in manifest and "/console.js" in manifest


def test_tasks_endpoint_lists_live_and_durable(live_daemon, tmp_path):
    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\necho working\nexit 0')
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bp}{os.pathsep}{old_path}"
    try:
        ws = _git_repo(tmp_path)
        started = live_daemon.delegate(_doc(), ws)
        live_daemon.wait(started["task_id"], timeout=60)
    finally:
        os.environ["PATH"] = old_path
    status, body, _ = _get(f"http://127.0.0.1:{live_daemon.port}/tasks")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    ids = [t.get("id") for t in payload["tasks"]]
    assert started["task_id"] in ids


def test_tasks_metadata_carries_usage_and_attempts(live_daemon, tmp_path):
    bp = tmp_path / "bin"
    bp.mkdir()
    body = "cat > /dev/null\n" + 'echo \'{"total_cost_usd": 0.77}\'\n' + "exit 1"
    _fake_bin(bp, "codex", body)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bp}{os.pathsep}{old_path}"
    try:
        ws = _git_repo(tmp_path)
        started = live_daemon.delegate(_doc(), ws)
        final = live_daemon.wait(started["task_id"], timeout=60)
        assert final["status"]["state"] == "failed"  # the fake exits non-zero
    finally:
        os.environ["PATH"] = old_path
    status, body, _ = _get(f"http://127.0.0.1:{live_daemon.port}/tasks")
    payload = json.loads(body.decode("utf-8"))
    record = next(t for t in payload["tasks"] if t["id"] == started["task_id"])
    meta = record["metadata"]
    assert meta["usage"]["cost_usd"] == 0.77
    assert len(meta["attempts"]) == 1 and meta["attempts"][0]["error"]
    assert meta["error"]


def test_tasks_metadata_durable_fallback_parses_runtime(live_daemon, tmp_path):
    # A task known only from durable state (no live record): usage/attempts come
    # from the task_runtime claim snapshot.
    tid = "task-20991231-235959-durable1"
    m = live_daemon.maestro
    with m._task_lock():
        m._register_task(tid, "durable only", 900)
    m._write_claim(tid, "task_status", "COMPLETE")
    m._write_claim(tid, "task_title", "durable only")
    m._write_claim(
        tid, "task_runtime",
        json.dumps({"usage": {"cost_usd": 1.25}, "attempts": [{"agent": "codex", "error": None}], "error": None}),
    )
    status, body, _ = _get(f"http://127.0.0.1:{live_daemon.port}/tasks")
    payload = json.loads(body.decode("utf-8"))
    record = next(t for t in payload["tasks"] if t["id"] == tid)
    assert record["metadata"]["usage"]["cost_usd"] == 1.25
    assert record["metadata"]["attempts"][0]["agent"] == "codex"


def test_tasks_metadata_durable_runtime_not_dict(live_daemon, tmp_path):
    tid = "task-20991231-235959-durable2"
    m = live_daemon.maestro
    with m._task_lock():
        m._register_task(tid, "bad runtime", 901)
    m._write_claim(tid, "task_status", "COMPLETE")
    m._write_claim(tid, "task_runtime", "[1, 2]")  # JSON but not a dict
    status, body, _ = _get(f"http://127.0.0.1:{live_daemon.port}/tasks")
    payload = json.loads(body.decode("utf-8"))
    record = next(t for t in payload["tasks"] if t["id"] == tid)
    assert record["metadata"]["usage"] is None and record["metadata"]["attempts"] == []


def test_tasks_metadata_durable_runtime_malformed(live_daemon, tmp_path):
    tid = "task-20991231-235959-durable3"
    m = live_daemon.maestro
    with m._task_lock():
        m._register_task(tid, "malformed runtime", 902)
    m._write_claim(tid, "task_status", "COMPLETE")
    m._write_claim(tid, "task_runtime", "{not json")
    status, body, _ = _get(f"http://127.0.0.1:{live_daemon.port}/tasks")
    payload = json.loads(body.decode("utf-8"))
    record = next(t for t in payload["tasks"] if t["id"] == tid)
    assert record["metadata"]["usage"] is None


def test_global_events_stream_carries_every_task(live_daemon):
    conn = HTTPConnection("127.0.0.1", live_daemon.port, timeout=15)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    assert resp.status == 200 and "text/event-stream" in resp.getheader("Content-Type", "")

    def read_until(predicate: str, seconds: float = 10.0) -> str:
        buf = ""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk.decode("utf-8")
            if predicate in buf:
                return buf
        return buf

    live_daemon.bus.publish(TaskEvent(task_id="task-a", type="state", data={"state": "working"}))
    live_daemon.bus.publish(TaskEvent(task_id="task-b", type="usage", data={"cost_usd": 0.5}))
    buf = read_until("task-b")
    assert "task-a" in buf and "task-b" in buf  # global stream is unfiltered
    conn.close()


def test_unknown_path_still_404(live_daemon):
    status, body, _ = _get(f"http://127.0.0.1:{live_daemon.port}/nope")
    assert status == 404 and json.loads(body.decode("utf-8"))["error"] == "not found"


# ---------------------------------------------------------------- CLI surface

def test_cli_delegate_end_to_end(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\necho line-one\necho line-two\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MAESTRO_DAEMON_URL", f"http://127.0.0.1:{live_daemon.port}")

    ws = _git_repo(tmp_path)
    captured: dict[str, object] = {}

    def _capture(stdout, *a, **kw):
        captured["out"] = (captured.get("out") or "") + stdout

    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: _capture(s), "flush": lambda self: None})())
    code = clic.main(["delegate", "--title", "T", "--request", "R", "--target", "codex", "--workspace", str(ws)])
    out = str(captured.get("out") or "")
    assert code == 0
    assert "line-one" in out and "line-two" in out
    assert "[state] completed" in out


def test_cli_delegate_no_wait(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'sleep 0.5\ncat > /dev/null\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MAESTRO_DAEMON_URL", f"http://127.0.0.1:{live_daemon.port}")

    ws = _git_repo(tmp_path)
    code = clic.main(["delegate", "--title", "T", "--request", "R", "--target", "codex", "--workspace", str(ws), "--no-wait"])
    assert code == 0


def test_cli_delegate_requires_full_flags(live_daemon, monkeypatch):
    from maestro import cli as clic

    monkeypatch.setenv("MAESTRO_DAEMON_URL", f"http://127.0.0.1:{live_daemon.port}")
    with pytest.raises(ValueError):
        clic._cmd_delegate(type("A", (), {"file": None, "title": "T", "request": None, "target": None, "fallback": [], "design_file": None, "workspace": None, "project": None})())


def test_cli_task_tail_catches_up(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\necho buffered-line\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MAESTRO_DAEMON_URL", f"http://127.0.0.1:{live_daemon.port}")

    ws = _git_repo(tmp_path)
    started = live_daemon.delegate(_doc(), ws)
    live_daemon.wait(started["task_id"], timeout=60)  # task already terminal: tail replays the ring buffer

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    code = clic.main(["task", "tail", started["task_id"]])
    out = str(captured.get("out") or "")
    assert code == 0 and "buffered-line" in out and "[state] completed" in out


def test_cli_task_audit(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\necho ok\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ['PATH']}")

    ws = _git_repo(tmp_path)
    started = live_daemon.delegate(_doc(), ws)
    live_daemon.wait(started["task_id"], timeout=60)

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    code = clic.main(["task", "audit", started["task_id"]])
    payload = json.loads(str(captured.get("out") or "{}"))
    assert code == 0
    assert payload["task_id"] == started["task_id"]
    assert payload["state"] in {"completed", "COMPLETE"}
    assert payload["attempts"] and payload["attempts"][0]["agent"] == "codex"
    assert any(r.get("ok") for r in payload["results"])
    # No context entries on this task: the key is present but empty.
    assert payload["context"] == []


def test_cli_task_audit_shows_composed_context(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\necho ok\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ['PATH']}")

    # Standing entry injected via the config seam (mirrors [context.<label>] tables).
    from maestro.context import ContextEntry
    live_daemon.maestro.config["context"] = {"style": ContextEntry(label="style", kind="text", text="Standing rule.", source="user config")}

    ws = _git_repo(tmp_path)
    started = live_daemon.delegate(_doc(context_entries=[{"label": "spec", "kind": "text", "text": "Per-task note."}]), ws)
    live_daemon.wait(started["task_id"], timeout=60)

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    code = clic.main(["task", "audit", started["task_id"]])
    payload = json.loads(str(captured.get("out") or "{}"))
    assert code == 0
    labels = [(e["label"], e["source"]) for e in payload["context"]]
    # Composed at delegate time: standing config entry first, handoff entry after.
    assert labels == [("style", "user config"), ("spec", "handoff")]


def test_cli_gc_dry_run_and_delete(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    state_dir = live_daemon.state_dir
    old_task = "task-20000101-000000-deadbeef"
    m = live_daemon.maestro
    with m._task_lock():
        m._register_task(old_task, "ancient task", 900)
    m._write_claim(old_task, "task_status", "COMPLETE")
    task_dir = state_dir / "tasks" / old_task
    task_dir.mkdir(parents=True)
    (task_dir / "result-codex-t1-0.json").write_text('{"ok": true}', encoding="utf-8")
    ancient = time.time() - 200 * 86400
    os.utime(task_dir, (ancient, ancient))

    captured: dict[str, object] = {}
    fake_out = type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})()
    monkeypatch.setattr(clic.sys, "stdout", fake_out)

    code = clic.main(["gc", "--days", "90", "--dry-run"])
    dry = json.loads(str(captured.get("out") or "{}"))
    assert code == 0
    assert any(item["task_id"] == old_task and item["dry_run"] for item in dry["removed"])
    assert task_dir.is_dir()  # dry run deletes nothing

    captured.clear()
    code = clic.main(["gc", "--days", "90"])
    real = json.loads(str(captured.get("out") or "{}"))
    assert code == 0
    assert any(item["task_id"] == old_task and not task_dir.is_dir() for item in real["removed"])
    assert all(str(r.get("task_id")) != old_task for r in m._registry_records())
    assert "task_status" not in m._claims(old_task)


def test_cli_gc_keeps_active_and_recent(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    state_dir = live_daemon.state_dir
    m = live_daemon.maestro
    recent = "task-20990101-000000-cafebabe"
    with m._task_lock():
        m._register_task(recent, "fresh task", 901)
    m._write_claim(recent, "task_status", "COMPLETE")
    (state_dir / "tasks" / recent).mkdir(parents=True)

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    code = clic.main(["gc", "--days", "90"])
    payload = json.loads(str(captured.get("out") or "{}"))
    assert code == 0 and payload["removed"] == [] and payload["kept"] >= 1


def test_daemon_url_rejects_stale_marker(tmp_path, monkeypatch):
    from maestro import cli as clic

    home = tmp_path / "home"
    home.mkdir()
    (home / "daemon.json").write_text(json.dumps({"pid": 999999999, "port": 12345}), encoding="utf-8")
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    monkeypatch.delenv("MAESTRO_DAEMON_URL", raising=False)
    with pytest.raises(ValueError, match="no daemon reachable"):
        clic._daemon_url()


def test_daemon_url_env_override(tmp_path, monkeypatch):
    from maestro import cli as clic

    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://127.0.0.1:9/")
    assert clic._daemon_url() == "http://127.0.0.1:9"


def test_daemon_endpoint_env_token_pair(tmp_path, monkeypatch):
    from maestro import cli as clic

    monkeypatch.setenv("MAESTRO_DAEMON_URL", "http://10.0.0.5:8790/")
    monkeypatch.setenv("MAESTRO_DAEMON_TOKEN", "env-token")
    assert clic._daemon_endpoint() == ("http://10.0.0.5:8790", "env-token")
    monkeypatch.delenv("MAESTRO_DAEMON_TOKEN")
    assert clic._daemon_endpoint() == ("http://10.0.0.5:8790", None)


def test_daemon_endpoint_marker_host_and_token(tmp_path, monkeypatch):
    from maestro import cli as clic
    from maestro import daemonctl

    home = tmp_path / "home"
    home.mkdir()
    # This process stands in for the daemon: it holds the owner lock that the
    # marker points at, and its endpoint is reported as answering.
    lock_fd = daemonctl.acquire_owner_lock(home)
    monkeypatch.setattr(daemonctl, "probe", lambda url, timeout=3.0: True)
    try:
        (home / "daemon.json").write_text(
            json.dumps({"pid": os.getpid(), "port": 8790, "host": "127.0.0.2", "token": "mk-token", "owner_lock": True}), encoding="utf-8"
        )
        monkeypatch.setenv("MAESTRO_HOME", str(home))
        monkeypatch.delenv("MAESTRO_DAEMON_URL", raising=False)
        assert clic._daemon_endpoint() == ("http://127.0.0.2:8790", "mk-token")
        # marker without host/token (loopback daemon) still resolves
        (home / "daemon.json").write_text(json.dumps({"pid": os.getpid(), "port": 8791, "owner_lock": True}), encoding="utf-8")
        assert clic._daemon_endpoint() == ("http://127.0.0.1:8791", None)
    finally:
        daemonctl.release_owner_lock(lock_fd)


def test_full_swap_demo_script_is_valid():
    script = Path(__file__).resolve().parent.parent / "examples" / "full-swap.sh"
    assert script.is_file()
    check = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    text = script.read_text(encoding="utf-8")
    assert "claude -p" in text and "maestro.mcp_server" in text  # leg A: host agent via MCP
    assert "task audit" in text


# ---------------------------------------------------------------- coverage: remaining branches

def test_daemon_url_reads_live_marker(live_daemon):
    from maestro import cli as clic

    assert clic._daemon_url() == f"http://127.0.0.1:{live_daemon.port}"


def test_post_jsonrpc_error_maps_to_value_error(live_daemon, monkeypatch):
    from maestro import cli as clic

    monkeypatch.setenv("MAESTRO_DAEMON_URL", f"http://127.0.0.1:{live_daemon.port}")
    with pytest.raises(ValueError, match="not found|unknown"):
        clic._post_jsonrpc(f"http://127.0.0.1:{live_daemon.port}", "bogus/method", {})


def _sse_fixture_server(tmp_path):
    """A throwaway SSE endpoint exercising every line kind _sse_events must parse."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = (
        ": keepalive\n\n"
        "retry: 5000\n"
        "event: output\ndata: {\"task_id\": \"t1\", \"type\": \"output\", \"data\": {\"line\": \"hi\"}}\n\n"
        "event: state\ndata: {\"task_id\": \"t1\", \"type\": \"state\", \"data\": {\"state\": \"completed\"}}\n\n"
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    import threading as _t

    thread = _t.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_sse_events_parses_all_line_kinds(tmp_path):
    from maestro import cli as clic

    server, url = _sse_fixture_server(tmp_path)
    try:
        events = list(clic._sse_events(url, "/events"))
    finally:
        server.shutdown()
        server.server_close()
    assert [(name, data["data"]) for name, data in events] == [
        ("output", {"line": "hi"}),
        ("state", {"state": "completed"}),
    ]


def test_stream_task_keyboard_interrupt(tmp_path, monkeypatch):
    from maestro import cli as clic

    def boom(url, path, token=None):
        yield ("output", {"data": {"line": "x"}})
        raise KeyboardInterrupt

    monkeypatch.setattr(clic, "_sse_events", boom)
    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    assert clic._stream_task("http://x", "task-1") == 130
    assert "[tail stopped]" in str(captured.get("out") or "")


def test_stream_task_closes_without_state(tmp_path):
    from maestro import cli as clic

    server, url = _sse_fixture_server(tmp_path)
    try:
        # per-task stream that ends without any state event -> nothing to judge
        assert clic._stream_task(url, None) == 0
    finally:
        server.shutdown()
        server.server_close()


def test_cli_delegate_queued_behind_active(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'sleep 0.4\ncat > /dev/null\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MAESTRO_DAEMON_URL", f"http://127.0.0.1:{live_daemon.port}")

    ws = _git_repo(tmp_path)
    started = live_daemon.delegate(_doc(), ws)  # holds the workspace slot
    try:
        captured: dict[str, object] = {}
        monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
        # A no-commit task works in place, so it waits for the workspace.
        # (A task with a branch would run in a worktree of its own instead.)
        handoff = tmp_path / "h.toml"
        handoff.write_text('[handoff]\ntitle = "T2"\nrequest = "R2"\n[expectations]\ncommit_policy = "no-commit"\n', encoding="utf-8")
        code = clic.main(["delegate", "--file", str(handoff), "--target", "codex", "--workspace", str(ws)])
        payload = json.loads(str(captured.get("out") or "{}"))
        assert code == 0 and payload["queued"] is True
    finally:
        live_daemon.wait(started["task_id"], timeout=60)


def test_cli_delegate_design_file(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MAESTRO_DAEMON_URL", f"http://127.0.0.1:{live_daemon.port}")

    ws = _git_repo(tmp_path)
    design = tmp_path / "design.txt"
    design.write_text("the design", encoding="utf-8")
    code = clic.main(["delegate", "--title", "T", "--request", "R", "--target", "codex", "--workspace", str(ws), "--design-file", str(design)])
    assert code == 0

    with pytest.raises(ValueError, match="design file"):
        args = type("A", (), {"file": None, "title": "T", "request": "R", "target": "codex", "fallback": [], "design_file": str(tmp_path / "missing.txt"), "workspace": str(ws), "project": None})()
        clic._cmd_delegate(args)


def test_cli_audit_malformed_runtime(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ['PATH']}")

    ws = _git_repo(tmp_path)
    started = live_daemon.delegate(_doc(), ws)
    live_daemon.wait(started["task_id"], timeout=60)
    live_daemon.maestro._write_claim(started["task_id"], "task_runtime", "{not json")

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    code = clic.main(["task", "audit", started["task_id"]])
    payload = json.loads(str(captured.get("out") or "{}"))
    assert code == 0 and payload["state"] is not None  # fell back to the phase claim


def test_cli_gc_uses_created_at_and_skips_bad_records(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    state_dir = live_daemon.state_dir
    m = live_daemon.maestro
    via_date = "task-20000101-000000-aaaa1111"
    bad_date = "task-20000101-000000-bbbb2222"
    active_old = "task-20000101-000000-cccc3333"
    with m._task_lock():
        m._register_task(via_date, "old by date", 910)
        m._register_task(bad_date, "bad created_at", 911)
        m._register_task(active_old, "still active", 912)
    # rewrite the via_date record with an ancient created_at (no task dir -> date fallback)
    items = {str(x["task_id"]): x for x in m._load_index()}
    items[via_date]["created_at"] = "2000-01-01T00:00:00+00:00"
    items[bad_date]["created_at"] = "not-a-date"
    m._save_index(list(items.values()))
    m._write_claim(via_date, "task_status", "COMPLETE")
    m._write_claim(bad_date, "task_status", "COMPLETE")
    m._write_claim(active_old, "task_status", "IMPLEMENTING")

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    code = clic.main(["gc", "--days", "90"])
    payload = json.loads(str(captured.get("out") or "{}"))
    removed_ids = [item["task_id"] for item in payload["removed"]]
    assert code == 0 and via_date in removed_ids          # aged out via created_at, no task dir
    assert bad_date not in removed_ids                     # unparseable date -> kept
    assert active_old not in removed_ids                   # non-terminal -> kept
    assert m.mem.forget("maestro:no-such-subject") == 0    # forget() with nothing to drop


def test_index_html_route_serves_dashboard(live_daemon):
    status, body, ctype = _get(f"http://127.0.0.1:{live_daemon.port}/index.html")
    assert status == 200 and "text/html" in ctype
    assert "Maestro" in body.decode("utf-8")


# ---------------------------------------------------------------- final branch gaps

def test_post_jsonrpc_non_json_error_body(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            # Read the request body before replying. Closing a socket that
            # still holds unread data makes the kernel send a reset, and the
            # client could then see "connection reset" instead of the reply.
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(400)
            self.send_header("Content-Length", "18")
            self.end_headers()
            self.wfile.write(b"plain text failure")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    import threading as _t

    thread = _t.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from maestro import cli as clic

        with pytest.raises(ValueError, match="HTTP 400"):
            clic._post_jsonrpc(f"http://127.0.0.1:{server.server_address[1]}", "message/send", {})
    finally:
        server.shutdown()
        server.server_close()


def test_sse_events_flushes_pending_data_at_eof(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # Stream ends with a data line but NO trailing blank line.
    body = b"event: output\ndata: {\"task_id\": \"t1\", \"type\": \"output\", \"data\": {\"line\": \"tail\"}}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    import threading as _t

    thread = _t.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from maestro import cli as clic

        events = list(clic._sse_events(f"http://127.0.0.1:{server.server_address[1]}", "/events"))
        assert [(name, data["data"]) for name, data in events] == [("output", {"line": "tail"})]
    finally:
        server.shutdown()
        server.server_close()


def test_stream_task_per_task_without_state_returns_zero(tmp_path):
    from maestro import cli as clic

    server, url = _sse_fixture_server(tmp_path)
    try:
        # per-task stream that ends without any state event -> nothing to judge
        assert clic._stream_task(url, "task-1") == 0
    finally:
        server.shutdown()
        server.server_close()


def test_cli_delegate_via_file_and_project(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    bp = tmp_path / "bin"
    bp.mkdir()
    _fake_bin(bp, "codex", 'cat > /dev/null\nexit 0')
    monkeypatch.setenv("PATH", f"{bp}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MAESTRO_DAEMON_URL", f"http://127.0.0.1:{live_daemon.port}")

    ws = _git_repo(tmp_path)
    handoff_file = tmp_path / "h.toml"
    handoff_file.write_text(
        '[handoff]\ntitle = "Via file"\nrequest = "Do it"\n\n[routing]\ntarget_agent = "codex"\n', encoding="utf-8"
    )
    code = clic.main(["delegate", "--file", str(handoff_file), "--project", str(ws)])
    assert code == 0


def test_cli_audit_without_runtime_claim(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    state_dir = live_daemon.state_dir
    m = live_daemon.maestro
    bare = "task-20991231-235959-deadbeef"
    with m._task_lock():
        m._register_task(bare, "bare task", 950)
    m._write_claim(bare, "task_status", "COMPLETE")
    (state_dir / "tasks" / bare).mkdir(parents=True)
    (state_dir / "tasks" / bare / "result-codex-t1-0.json").write_text("not json", encoding="utf-8")

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    code = clic.main(["task", "audit", bare])
    payload = json.loads(str(captured.get("out") or "{}"))
    assert code == 0
    assert payload["state"] == "COMPLETE"   # no task_runtime claim -> phase fallback
    assert payload["results"] == []          # malformed result file skipped

    m._write_claim(bare, "task_runtime", "[1, 2]")  # JSON but not a dict
    captured.clear()
    code = clic.main(["task", "audit", bare])
    payload = json.loads(str(captured.get("out") or "{}"))
    assert code == 0 and payload["state"] == "COMPLETE"


def test_cli_gc_naive_created_at_and_record_without_id(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    m = live_daemon.maestro
    naive = "task-20000101-000000-naive001"
    with m._task_lock():
        m._register_task(naive, "naive date", 960)
    items = {str(x["task_id"]): x for x in m._load_index()}
    items[naive]["created_at"] = "2000-01-01T00:00:00"  # naive -> tz assumed UTC
    items["task-20000101-000000-noid0000"] = {"number": 961, "title": "no id"}
    m._save_index(list(items.values()))
    m._write_claim(naive, "task_status", "COMPLETE")

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    code = clic.main(["gc", "--days", "90"])
    payload = json.loads(str(captured.get("out") or "{}"))
    removed_ids = [item["task_id"] for item in payload["removed"]]
    assert code == 0 and naive in removed_ids


def test_global_sse_route_returns_after_client_gone(live_daemon):
    from http.client import HTTPConnection

    live_daemon.sse_heartbeat_s = 0.2  # fast keepalive so the closed-socket write fails quickly
    conn = HTTPConnection("127.0.0.1", live_daemon.port, timeout=10)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    assert resp.status == 200
    conn.close()  # server-side handler notices on the next keepalive and returns


def test_stream_task_prints_usage_events(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = (
        b"event: output\ndata: {\"task_id\": \"t1\", \"type\": \"output\", \"data\": {\"line\": \"x\"}}\n\n"
        b"event: usage\ndata: {\"task_id\": \"t1\", \"type\": \"usage\", \"data\": {\"cost_usd\": 0.25}}\n\n"
        b"event: state\ndata: {\"task_id\": \"t1\", \"type\": \"state\", \"data\": {\"state\": \"completed\"}}\n\n"
    )

    class HandlerU(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), HandlerU)
    import threading as _t

    thread = _t.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from maestro import cli as clic

        captured: dict[str, object] = {}
        real_stdout = __import__("sys").stdout
        __import__("sys").stdout = type("S", (), {"write": lambda self, t: captured.__setitem__("out", (captured.get("out") or "") + t), "flush": lambda self: None})()
        try:
            code = clic._stream_task(f"http://127.0.0.1:{server.server_address[1]}", "task-1")
        finally:
            __import__("sys").stdout = real_stdout
        out = str(captured.get("out") or "")
        assert code == 0 and "[usage]" in out and '"cost_usd": 0.25' in out and "[state] completed" in out
    finally:
        server.shutdown()
        server.server_close()


def test_stream_task_per_task_closes_without_state(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # Only an output event; the stream ends with no state event at all.
    body = b"event: output\ndata: {\"task_id\": \"t1\", \"type\": \"output\", \"data\": {\"line\": \"x\"}}\n\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    import threading as _t

    thread = _t.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from maestro import cli as clic

        assert clic._stream_task(f"http://127.0.0.1:{server.server_address[1]}", "task-1") == 0
    finally:
        server.shutdown()
        server.server_close()


def test_cli_audit_no_task_dir_and_empty_dir(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    m = live_daemon.maestro
    no_dir = "task-20991231-235959-nodir001"
    empty_dir = "task-20991231-235959-empty001"
    with m._task_lock():
        m._register_task(no_dir, "no dir", 970)
        m._register_task(empty_dir, "empty dir", 971)
    (live_daemon.state_dir / "tasks" / empty_dir).mkdir(parents=True)
    # JSON but not a dict: must be skipped, not crash the audit.
    (live_daemon.state_dir / "tasks" / empty_dir / "result-codex-t1-0.json").write_text("[1, 2]", encoding="utf-8")

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    for tid in (no_dir, empty_dir):
        captured.clear()
        code = clic.main(["task", "audit", tid])
        payload = json.loads(str(captured.get("out") or "{}"))
        assert code == 0 and payload["results"] == []


def test_cli_gc_record_without_created_at(live_daemon, tmp_path, monkeypatch):
    from maestro import cli as clic

    m = live_daemon.maestro
    no_date = "task-20991231-235959-nodate01"
    with m._task_lock():
        m._register_task(no_date, "no date", 980)
    items = {str(x["task_id"]): x for x in m._load_index()}
    del items[no_date]["created_at"]
    m._save_index(list(items.values()))
    m._write_claim(no_date, "task_status", "COMPLETE")

    captured: dict[str, object] = {}
    monkeypatch.setattr(clic.sys, "stdout", type("S", (), {"write": lambda self, s: captured.__setitem__("out", (captured.get("out") or "") + s), "flush": lambda self: None})())
    code = clic.main(["gc", "--days", "90"])
    payload = json.loads(str(captured.get("out") or "{}"))
    assert code == 0 and all(item["task_id"] != no_date for item in payload["removed"])  # undatable -> kept


def test_console_asset_missing_dist(monkeypatch):
    import maestro.dashboard as dash
    from pathlib import Path

    monkeypatch.setattr(dash, "WEB_DIST", Path("/nonexistent/maestro-web-dist"))
    assert dash.console_asset("/") is None
    manifest = dash.console_manifest()
    assert all("(0 bytes)" in v for v in manifest.values())


def test_console_route_404_when_dist_missing(live_daemon, monkeypatch):
    import maestro.dashboard as dash
    from pathlib import Path

    monkeypatch.setattr(dash, "WEB_DIST", Path("/nonexistent/maestro-web-dist"))
    status, body, ctype = _get(f"http://127.0.0.1:{live_daemon.port}/console.js")
    assert status == 404 and "application/json" in ctype
