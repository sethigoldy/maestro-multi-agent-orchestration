"""Task branch names: validation, and renaming a finished task's branch.

A task gets its own git branch the first time an agent works on it. By default
the name is ``maestro/<task_id>``. A handoff can name the branch instead
(``[expectations] branch``, ``maestro delegate --branch``, or the MCP
``delegate`` tool's ``branch`` argument).

A branch that was created under the default name can be renamed later with
:func:`rename_task_branch`. It renames the git branch and updates the task's
durable record in the same step, so ``maestro task list``, ``task status``,
receipts and follow-up turns all use the new name. If the branch was already
renamed by hand with ``git branch -m``, the same call only updates the record.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .core import Maestro

_FORBIDDEN_CHARS = set(" ~^:?*[\\")


def validate_branch_name(name: Any) -> str:
    """Return ``name`` stripped of surrounding spaces, or raise ValueError.

    The rules are the ones ``git check-ref-format --branch`` applies, checked
    in Python so a bad name is rejected when the handoff is read, before any
    agent starts.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Branch name must be a non-empty string")
    value = name.strip()
    problem = None
    if value.startswith("-"):
        problem = "it must not start with '-'"
    elif value in ("@", "HEAD"):
        problem = f"{value!r} is reserved by git"
    elif any(ord(ch) < 32 or ord(ch) == 127 or ch in _FORBIDDEN_CHARS for ch in value):
        problem = "it must not contain spaces, control characters or any of ~ ^ : ? * [ \\"
    elif ".." in value or "@{" in value:
        problem = "it must not contain '..' or '@{'"
    elif value.startswith("/") or value.endswith("/") or "//" in value:
        problem = "it must not start or end with '/' or contain '//'"
    elif value.endswith("."):
        problem = "it must not end with '.'"
    elif any(part.startswith(".") or part.endswith(".lock") for part in value.split("/")):
        problem = "no part between slashes may start with '.' or end with '.lock'"
    if problem:
        raise ValueError(f"Invalid branch name {value!r}: {problem}")
    return value


def branch_exists(workspace: Path, name: str) -> bool:
    """True when ``refs/heads/<name>`` exists in the workspace's repository."""
    probe = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        text=True, capture_output=True,
    )
    return probe.returncode == 0


def rename_task_branch(maestro: "Maestro", task_id: str, new_branch: str) -> dict[str, Any]:
    """Rename a task's branch in git and in the task's durable record.

    Three cases are handled:

    * The recorded branch exists and ``new_branch`` does not: the git branch is
      renamed with ``git branch -m`` and the record is updated.
    * The recorded branch is gone and ``new_branch`` exists: someone already
      renamed it by hand, so only the record is updated.
    * Anything else is refused with a ValueError that says which branch is
      missing or already taken. Nothing is changed in that case.

    Only the local branch is renamed. A copy already pushed to a remote keeps
    its old name there.

    Returns ``{"task_id", "old_branch", "branch", "git_renamed"}``, where
    ``git_renamed`` is False when only the record changed.
    """
    from .knowledge import project_knowledge

    new_branch = validate_branch_name(new_branch)
    claims = maestro._claims(task_id)
    workspace_raw = claims.get("task_workspace")
    if not workspace_raw:
        raise KeyError(f"Unknown task reference {task_id!r}")
    runtime: dict[str, Any] = {}
    try:
        parsed = json.loads(claims.get("task_runtime") or "{}")
        if isinstance(parsed, dict):
            runtime = parsed
    except ValueError:
        pass
    old_branch = claims.get("task_branch") or runtime.get("branch")
    if not old_branch:
        raise ValueError(
            f"Task {task_id} has no branch yet (it has not started, or it runs with commit_policy='no-commit')"
        )
    workspace = Path(workspace_raw)
    if old_branch == new_branch:
        return {"task_id": task_id, "old_branch": old_branch, "branch": new_branch, "git_renamed": False}
    old_exists = branch_exists(workspace, old_branch)
    new_exists = branch_exists(workspace, new_branch)
    if old_exists and new_exists:
        raise ValueError(f"Cannot rename {old_branch!r} to {new_branch!r}: a branch named {new_branch!r} already exists")
    if not old_exists and not new_exists:
        raise ValueError(
            f"Neither the task's branch {old_branch!r} nor {new_branch!r} exists in {workspace}; "
            "nothing to rename and nothing to record"
        )
    git_renamed = False
    if old_exists:
        moved = subprocess.run(
            ["git", "-C", str(workspace), "branch", "-m", old_branch, new_branch],
            text=True, capture_output=True,
        )
        if moved.returncode != 0:
            raise ValueError(f"git branch -m {old_branch} {new_branch} failed: {(moved.stderr or moved.stdout).strip()}")
        git_renamed = True
    maestro._write_claim(task_id, "task_branch", new_branch)
    if runtime:
        runtime["branch"] = new_branch
        maestro._write_claim(task_id, "task_runtime", json.dumps(runtime, ensure_ascii=False))
    # The knowledge snapshot names the branch; re-project it so receipts and
    # the next follow-up's context show the new name.
    knowledge = project_knowledge(task_id, maestro._claims(task_id), runtime, workspace=workspace)
    maestro._write_claim(task_id, "task_knowledge", knowledge.serialize())
    return {"task_id": task_id, "old_branch": old_branch, "branch": new_branch, "git_renamed": git_renamed}
