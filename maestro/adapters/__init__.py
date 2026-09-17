"""Adapter factory: registry spec → adapter instance."""

from __future__ import annotations

from ..agents import AgentSpec
from .base import AdapterNotAvailable, BaseAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .generic import GenericAdapter

__all__ = [
    "AdapterNotAvailable",
    "BaseAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "GenericAdapter",
    "make_adapter",
]

#: Kinds implemented in this release (M2). The rest land in M4/M5.
IMPLEMENTED_KINDS: frozenset[str] = frozenset({"codex", "claude_code", "generic"})


def make_adapter(spec: AgentSpec) -> BaseAdapter:
    if spec.kind == "generic":
        return GenericAdapter(spec)
    if spec.kind not in IMPLEMENTED_KINDS:
        raise AdapterNotAvailable(
            f"Adapter kind {spec.kind!r} is not implemented in this release yet (planned for M4/M5); "
            "register it as a generic agent or upgrade Maestro"
        )
    if spec.kind == "codex":
        return CodexAdapter(spec)
    return ClaudeCodeAdapter(spec)
