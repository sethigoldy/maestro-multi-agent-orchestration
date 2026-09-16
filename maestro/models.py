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
