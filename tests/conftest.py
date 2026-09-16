from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Claim:
    subject: str
    predicate: str
    object: str
    episode_ids: list[str] | None = None


@dataclass
class Episode:
    episode_ids: list[str]


class FakeMemvara:
    stores: dict[str, list[Claim]] = {}
    next_episode = 0

    def __init__(self, db_path: str, **kwargs):
        self.db_path = str(Path(db_path).resolve())
        self._claims = self.stores.setdefault(self.db_path, [])

    @classmethod
    def reset(cls) -> None:
        cls.stores.clear()
        cls.next_episode = 0

    def close(self) -> None:
        return None

    def remember(self, subject, predicate, object, **kwargs):
        self.next_episode += 1
        self._claims.append(Claim(subject, predicate, str(object), [f"e{self.next_episode}"]))

    def history(self, subject, predicate):
        return [c for c in self._claims if c.subject == subject and c.predicate == predicate]

    def get_all(self):
        return list(self._claims)

    def add(self, content, role="system", ts=None):
        self.next_episode += 1
        return Episode([f"e{self.next_episode}"])


class FakeFastMCP:
    def __init__(self, name):
        self.name = name
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco

    def run(self):
        return None


FakeNullLLM = type("NullLLM", (), {})

memvara = types.ModuleType("memvara")
memvara.Memvara = FakeMemvara
memvara.NullLLM = FakeNullLLM
sys.modules["memvara"] = memvara

mcp_pkg = types.ModuleType("mcp")
mcp_server_pkg = types.ModuleType("mcp.server")
fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
fastmcp_mod.FastMCP = FakeFastMCP
mcp_server_pkg.fastmcp = fastmcp_mod
mcp_pkg.server = mcp_server_pkg
sys.modules["mcp"] = mcp_pkg
sys.modules["mcp.server"] = mcp_server_pkg
sys.modules["mcp.server.fastmcp"] = fastmcp_mod
