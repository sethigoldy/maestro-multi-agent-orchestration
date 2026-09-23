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
    """Daemons start UDP presence by default; keep test traffic on loopback."""
    monkeypatch.setenv("MAESTRO_DISCOVERY_TTL", "0")
    monkeypatch.setenv("MAESTRO_DISCOVERY_IF", "127.0.0.1")


class FakeFastMCP:
    def __init__(self, name): self.name=name; self.tools={}
    def tool(self):
        def deco(fn): self.tools[fn.__name__]=fn; return fn
        return deco
    def run(self): return None
mcp_pkg=types.ModuleType('mcp'); mcp_server_pkg=types.ModuleType('mcp.server'); fastmcp_mod=types.ModuleType('mcp.server.fastmcp')
fastmcp_mod.FastMCP=FakeFastMCP; mcp_server_pkg.fastmcp=fastmcp_mod; mcp_pkg.server=mcp_server_pkg
sys.modules['mcp']=mcp_pkg; sys.modules['mcp.server']=mcp_server_pkg; sys.modules['mcp.server.fastmcp']=fastmcp_mod
