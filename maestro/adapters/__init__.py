"""Adapter factory: registry spec → adapter instance."""

from __future__ import annotations

from ..agents import AgentSpec
from .base import AdapterNotAvailable, BaseAdapter
from .claude_code import ClaudeCodeAdapter
from .cline import ClineAdapter
from .codex import CodexAdapter
from .generic import GenericAdapter
from .hermes import HermesAdapter
from .pi import PiAdapter

__all__ = [
    "AdapterNotAvailable",
    "BaseAdapter",
    "ClaudeCodeAdapter",
    "ClineAdapter",
    "CodexAdapter",
    "GenericAdapter",
    "HermesAdapter",
    "PiAdapter",
    "make_adapter",
]

#: Kinds implemented in this release (M2+M4). The rest land in M5.
IMPLEMENTED_KINDS: frozenset[str] = frozenset({"codex", "claude_code", "generic", "pi", "cline", "hermes"})


def make_adapter(spec: AgentSpec) -> BaseAdapter:
    if spec.kind == "generic":
        return GenericAdapter(spec)
    if spec.kind not in IMPLEMENTED_KINDS:
        raise AdapterNotAvailable(
            f"Adapter kind {spec.kind!r} is not implemented in this release yet (planned for M5); "
            "register it as a generic agent or upgrade Maestro"
        )
    adapters = {
        "codex": CodexAdapter,
        "claude_code": ClaudeCodeAdapter,
        "pi": PiAdapter,
        "cline": ClineAdapter,
        "hermes": HermesAdapter,
    }
    return adapters[spec.kind](spec)
