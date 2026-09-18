from __future__ import annotations
import sys, types

import pytest


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
