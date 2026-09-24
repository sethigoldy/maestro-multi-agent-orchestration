from __future__ import annotations
import sys, types

import pytest


@pytest.fixture(autouse=True)
def _no_detached_daemon_outlives_a_test(monkeypatch):
    """No test may leave a detached background daemon running after pytest ends.

    The MCP server's main() switches daemon.BACKGROUND_OWNER on, and then
    get_daemon() starts a detached daemon that outlives the process. It is
    kept off here unless a test sets it itself (monkeypatch restores it).
    Every daemon a test starts through daemonctl.start (directly, through the
    CLI, or as a background owner) is stopped at the end of that test, unless
    it is this test process itself.
    """
    import os

    import maestro.daemon as daemon_module
    from maestro import daemonctl

    monkeypatch.setattr(daemon_module, "BACKGROUND_OWNER", False)
    started_dirs: list = []
    real_start = daemonctl.start

    def tracking_start(state_dir=None, **kwargs):
        started_dirs.append(state_dir or daemonctl._state_dir())
        return real_start(state_dir, **kwargs)

    monkeypatch.setattr(daemonctl, "start", tracking_start)
    yield
    for state_dir in started_dirs:
        try:
            info = daemonctl.status(state_dir)
            if info.pid is not None and info.pid != os.getpid() and not info.stale_marker:
                daemonctl.stop(state_dir, grace_s=5)
        except Exception:
            pass  # a test that removed its directory: nothing left to stop there


@pytest.fixture(autouse=True)
def _pin_discovery_to_loopback(monkeypatch):
    """Daemons start UDP presence by default; keep test traffic on loopback.

    Under pytest-xdist every worker gets its own discovery port, so daemons
    started by tests running in parallel never hear each other's announcements.
    """
    import os

    monkeypatch.setenv("MAESTRO_DISCOVERY_TTL", "0")
    # Daemons started by tests listen on any free port, not the fixed default
    # 9785, so tests running in parallel (and a daemon the developer is
    # running) never collide. tests/test_daemon_port.py unsets it.
    monkeypatch.setenv("MAESTRO_DAEMON_PORT", "0")
    monkeypatch.setenv("MAESTRO_DISCOVERY_IF", "127.0.0.1")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")  # "gw0", "gw1", … under xdist
    if worker.startswith("gw") and worker[2:].isdigit():
        monkeypatch.setenv("MAESTRO_DISCOVERY_PORT", str(19786 + int(worker[2:])))


@pytest.fixture(autouse=True)
def _poll_quickly(monkeypatch):
    """Poll for cancels and state changes every 50 ms instead of every 500 ms.

    These intervals only decide how soon a waiting loop notices a change, so a
    shorter one changes no outcome; it only stops each test that cancels an
    agent or waits on another daemon's task from sleeping up to half a second.
    Grace periods, which tests do depend on, are left as they are.
    """
    import maestro.adapters.a2a_remote as a2a_remote
    import maestro.adapters.base as base
    import maestro.daemon as daemon_module

    monkeypatch.setattr(base, "_CANCEL_POLL_S", 0.05)
    monkeypatch.setattr(a2a_remote, "_CANCEL_POLL_S", 0.05)
    monkeypatch.setattr(daemon_module, "DURABLE_POLL_S", 0.05)


class FakeFastMCP:
    def __init__(self, name): self.name=name; self.tools={}
    def tool(self):
        def deco(fn): self.tools[fn.__name__]=fn; return fn
        return deco
    def run(self): return None
mcp_pkg=types.ModuleType('mcp'); mcp_server_pkg=types.ModuleType('mcp.server'); fastmcp_mod=types.ModuleType('mcp.server.fastmcp')
fastmcp_mod.FastMCP=FakeFastMCP; mcp_server_pkg.fastmcp=fastmcp_mod; mcp_pkg.server=mcp_server_pkg
sys.modules['mcp']=mcp_pkg; sys.modules['mcp.server']=mcp_server_pkg; sys.modules['mcp.server.fastmcp']=fastmcp_mod
