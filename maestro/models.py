from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Phase(str, Enum):
    DESIGNED = "DESIGNED"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    FIXING = "FIXING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class AgentState(str, Enum):
    """A2A-aligned task states (subset of the A2A TaskState used by Maestro)."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


_PHASE_TO_AGENT_STATE: dict[Phase, AgentState] = {
    Phase.DESIGNED: AgentState.SUBMITTED,
    Phase.IMPLEMENTING: AgentState.WORKING,
    Phase.VERIFYING: AgentState.WORKING,
    Phase.REVIEWING: AgentState.INPUT_REQUIRED,
    Phase.FIXING: AgentState.WORKING,
    Phase.COMPLETE: AgentState.COMPLETED,
    Phase.FAILED: AgentState.FAILED,
}


def to_agent_state(phase: Phase) -> AgentState:
    """Map an internal work phase onto the A2A-aligned task state."""
    return _PHASE_TO_AGENT_STATE[phase]


@dataclass
class Task:
    task_id: str
    title: str
    request: str
    design_path: Path
    phase: Phase
    metadata: dict[str, Any] = field(default_factory=dict)
    codex_model: str | None = None
    codex_effort: str | None = None
    # Multi-agent routing (Maestro 1.0): which agents own this task and where it came from.
    origin_agent: str | None = None
    target_agent: str | None = None
    parent_task_id: str | None = None
