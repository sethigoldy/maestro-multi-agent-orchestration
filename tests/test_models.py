from __future__ import annotations

from pathlib import Path
from maestro.models import Task, Phase


def test_task_defaults_and_custom_fields():
    task = Task("t", "title", "request", Path("design"), Phase.DESIGNED)
    assert task.metadata == {}
    assert task.codex_model is None and task.codex_effort is None
    task2 = Task("t2", "title", "r", Path("d"), Phase.COMPLETE, {"x": 1}, "m", "max")
    assert task2.metadata == {"x": 1}
    assert task2.codex_model == "m" and task2.codex_effort == "max"
