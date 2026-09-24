"""Git worktrees for tasks that run next to a busy workspace.

When a task would wait because another task is using its workspace, the
daemon gives it its own worktree under ``<state dir>/worktrees/<task id>``
(see docs/design-parallel-tasks.md). Every function here talks to git and
nothing else; the daemon decides when to call them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True)


def _message(run: subprocess.CompletedProcess) -> str:
    return (run.stderr or run.stdout).strip()


def worktree_path(state_dir: Path, task_id: str) -> Path:
    """Where a task's worktree lives. The path uses the task id, so renaming
    the task's branch never makes it wrong."""
    return Path(state_dir) / "worktrees" / task_id


def head_commit(workspace: Path) -> str:
    """The commit checked out in the workspace. A new worktree starts here."""
    run = _git(workspace, "rev-parse", "--verify", "HEAD")
    if run.returncode != 0:
        raise RuntimeError(
            f"the workspace {workspace} has no commit yet, so a worktree cannot be created for this task; "
            "commit something in the workspace first"
        )
    return run.stdout.strip()


def main_repo_root(path: Path) -> Path | None:
    """The main checkout of the repository that ``path`` belongs to, or None
    when ``path`` is not in a git repository. For a worktree this is the
    checkout that owns it, where the project's virtual environment usually is."""
    run = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if run.returncode != 0:
        return None
    common = Path(run.stdout.strip())
    return common.parent.resolve() if common.name == ".git" else common.resolve()


def add_worktree(workspace: Path, path: Path, branch: str, *, new_branch: bool, start: str | None = None) -> None:
    """Create a worktree at ``path`` with ``branch`` checked out.

    With ``new_branch`` the branch is created from ``start``; otherwise the
    existing branch is checked out. Git's message is kept on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add"]
    args += ["-b", branch, str(path)] + ([start] if start else []) if new_branch else [str(path), branch]
    run = _git(workspace, *args)
    if run.returncode != 0:
        raise RuntimeError(f"could not create the worktree {path} for branch {branch!r}: {_message(run)}")


def ensure_worktree(workspace: Path, path: Path, branch: str) -> bool:
    """Make sure the worktree exists. Returns True when it had to be created
    again (it was removed by cleanup or deleted by hand). Only committed work
    comes back; uncommitted work was in the removed directory."""
    if path.is_dir():
        return False
    _git(workspace, "worktree", "prune")
    add_worktree(workspace, path, branch, new_branch=False)
    return True


def dirty_files(path: Path) -> list[str]:
    """Files with uncommitted changes in the worktree (tracked and untracked)."""
    if not path.is_dir():
        return []
    run = _git(path, "status", "--porcelain", "--untracked-files=all")
    if run.returncode != 0:
        return []
    return sorted(line[3:] for line in run.stdout.splitlines() if line.strip())


def remove_worktree(workspace: Path, path: Path, *, force: bool) -> None:
    """Remove the worktree. The branch and its commits are kept."""
    args = ["worktree", "remove"] + (["--force"] if force else []) + [str(path)]
    run = _git(workspace, *args)
    if run.returncode != 0:
        raise RuntimeError(f"could not remove the worktree {path}: {_message(run)}")
