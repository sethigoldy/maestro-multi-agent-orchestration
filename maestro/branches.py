"""Task branch names: validation, and renaming a finished task's branch.

A task gets its own git branch the first time an agent works on it. By default
the name is ``maestro/<task_id>``. A handoff can name the branch instead
(``[expectations] branch``, ``maestro delegate --branch``, or the MCP
``delegate`` tool's ``branch`` argument).

A task's branch can be renamed later with :func:`rename_task_branch`. It
renames the git branch and updates the task's durable record in the same step,
so ``maestro task list``, ``task status``, receipts and follow-up turns all use
the new name. If the branch was already renamed by hand with ``git branch -m``,
the same call only updates the record, after checking git's reflog to confirm
that the new branch really is the task's branch under a new name. If the task
has no branch yet (for example its first turn could not create the branch it
asked for), the call changes the name its next turn will create.

The branch name only applies to the workspace of the daemon that runs the
task. A handoff forwarded to a remote daemon does not carry it; the remote
daemon puts its work on its own default branch.
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


def _local_branches(workspace: Path) -> list[str]:
    """Names of every local branch in the workspace's repository."""
    listed = subprocess.run(
        ["git", "-C", str(workspace), "for-each-ref", "--format=%(refname)", "refs/heads/"],
        text=True, capture_output=True,
    )
    return [line.removeprefix("refs/heads/") for line in listed.stdout.splitlines() if line]


def branch_name_clash(workspace: Path, name: str, ignore: str | None = None) -> str | None:
    """Say why a new branch called ``name`` cannot be created, or return None.

    Git stores ``feat/login`` as a file ``login`` inside a folder ``feat``, so
    a branch name cannot also be the folder of another branch. When ``feat``
    exists, ``feat/login`` cannot be created, and when ``feat/x`` exists,
    ``feat`` cannot be created. ``ignore`` names a branch to leave out of the
    check: the branch being renamed, which git moves out of the way first.
    """
    for existing in _local_branches(workspace):
        if existing == ignore:
            continue
        if existing == name:
            return f"a branch named {name!r} already exists"
        if existing.startswith(name + "/") or name.startswith(existing + "/"):
            return (
                f"the branch {existing!r} already exists, and git cannot have both {existing!r} and {name!r} "
                "because one name would have to be a folder holding the other"
            )
    return None


def was_renamed_from(workspace: Path, name: str, old_name: str) -> bool:
    """True when git's reflog for ``name`` records a rename from ``old_name``.

    ``git branch -m`` writes "Branch: renamed refs/heads/<old> to
    refs/heads/<new>" and carries the reflog over to the new name, so the
    entry survives a chain of renames (old -> a -> b).
    """
    log = subprocess.run(
        ["git", "-C", str(workspace), "reflog", "show", "--format=%gs", f"refs/heads/{name}", "--"],
        text=True, capture_output=True,
    )
    return log.returncode == 0 and f"renamed refs/heads/{old_name} to " in log.stdout


def find_renamed_branches(workspace: Path, old_name: str) -> list[str]:
    """Local branches whose reflog shows they were renamed from ``old_name``.

    Usually there is one. There can be more when a renamed branch was later
    copied with ``git branch -c``, because the copy takes the reflog with it.
    """
    return [name for name in _local_branches(workspace) if was_renamed_from(workspace, name, old_name)]


