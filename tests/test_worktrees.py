"""Git worktree helpers used for tasks that run next to a busy workspace."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from maestro import worktrees

_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ws), *args], text=True, capture_output=True, env=_ENV)


def _repo(tmp_path: Path, commit: bool = True) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    if commit:
        (ws / "README.md").write_text("# repo\n", encoding="utf-8")
        _git(ws, "add", ".")
        _git(ws, "commit", "-qm", "initial")
    return ws


def test_worktree_path_is_under_state_dir_and_named_by_task(tmp_path):
    assert worktrees.worktree_path(tmp_path, "task-1") == tmp_path / "worktrees" / "task-1"


def test_head_commit_and_no_commit(tmp_path):
    ws = _repo(tmp_path)
    assert len(worktrees.head_commit(ws)) == 40
    (tmp_path / "e").mkdir()
    empty = _repo(tmp_path / "e", commit=False)
    with pytest.raises(RuntimeError, match="has no commit yet"):
        worktrees.head_commit(empty)


def test_add_worktree_new_branch_from_commit_leaves_workspace_alone(tmp_path):
    ws = _repo(tmp_path)
    (ws / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    path = tmp_path / "home" / "worktrees" / "task-1"
    worktrees.add_worktree(ws, path, "maestro/task-1", new_branch=True, start=worktrees.head_commit(ws))
    assert (path / "README.md").is_file()
    assert not (path / "wip.txt").exists()  # uncommitted workspace changes are not copied
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "maestro/task-1"
    assert _git(ws, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "maestro/task-1"


def test_add_worktree_failure_raises_with_git_message(tmp_path):
    ws = _repo(tmp_path)
    path = tmp_path / "wt"
    worktrees.add_worktree(ws, path, "b1", new_branch=True, start=worktrees.head_commit(ws))
    with pytest.raises(RuntimeError, match="could not create the worktree"):
        worktrees.add_worktree(ws, tmp_path / "wt2", "b1", new_branch=True, start=worktrees.head_commit(ws))


def test_ensure_worktree_recreates_a_deleted_worktree(tmp_path):
    import shutil

    ws = _repo(tmp_path)
    path = tmp_path / "wt"
    worktrees.add_worktree(ws, path, "b1", new_branch=True, start=worktrees.head_commit(ws))
    assert worktrees.ensure_worktree(ws, path, "b1") is False  # exists: nothing to do
    shutil.rmtree(path)
    assert worktrees.ensure_worktree(ws, path, "b1") is True
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "b1"


def test_dirty_files_and_remove(tmp_path):
    ws = _repo(tmp_path)
    path = tmp_path / "wt"
    worktrees.add_worktree(ws, path, "b1", new_branch=True, start=worktrees.head_commit(ws))
    assert worktrees.dirty_files(path) == []
    (path / "new.txt").write_text("x\n", encoding="utf-8")
    (path / "README.md").write_text("changed\n", encoding="utf-8")
    assert worktrees.dirty_files(path) == ["README.md", "new.txt"]
    with pytest.raises(RuntimeError, match="could not remove the worktree"):
        worktrees.remove_worktree(ws, path, force=False)
    worktrees.remove_worktree(ws, path, force=True)
    assert not path.exists()
    assert _git(ws, "rev-parse", "--verify", "b1").returncode == 0  # the branch is kept


def test_dirty_files_of_missing_dir_is_empty(tmp_path):
    assert worktrees.dirty_files(tmp_path / "missing") == []


def test_main_repo_root_from_worktree_and_non_repo(tmp_path):
    ws = _repo(tmp_path)
    path = tmp_path / "wt"
    worktrees.add_worktree(ws, path, "b1", new_branch=True, start=worktrees.head_commit(ws))
    assert worktrees.main_repo_root(path) == ws.resolve()
    assert worktrees.main_repo_root(ws) == ws.resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktrees.main_repo_root(plain) is None


def test_dirty_files_of_a_directory_outside_git_is_empty(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("x\n", encoding="utf-8")
    assert worktrees.dirty_files(plain) == []


def test_repo_prefix_of_root_and_subdirectory(tmp_path):
    ws = _repo(tmp_path)
    (ws / "pkg" / "sub").mkdir(parents=True)
    assert worktrees.repo_prefix(ws) == ""
    assert worktrees.repo_prefix(ws / "pkg" / "sub") == "pkg/sub"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktrees.repo_prefix(plain) == ""
