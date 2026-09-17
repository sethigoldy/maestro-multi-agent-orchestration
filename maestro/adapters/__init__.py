"""Adapter factory: registry spec → adapter instance."""

from __future__ import annotations

from ..agents import AgentSpec
from .a2a_remote import A2ARemoteAdapter
from .base import AdapterNotAvailable, BaseAdapter
from .claude_code import ClaudeCodeAdapter
from .cline import ClineAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter
from .generic import GenericAdapter
from .hermes import HermesAdapter
from .openhands import OpenHandsAdapter
from .pi import PiAdapter

__all__ = [
    "A2ARemoteAdapter",
    "AdapterNotAvailable",
    "BaseAdapter",
    "ClaudeCodeAdapter",
    "ClineAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "GenericAdapter",
    "HermesAdapter",
    "OpenHandsAdapter",
    "PiAdapter",
    "make_adapter",
]

#: Kinds implemented in this release (M2+M4+M5+v2). copilot lands in v2-M5.
IMPLEMENTED_KINDS: frozenset[str] = frozenset(
    {"codex", "claude_code", "generic", "pi", "cline", "hermes", "cursor", "openhands", "a2a_remote"}
)


def make_adapter(spec: AgentSpec) -> BaseAdapter:
    if spec.kind == "generic":
        return GenericAdapter(spec)
    if spec.kind not in IMPLEMENTED_KINDS:
        raise AdapterNotAvailable(
            f"Adapter kind {spec.kind!r} is not implemented in this release yet; "
            "register it as a generic agent or upgrade Maestro"
        )
    adapters = {
        "codex": CodexAdapter,
        "claude_code": ClaudeCodeAdapter,
        "pi": PiAdapter,
        "cline": ClineAdapter,
        "hermes": HermesAdapter,
        "cursor": CursorAdapter,
        "openhands": OpenHandsAdapter,
        "a2a_remote": A2ARemoteAdapter,
    }
    return adapters[spec.kind](spec)