def rename_task_branch(maestro: "Maestro", task_id: str, new_branch: str) -> dict[str, Any]:
    """Rename a task's branch in git and in the task's durable record.

    Four cases are handled:

    * The recorded branch exists and ``new_branch`` can be created (no branch
      has that name, and no branch clashes with it as a folder, see
      :func:`branch_name_clash`): the git branch is renamed with
      ``git branch -m`` and the record is updated.
    * The recorded branch is gone and ``new_branch`` exists, and git's reflog
      for ``new_branch`` shows it was renamed from the recorded branch: it was
      renamed by hand, so only the record is updated. Without that reflog
      entry the call is refused, because an unrelated branch that happens to
      exist must never become the task's branch.
    * The task has no branch yet, because its first turn has not run or could
      not create the branch: the name its next turn will create is changed in
      the task's handoff record. ``new_branch`` must be a name that can be
      created, as at delegation.
    * Anything else is refused with a ValueError that says which branch is
      missing or already taken. Nothing is changed in that case.

    Only the local branch is renamed. A copy already pushed to a remote keeps
    its old name there.

    Returns ``{"task_id", "old_branch", "branch", "git_renamed"}``, where
    ``git_renamed`` is False when only the record changed. When the task had no
    branch yet, the result also has ``"pending": True``, and ``old_branch`` is
    the name the next turn would have created.
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
    workspace = Path(workspace_raw)
    if not old_branch:
        return _rename_pending_branch(maestro, task_id, workspace, runtime, new_branch)
    if old_branch == new_branch:
        return {"task_id": task_id, "old_branch": old_branch, "branch": new_branch, "git_renamed": False}
    git_renamed = False
    if branch_exists(workspace, old_branch):
        clash = branch_name_clash(workspace, new_branch, ignore=old_branch)
        if clash:
            raise ValueError(f"Cannot rename {old_branch!r} to {new_branch!r}: {clash}")
        moved = subprocess.run(
            ["git", "-C", str(workspace), "branch", "-m", old_branch, new_branch],
            text=True, capture_output=True,
        )
        if moved.returncode != 0:
            raise ValueError(f"git branch -m {old_branch} {new_branch} failed: {(moved.stderr or moved.stdout).strip()}")
        git_renamed = True
    elif not branch_exists(workspace, new_branch):
        raise ValueError(
            f"Neither the task's branch {old_branch!r} nor {new_branch!r} exists in {workspace}; "
            "nothing to rename and nothing to record"
        )
    elif not was_renamed_from(workspace, new_branch, old_branch):
        raise ValueError(
            f"The task's branch {old_branch!r} no longer exists, and git has no record that {new_branch!r} "
            f"was renamed from it, so {new_branch!r} may be an unrelated branch. If it is the task's branch, "
            f"rename it back with 'git branch -m {new_branch} {old_branch}' and run this command again"
        )
    maestro._write_claim(task_id, "task_branch", new_branch)
    if runtime:
        runtime["branch"] = new_branch
        maestro._write_claim(task_id, "task_runtime", json.dumps(runtime, ensure_ascii=False))
    # The knowledge snapshot names the branch; re-project it so receipts and
    # the next follow-up's context show the new name.
    # Changed files are read where the task's work is: its run directory.
    claims = maestro._claims(task_id)
    run_dir = Path(claims["task_run_dir"]) if claims.get("task_run_dir") else workspace
    knowledge = project_knowledge(task_id, claims, runtime, workspace=run_dir)
    maestro._write_claim(task_id, "task_knowledge", knowledge.serialize())
    return {"task_id": task_id, "old_branch": old_branch, "branch": new_branch, "git_renamed": git_renamed}


def _rename_pending_branch(
    maestro: "Maestro", task_id: str, workspace: Path, runtime: dict[str, Any], new_branch: str,
) -> dict[str, Any]:
    """Change the branch name a task's next turn will create.

    Used when the task has no branch yet. The name is stored as
    ``[expectations] branch`` in the task's handoff record, which is where the
    next turn (a follow-up, or the answer to a question) reads it from.
    """
    doc = runtime.get("doc")
    expectations = doc.get("expectations") if isinstance(doc, dict) else None
    if not isinstance(expectations, dict):
        raise ValueError(f"Task {task_id} has no branch yet, and its handoff was not recorded, so there is no branch name to change")
    if expectations.get("commit_policy") == "no-commit":
        raise ValueError(
            f"Task {task_id} has no branch yet and will not get one, because it runs with commit_policy='no-commit'"
        )
    old_branch = expectations.get("branch") or f"maestro/{task_id}"
    result = {"task_id": task_id, "old_branch": old_branch, "branch": new_branch, "git_renamed": False, "pending": True}
    if old_branch == new_branch:
        return result
    clash = branch_name_clash(workspace, new_branch)
    if clash:
        raise ValueError(f"Cannot use {new_branch!r} as the branch for task {task_id}: {clash}")
    expectations["branch"] = new_branch
    maestro._write_claim(task_id, "task_runtime", json.dumps(runtime, ensure_ascii=False))
    return result
