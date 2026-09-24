# Parallel Tasks with Per-Task Worktrees Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a task would wait because another task is using its workspace, Maestro runs it in its own git worktree instead, so up to `max_parallel` tasks for one workspace run at the same time.

**Architecture:** Every task gets a run directory (`run_dir`) and a kind (`workspace` or `worktree`), chosen under the daemon lock when a turn is scheduled. The existing workspace slot (`_active`) keeps meaning "a task whose run directory is the workspace is running or parked there". A new capacity check counts the tasks of a workspace that are running a turn. The turn thread creates the worktree (`git worktree add`) and every git, agent and verification step of the turn uses `run_dir`; context files and skills still resolve against the workspace.

**Tech Stack:** Python 3.11+, stdlib `subprocess` + git CLI (>= 2.30), pytest with pytest-xdist and pytest-cov (100% line and branch coverage gate), React console bundled with esbuild (`web/`, checked-in `maestro/web_dist/`).

**Spec:** `docs/design-parallel-tasks.md`

## Global Constraints

- Maestro never commits, merges or pushes. Worktrees hold uncommitted work.
- The first task for a free workspace runs in the workspace itself, exactly as today (`git checkout -b <branch>` in the workspace).
- Worktree path: `<MAESTRO_HOME>/worktrees/<task-id>` (the daemon's `state_dir / "worktrees" / task_id`).
- A new worktree starts from the commit checked out in the workspace (`git rev-parse HEAD`); a workspace with no commit fails the turn with a message that says so.
- `[defaults] max_parallel`: integer >= 1, default 4. It counts tasks running a turn for one workspace path. `1` reproduces today's behaviour.
- `commit_policy = "no-commit"` tasks never get a worktree. Tasks whose target is an `a2a_remote` agent never get a worktree.
- If `git worktree add` fails, the task fails with git's message. Never fall back to the workspace.
- Maestro never removes a worktree with uncommitted changes unless `--force` is given, never removes a branch, and never removes or changes the user's workspace.
- Old tasks (no run-directory data) are treated as having run in their workspace.
- New stored claims: `task_run_dir` (absolute path), `task_run_dir_kind` (`workspace` | `worktree`), `task_run_dir_removed` (`true` when removed by cleanup; absent otherwise).
- Coverage stays at 100% (`python -m pytest -n auto -q --cov --cov-fail-under=100`).
- All user-facing text, docs, comments and commit messages are plain full sentences. No AI or model names in commits or PR text.

## Review Focus

- A follow-up or answer for a task that ran in the workspace, sent while another task runs in the workspace, must queue and must not start in a worktree (its uncommitted work is in the workspace). Test in Task 3.
- A task parked on a question inside a worktree must free its place under the limit, and the next queued task must start at that moment, not when some other task finishes. Test in Task 3.
- Renaming the branch of a task that runs in a worktree (`task rename-branch`) must leave the next turn working in the same worktree on the renamed branch. Test in Task 4.
- Python verification inside a new worktree must find the main repository's `.venv/`, or every Python task in a worktree fails with "pytest missing". Test in Task 4.
- `maestro gc` must never delete the record of a task whose worktree still has uncommitted changes, because the record is how the user finds that worktree. Test in Task 6.

## File Structure

- Create `maestro/worktrees.py`: all git worktree operations (path, create, re-create, dirty files, remove, main repository root). One responsibility: talk to git about worktrees.
- Modify `maestro/core.py`: parse `[defaults] max_parallel`; add `run_dir`, `run_dir_kind`, `run_dir_removed` to `Maestro.status`.
- Modify `maestro/daemon.py`: scheduling (`_claim_turn`, `_pump_queue`, `_release`), `_prepare_run_dir` in `_run_turn`, run_dir through verification and gates, `cleanup_worktree`, durable record keys, `status_a2a` metadata.
- Modify `maestro/worker.py`: `_python_executable` falls back to the main repository's virtual environment.
- Modify `maestro/knowledge.py`: read changed files from the run directory.
- Modify `maestro/a2a.py`: `tasks/cleanup` method; queued reason in delegate result.
- Modify `maestro/mcp_server.py`: `cleanup_task_worktree` tool; delegate message wording.
- Modify `maestro/cli.py`: `task cleanup`, delegate message wording, audit field, `gc` worktree handling.
- Modify `maestro/receipt.py`, `maestro/tui.py`, `web/src/lib/events.js`, `web/src/components/DetailPane.jsx`, then rebuild `maestro/web_dist/`.
- Tests: `tests/test_worktrees.py` (new), `tests/test_parallel_tasks.py` (new), plus additions to `tests/test_core.py`, `tests/test_worker.py`, `tests/test_cli.py`, `tests/test_tui.py`, `tests/test_docs_usage.py`.
- Docs: `docs/design-parallel-tasks.md` (status line), `README.md`, `docs/usage/explanation/concepts.md`, `docs/usage/reference/cli.md`, `docs/usage/reference/configuration.md`, `docs/usage/reference/mcp-tools.md`, `docs/usage/how-to/manage-in-flight-tasks.md`, `CHANGELOG.md`.

---

### Task 1: Worktree helpers

**Files:**
- Create: `maestro/worktrees.py`
- Test: `tests/test_worktrees.py`

**Interfaces:**
- Produces:
  - `worktree_path(state_dir: Path, task_id: str) -> Path`
  - `head_commit(workspace: Path) -> str` (raises `RuntimeError` when the workspace has no commit)
  - `main_repo_root(path: Path) -> Path | None`
  - `add_worktree(workspace: Path, path: Path, branch: str, *, new_branch: bool, start: str | None = None) -> None` (raises `RuntimeError` with git's message)
  - `ensure_worktree(workspace: Path, path: Path, branch: str) -> bool` (returns True when it had to re-create the worktree)
  - `dirty_files(path: Path) -> list[str]`
  - `remove_worktree(workspace: Path, path: Path, *, force: bool) -> None` (raises `RuntimeError`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_worktrees.py
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
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `python -m pytest -q tests/test_worktrees.py`
Expected: FAIL with `ImportError: cannot import name 'worktrees'`.

- [ ] **Step 3: Implement `maestro/worktrees.py`**

```python
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
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `python -m pytest -q tests/test_worktrees.py`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add maestro/worktrees.py tests/test_worktrees.py
git commit -m "Add git worktree helpers for tasks that run next to a busy workspace"
```

---

### Task 2: The `max_parallel` setting

**Files:**
- Modify: `maestro/core.py` (`_parse_defaults`, around line 373)
- Test: `tests/test_core.py`

**Interfaces:**
- Produces: `Maestro.config["defaults"]["max_parallel"]` is present only when set in config; the daemon reads it with `MaestroDaemon._max_parallel() -> int` (added in Task 3) which returns 4 when it is absent.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_core.py`)

```python
def test_defaults_max_parallel_parsed_and_validated():
    assert Maestro._parse_defaults({"max_parallel": 2}) == {"max_parallel": 2}
    assert Maestro._parse_defaults({}) == {}
    for bad in (0, -1, "4", 2.5, True):
        with pytest.raises(ValueError, match="max_parallel must be a whole number of at least 1"):
            Maestro._parse_defaults({"max_parallel": bad})
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m pytest -q tests/test_core.py -k max_parallel`
Expected: FAIL with `[defaults] has unknown keys: max_parallel`.

- [ ] **Step 3: Implement** in `_parse_defaults`: add `"max_parallel"` to the allowed key set, and before `return out`:

```python
        max_parallel = raw.get("max_parallel")
        if max_parallel is not None:
            if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel < 1:
                raise ValueError("[defaults] max_parallel must be a whole number of at least 1")
            out["max_parallel"] = max_parallel
```

Update the docstring to list `max_parallel` (the number of tasks that may run turns at the same time for one workspace).

- [ ] **Step 4: Run to see it pass**

Run: `python -m pytest -q tests/test_core.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/core.py tests/test_core.py
git commit -m "Accept [defaults] max_parallel in the config"
```

---

### Task 3: Scheduling — choose where each turn runs

**Files:**
- Modify: `maestro/daemon.py` — `delegate` (~line 742), `_start_queued` (~861), `_release` (~1127), `_acquire_or_queue` (~1801), `_set_state` (~900), `_make_record`, `cancel`
- Test: `tests/test_parallel_tasks.py` (new)

**Interfaces:**
- Consumes: `worktrees.worktree_path` (Task 1), `config["defaults"]["max_parallel"]` (Task 2).
- Produces:
  - Record fields `run_dir: str | None`, `run_dir_kind: "workspace" | "worktree" | None`.
  - `MaestroDaemon._max_parallel() -> int`
  - `MaestroDaemon._running_count(key: str) -> int`
  - `MaestroDaemon._claim_turn(task_id: str) -> str | None` — under the lock; returns the kind the turn will run in, or None when the task must queue. Sets `run_dir`/`run_dir_kind` on first claim and takes `_active[key]` for the workspace kind.
  - `MaestroDaemon._queue_reason(task_id: str) -> str` — plain text for why a task is queued.
  - `MaestroDaemon._pump_queue() -> None` — start every queued task that can now claim a turn, FIFO.
  - `delegate` result gains `run_dir` (str) and, when queued, `reason` (str).

The rules (docs/design-parallel-tasks.md section 3), implemented in `_claim_turn`:

```python
    def _max_parallel(self) -> int:
        return int((self.maestro.config.get("defaults") or {}).get("max_parallel", 4))

    def _running_count(self, key: str) -> int:
        """Tasks of this workspace that are running a turn now. Parked,
        finished and queued tasks do not count."""
        return sum(
            1 for tid, rec in self._tasks.items()
            if rec.get("workspace") == key and not rec.get("queued")
            and (rec.get("state") in (STATE_SUBMITTED, STATE_WORKING) or tid in self._turn_starting)
        )

    def _worktree_allowed(self, record: dict[str, Any]) -> bool:
        doc = record.get("doc") or {}
        expectations = doc.get("expectations") or {}
        if (expectations.get("commit_policy") or "branch") == "no-commit":
            return False
        target = (doc.get("routing") or {}).get("target_agent")
        spec = self.registry.get(target) if target else None
        return not (spec is not None and spec.kind == "a2a_remote")

    def _claim_turn(self, task_id: str) -> str | None:
        """Decide where the task's next turn runs. Caller holds the lock.

        Returns "workspace" or "worktree", or None when the turn must queue.
        See docs/design-parallel-tasks.md, section 3."""
        record = self._tasks[task_id]
        key = record["workspace"]
        # The task itself is not running yet, so it is not in the count.
        if self._running_count(key) - (1 if self._counts_self(task_id) else 0) >= self._max_parallel():
            return None
        kind = record.get("run_dir_kind")
        if kind == "worktree":
            return "worktree"
        if kind == "workspace" or not self._worktree_allowed(record):
            if self._active.get(key) not in (None, task_id):
                return None
            self._active[key] = task_id
            record["run_dir"], record["run_dir_kind"] = key, "workspace"
            return "workspace"
        if self._active.get(key) in (None, task_id):
            self._active[key] = task_id
            record["run_dir"], record["run_dir_kind"] = key, "workspace"
            return "workspace"
        record["run_dir"] = str(worktrees.worktree_path(self.state_dir, task_id))
        record["run_dir_kind"] = "worktree"
        return "worktree"

    def _counts_self(self, task_id: str) -> bool:
        rec = self._tasks[task_id]
        return not rec.get("queued") and (rec.get("state") in (STATE_SUBMITTED, STATE_WORKING) or task_id in self._turn_starting)
```

Note on `_worktree_allowed`: `record["doc"]` is `HandoffDoc.to_dict()`; check its real key layout in `maestro/handoff.py` (`to_dict`, ~line 90) and read `commit_policy` and `target_agent` from where it puts them. Write the helper against the real layout.

- [ ] **Step 1: Write the failing tests** (`tests/test_parallel_tasks.py`)

```python
"""Parallel tasks in one workspace: where each turn runs (design-parallel-tasks.md)."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from maestro.daemon import MaestroDaemon
from maestro.handoff import HandoffDoc

_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _fake_bin(dirpath: Path, name: str, body: str) -> None:
    path = dirpath / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _repo(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for args in (("init", "-q"),):
        subprocess.run(["git", "-C", str(ws), *args], env=_ENV, check=True)
    (ws / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "."], env=_ENV, check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "initial"], env=_ENV, check=True)
    return ws


def _doc(**kw) -> HandoffDoc:
    base = dict(title="T", request="R", verification="none", commit_policy="branch", target_agent="codex", explicit_target=True)
    base.update(kw)
    return HandoffDoc(**base)


def _daemon(tmp_path, monkeypatch, config: str = "") -> MaestroDaemon:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if config:
        (home / "config.toml").write_text(config, encoding="utf-8")
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    return MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)


@pytest.fixture
def binpath(tmp_path, monkeypatch):
    bp = tmp_path / "bin"
    bp.mkdir()
    monkeypatch.setenv("PATH", f"{bp}:{os.environ['PATH']}")
    return bp


def _slow_agent(binpath: Path, gate: Path) -> None:
    # Waits until the test creates the gate file, so tasks overlap in time.
    _fake_bin(binpath, "codex", f'cat > /dev/null\nwhile [ ! -f "{gate}" ]; do sleep 0.05; done\necho "$PWD" > ran-here.txt\nexit 0')


def _stop(d: MaestroDaemon, gate: Path) -> None:
    gate.touch()
    for tid in list(d._tasks):
        try:
            d.wait(tid, timeout=30)
        except Exception:
            pass
    d.stop()


def test_second_task_runs_in_a_worktree_at_once(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        first = d.delegate(_doc(title="first"), ws)
        second = d.delegate(_doc(title="second"), ws)
        assert first["queued"] is False and first["run_dir"] == str(ws)
        assert second["queued"] is False
        assert second["run_dir"] == str(d.state_dir / "worktrees" / second["task_id"])
    finally:
        _stop(d, gate)


def test_limit_queues_with_a_reason_and_releases_on_finish(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch, "[defaults]\nmax_parallel = 2\n")
    try:
        d.delegate(_doc(), ws)
        d.delegate(_doc(), ws)
        third = d.delegate(_doc(), ws)
        assert third["queued"] is True
        assert "limit of 2 running tasks" in third["reason"]
        gate.touch()
        final = d.wait(third["task_id"], timeout=60)
        assert final["status"]["state"] == "completed"
    finally:
        _stop(d, gate)


def test_max_parallel_one_keeps_todays_queue(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch, "[defaults]\nmax_parallel = 1\n")
    try:
        d.delegate(_doc(), ws)
        assert d.delegate(_doc(), ws)["queued"] is True
    finally:
        _stop(d, gate)


def test_no_commit_task_waits_for_the_workspace(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        d.delegate(_doc(), ws)
        waiting = d.delegate(_doc(commit_policy="no-commit"), ws)
        assert waiting["queued"] is True
        assert "works in place" in waiting["reason"]
    finally:
        _stop(d, gate)


def test_followup_of_a_workspace_task_queues_while_another_task_uses_the_workspace(tmp_path, monkeypatch, binpath):
    _fake_bin(binpath, "codex", "cat > /dev/null\nexit 0")
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        done = d.delegate(_doc(), ws)
        d.wait(done["task_id"], timeout=60)
        gate = tmp_path / "go"
        _slow_agent(binpath, gate)
        d.delegate(_doc(), ws)  # takes the workspace, which is free again
        d.followup(done["task_id"], "more")
        assert d._tasks[done["task_id"]]["queued"] is True  # stays in the workspace, so it waits
        assert d._tasks[done["task_id"]]["run_dir_kind"] == "workspace"
    finally:
        _stop(d, tmp_path / "go")


def test_parked_worktree_task_frees_its_place_under_the_limit(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    ws = _repo(tmp_path)
    # The agent in the worktree asks a question (Maestro's question marker; see
    # maestro/adapters/base.py for the exact output that parks a task) and exits.
    _fake_bin(binpath, "codex", f'cat > /dev/null\ncase "$PWD" in *worktrees*) echo "QUESTION: which db?"; exit 0;; esac\nwhile [ ! -f "{gate}" ]; do sleep 0.05; done\nexit 0')
    d = _daemon(tmp_path, monkeypatch, "[defaults]\nmax_parallel = 2\n")
    try:
        d.delegate(_doc(), ws)                       # runs in the workspace until the gate
        parked = d.delegate(_doc(), ws)              # worktree; parks on its question
        d.wait(parked["task_id"], timeout=30)        # returns at input-required
        third = d.delegate(_doc(), ws)
        assert third["queued"] is False              # the parked task does not count
    finally:
        _stop(d, gate)
```

Before writing the parked test, open `maestro/adapters/base.py` and find how an agent's output becomes `result.question`; use that exact marker in the fake agent instead of `QUESTION:` if it differs.

- [ ] **Step 2: Run to see them fail**

Run: `python -m pytest -q tests/test_parallel_tasks.py`
Expected: FAIL — `KeyError: 'run_dir'` in the first test, and the second delegate returns `queued: True`.

- [ ] **Step 3: Implement the scheduling**

1. `import` `from . import worktrees` at the top of `daemon.py`.
2. `_make_record`: add `"run_dir": None, "run_dir_kind": None`.
3. Add `_max_parallel`, `_running_count`, `_counts_self`, `_worktree_allowed`, `_claim_turn` (code above).
4. Add `_queue_reason`:

```python
    def _queue_reason(self, task_id: str) -> str:
        record = self._tasks[task_id]
        key = record["workspace"]
        limit = self._max_parallel()
        if self._running_count(key) >= limit:
            return f"the workspace is at its limit of {limit} running tasks; this task starts when one of them finishes or stops to ask a question"
        if record.get("run_dir_kind") == "workspace":
            return "this task's work is in the workspace, and another task is using the workspace; it starts when that task finishes"
        return "this task works in place (commit_policy no-commit), and another task is using the workspace; it starts when that task finishes"
```

5. `delegate`: replace the `_active` block with:

```python
        with self._lock:
            kind = self._claim_turn(task_id)
            queued = kind is None
            if queued:
                self._queue.append(task_id)
            record["queued"] = queued
            reason = self._queue_reason(task_id) if queued else None
        self._persist(task_id)
        run_dir = record.get("run_dir") or key
        if queued:
            return {"task_id": task_id, "queued": True, "reason": reason, "run_dir": run_dir, "state": STATE_SUBMITTED, "ts": utcnow_iso()}
        state = self._launch(task_id, doc, ws, turn_flag, routing_resolved)
        return {"task_id": task_id, "queued": False, "run_dir": run_dir, "state": state, "ts": utcnow_iso()}
```

   Note `run_dir` for a queued task that has no kind yet is the workspace path; the real run directory is decided when it starts.
6. `_acquire_or_queue(task_id)`: replace the body's slot check with `kind = self._claim_turn(task_id)`; if `kind is None`, queue with `continuation = True` as today and return False; else `record["queued"] = False` and return True.
7. `_start_queued`: replace `if self._active.get(key) not in (None, task_id): return` / `self._active[key] = task_id` with `if self._claim_turn(task_id) is None: return` (it stays queued). `_release` already removed it from `_queue`; put it back at the front in that case: `self._queue.insert(0, task_id); record["queued"] = True; return`.
8. `_release(task_id)`: free `_active[key]` only when this task owns it (unchanged), then call `self._pump_queue()` instead of the inline loop. Move the loop into `_pump_queue`:

```python
    def _pump_queue(self) -> None:
        """Start every queued task that can now claim a turn, in FIFO order."""
        started: list[str] = []
        with self._lock:
            for queued_id in list(self._queue):
                qrec = self._tasks.get(queued_id)
                if qrec is None:
                    self._queue.remove(queued_id)
                    continue
                qrec["queued"] = False  # so _claim_turn does not count it as queued
                if self._claim_turn(queued_id) is None:
                    qrec["queued"] = True
                    continue
                self._queue.remove(queued_id)
                self._turn_starting.add(queued_id)  # counts as running until its thread sets working
                started.append(queued_id)
        for queued_id in started:
            self._start_queued(queued_id)
```

   and make `_start_queued` accept a task that already claimed its turn (skip the claim when `queued_id` came from `_pump_queue`: pass `claimed=True`).
9. `_set_state`: after the state is written, when the new state is `STATE_INPUT_REQUIRED` and the record's `run_dir_kind == "worktree"`, call `self._pump_queue()` so a parked worktree task frees its place. A parked workspace task keeps `_active` (it still owns the workspace), but it no longer counts as running, so `_pump_queue` runs for it too: call it for every new input-required state.
10. `cancel`: it already calls `_release`; confirm a canceled queued task is removed from `_queue` (existing behaviour) and nothing else changes.

- [ ] **Step 4: Run the new and existing queue tests**

Run: `python -m pytest -q -n auto tests/test_parallel_tasks.py tests/test_daemon.py tests/test_task_state.py tests/test_lifecycle*.py`
Expected: the new tests PASS. Existing tests that delegate two `commit_policy="branch"` tasks to one workspace and assert the second is queued now fail: add `(home / "config.toml").write_text("[defaults]\nmax_parallel = 1\n")` before their daemon starts, or give them `commit_policy="no-commit"`, whichever keeps the test's intent. List each changed test in the commit message.

- [ ] **Step 5: Commit**

```bash
git add maestro/daemon.py tests/
git commit -m "Run a task in its own worktree when its workspace is busy"
```

---

### Task 4: Run every step of a turn in the run directory

**Files:**
- Modify: `maestro/daemon.py` — `_run_turn` (~1045), `_prepare_branch` (~1658), `_post_complete`, `_gate_cycle`, `_gate_turn`, `_fix_turn` (context root only), `_refresh_knowledge` (~1840), `_durable_record` key list (~1888), `_persist`
- Modify: `maestro/worker.py` `_python_executable` (~66)
- Modify: `maestro/knowledge.py` `project_knowledge` caller input
- Test: `tests/test_parallel_tasks.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: `worktrees.head_commit`, `worktrees.add_worktree`, `worktrees.ensure_worktree`, `worktrees.main_repo_root` (Task 1); `record["run_dir"]`, `record["run_dir_kind"]` (Task 3).
- Produces:
  - `MaestroDaemon._prepare_run_dir(task_id, doc, workspace, recorded) -> tuple[Path, str | None]` returns `(run_dir, branch)`.
  - `MaestroDaemon._context_root(task_id: str, fallback: Path) -> Path` returns the task's workspace (for context files and skills).
  - Claims `task_run_dir`, `task_run_dir_kind` written when the run directory is first prepared; `task_run_dir_removed` cleared when a worktree is created again.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_parallel_tasks.py`)

```python
def test_worktree_task_changes_stay_in_its_worktree(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        first = d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        gate.touch()
        d.wait(first["task_id"], timeout=60)
        final = d.wait(second["task_id"], timeout=60)
        wt = Path(second["run_dir"])
        assert (ws / "ran-here.txt").read_text().strip() == str(ws)
        assert (wt / "ran-here.txt").read_text().strip() == str(wt)
        assert final["metadata"]["run_dir"] == str(wt)
        claims = d.maestro._claims(second["task_id"])
        assert claims["task_run_dir"] == str(wt) and claims["task_run_dir_kind"] == "worktree"
        branch = subprocess.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], text=True, capture_output=True).stdout.strip()
        assert branch == f"maestro/{second['task_id']}"
    finally:
        _stop(d, gate)


def test_followup_in_a_deleted_worktree_recreates_it(tmp_path, monkeypatch, binpath):
    import shutil

    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        gate.touch()
        d.wait(second["task_id"], timeout=60)
        shutil.rmtree(second["run_dir"])
        sub = d.bus.subscribe("output")
        try:
            d.followup(second["task_id"], "again")
            note = sub.wait(
                predicate=lambda e: e.task_id == second["task_id"] and "was created again" in (e.data.get("line") or ""),
                timeout=30,
            )
        finally:
            sub.close()
        final = d.wait(second["task_id"], timeout=60)
        assert note is not None
        assert final["status"]["state"] == "completed"
        assert Path(second["run_dir"]).is_dir()
    finally:
        _stop(d, gate)


def test_rename_branch_of_a_worktree_task_keeps_its_worktree(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        gate.touch()
        d.wait(second["task_id"], timeout=60)
        d.rename_branch(second["task_id"], "feat/renamed")
        d.followup(second["task_id"], "again")
        d.wait(second["task_id"], timeout=60)
        head = subprocess.run(["git", "-C", second["run_dir"], "rev-parse", "--abbrev-ref", "HEAD"], text=True, capture_output=True).stdout.strip()
        assert head == "feat/renamed"
    finally:
        _stop(d, gate)


def test_workspace_without_commit_fails_the_worktree_task(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = tmp_path / "empty"
    ws.mkdir()
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True)
    d = _daemon(tmp_path, monkeypatch)
    try:
        d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        final = d.wait(second["task_id"], timeout=60)
        assert final["status"]["state"] == "failed"
        assert "has no commit yet" in final["metadata"]["error"]
    finally:
        _stop(d, gate)
```

In `tests/test_worker.py`:

```python
def test_python_executable_falls_back_to_the_main_repository_venv(tmp_path, monkeypatch):
    from maestro import worker, worktrees

    monkeypatch.delenv("MAESTRO_PYTHON", raising=False)
    main = tmp_path / "main"
    venv_python = main / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_python.chmod(0o755)
    run_dir = tmp_path / "wt"
    run_dir.mkdir()
    monkeypatch.setattr(worktrees, "main_repo_root", lambda path: main)
    assert worker._python_executable(run_dir) == str(venv_python)
```


- [ ] **Step 2: Run to see them fail**

Run: `python -m pytest -q tests/test_parallel_tasks.py tests/test_worker.py -k "worktree or main_repository"`
Expected: FAIL — the second agent writes `ran-here.txt` in the workspace, `run_dir` missing from metadata.

- [ ] **Step 3: Implement**

1. `_prepare_run_dir` in `daemon.py`, called at the top of `_run_turn` in place of `_prepare_branch`:

```python
    def _prepare_run_dir(self, task_id: str, doc: HandoffDoc, workspace: Path, recorded: str | None) -> tuple[Path, str | None]:
        """Get the task's run directory ready for this turn and return it with the branch.

        A task in the workspace checks out its branch there, as before. A task
        in a worktree gets the worktree created on its first turn, and created
        again from its branch when it was removed."""
        record = self._tasks.get(task_id) or {}
        if record.get("run_dir_kind") != "worktree":
            branch = self._prepare_branch(workspace, task_id, doc.commit_policy, requested=doc.branch, recorded=recorded)
            self._record_run_dir(task_id, workspace, "workspace")
            return workspace, branch
        run_dir = Path(record["run_dir"])
        if recorded:
            branch = recorded if branch_exists(workspace, recorded) else self._renamed_task_branch(workspace, task_id, recorded)
            if worktrees.ensure_worktree(workspace, run_dir, branch):
                self.bus.publish(TaskEvent(task_id=task_id, type="output", data={"agent": "maestro", "line": f"[maestro] the worktree {run_dir} was missing and was created again from branch {branch}; only committed work is in it"}))
            else:
                checked = subprocess.run(["git", "-C", str(run_dir), "checkout", branch], text=True, capture_output=True)
                if checked.returncode != 0:
                    raise RuntimeError(f"could not check out the task branch {branch!r} in {run_dir}: {(checked.stderr or checked.stdout).strip()}")
        else:
            branch = doc.branch or f"maestro/{task_id}"
            worktrees.add_worktree(workspace, run_dir, branch, new_branch=True, start=worktrees.head_commit(workspace))
        self._record_run_dir(task_id, run_dir, "worktree")
        return run_dir, branch

    def _record_run_dir(self, task_id: str, run_dir: Path, kind: str) -> None:
        record = self._tasks.get(task_id)
        if record is not None:
            record["run_dir"], record["run_dir_kind"] = str(run_dir), kind
        self.maestro._write_claim(task_id, "task_run_dir", str(run_dir))
        self.maestro._write_claim(task_id, "task_run_dir_kind", kind)
        self.maestro._write_claim(task_id, "task_run_dir_removed", "")

    def _context_root(self, task_id: str, fallback: Path) -> Path:
        """Context files and skills resolve against the caller's workspace:
        files that are not in git exist only there."""
        record = self._tasks.get(task_id) or {}
        return Path(record["workspace"]) if record.get("workspace") else fallback
```

   Check how an empty claim value is treated by `_write_claim` / `_claims`; if an empty string cannot be written, write `"false"` and treat anything but `"true"` as not removed.
2. In `_run_turn`: `run_dir, branch = self._prepare_run_dir(task_id, doc, workspace, recorded)`; keep the existing branch-event code; then use `run_dir` for `_record_turn_baseline`, `build_prompt`, `adapter.run`, `_post_complete`. Use `self._context_root(task_id, workspace)` for `_rendered_context`.
3. `_post_complete`, `_gate_cycle`, `_gate_turn`, `_fix_turn` already receive the directory as their `workspace` argument and pass it on; they now receive `run_dir`. Change only their `_rendered_context(...)` calls to `self._context_root(task_id, workspace)`.
4. `_refresh_knowledge`: pass `Path(claims.get("task_run_dir") or workspace_raw)` as `workspace=` to `project_knowledge`.
5. `_durable_record` key list (~line 1888): add `"run_dir"` and `"run_dir_kind"` so a follow-up after a daemon restart stays in its worktree.
6. `worker._python_executable(root)`: after the `.venv`/`venv` loop over `root`, repeat the loop over `worktrees.main_repo_root(root)` when it is not None and differs from `root`. Update the docstring order: `MAESTRO_PYTHON`, the run directory's virtual environment, the main repository's virtual environment, Maestro's own interpreter. Also fix `docs/design-parallel-tasks.md` section 4, which lists `MAESTRO_PYTHON` last; the real order puts it first.

- [ ] **Step 4: Run to see them pass**

Run: `python -m pytest -q -n auto tests/test_parallel_tasks.py tests/test_worker.py tests/test_daemon.py tests/test_branches.py tests/test_knowledge*.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/ tests/ docs/design-parallel-tasks.md
git commit -m "Run the agent, verification and gates in the task's run directory"
```

---

### Task 5: Show `run_dir` everywhere a task is shown

**Files:**
- Modify: `maestro/daemon.py` `status_a2a` (~2216), `maestro/core.py` `Maestro.status` (~701), `maestro/cli.py` audit (~341) and delegate output (~299-310), `maestro/mcp_server.py` delegate message (~101), `maestro/a2a.py` delegate/message results (~186), `maestro/receipt.py` (~207), `maestro/tui.py` (`normalize`, `render_frame` detail), `web/src/lib/events.js`, `web/src/components/DetailPane.jsx`, `maestro/web_dist/` (rebuilt)
- Test: `tests/test_core.py`, `tests/test_cli.py`, `tests/test_tui.py`, `tests/test_receipt.py`

**Interfaces:**
- Consumes: claims `task_run_dir`, `task_run_dir_kind`, `task_run_dir_removed` (Task 4); record `run_dir` (Task 3).
- Produces: `run_dir` in A2A task metadata, `Maestro.status()` (`run_dir`, `run_dir_kind`, and `run_dir_removed: true` only when removed), receipt JSON, CLI audit JSON, TUI flat task.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_core.py
def test_status_reports_run_dir_and_defaults_to_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path/'home'))
    m = Maestro(tmp_path)
    try:
        tid = _seed_task(m)
        s = m.status(tid)
        assert s['run_dir'] == s['workspace'] and s['run_dir_kind'] == 'workspace'  # tasks from before this change
        m._write_claim(tid, 'task_run_dir', '/home/.maestro/worktrees/x')
        m._write_claim(tid, 'task_run_dir_kind', 'worktree')
        m._write_claim(tid, 'task_run_dir_removed', 'true')
        s = m.status(tid)
        assert s['run_dir'] == '/home/.maestro/worktrees/x' and s['run_dir_kind'] == 'worktree' and s['run_dir_removed'] is True
    finally:
        m.close()
```

```python
# tests/test_tui.py
def test_detail_shows_run_dir_only_when_it_differs_from_workspace():
    base = {"task_id": "t", "title": "T", "state": "working", "workspace": "/repo"}
    same = _ANSI_RE.sub("", tui.render_frame([dict(base, run_dir="/repo")], 0, live=True))
    other = _ANSI_RE.sub("", tui.render_frame([dict(base, run_dir="/h/worktrees/t")], 0, live=True))
    assert "run dir:" not in same
    assert "run dir: /h/worktrees/t" in other
    assert tui.normalize({"id": "t", "metadata": {"run_dir": "/x"}})["run_dir"] == "/x"
```

```python
# tests/test_cli.py — delegate output names where the task runs and why it waits
def test_delegate_prints_queue_reason(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    captured = _delegate_capture(monkeypatch)  # existing helper
    # Make the fake daemon answer "queued" with a reason: adjust _delegate_capture's
    # returned result (read the helper) so result == {"task": {"id": None, ...}, "queued": True,
    # "reason": "the workspace is at its limit of 4 running tasks; ..."}.
    rc = cli.main(["delegate", "--title", "T", "--request", "R", "--target", "codex"])
    assert rc == 0
    assert "limit of 4 running tasks" in capsys.readouterr().out
```

Read `_delegate_capture` in `tests/test_cli.py` and the delegate path in `cli.py` (`_cmd_delegate`, ~line 280-311) before writing this test: the CLI uses A2A `message/send`, whose queued result is built in `a2a.py:186`. Carry the daemon's `reason` into that result's `metadata` and print it in `cli.py` instead of the fixed text "workspace already has an active task; this handoff is next in line". Do the same in `mcp_server.py:101`.

- [ ] **Step 2: Run to see them fail**

Run: `python -m pytest -q tests/test_core.py tests/test_tui.py tests/test_cli.py -k "run_dir or queue_reason"`
Expected: FAIL (`KeyError: 'run_dir'`, missing text).

- [ ] **Step 3: Implement**

1. `status_a2a`: live record → `record.get("run_dir") or workspace`; durable → `claims.get("task_run_dir") or workspace`. Add `"run_dir"` to `metadata`.
2. `Maestro.status`: after `workspace` is known:

```python
        result["run_dir"]=claims.get("task_run_dir") or result["workspace"]
        result["run_dir_kind"]=claims.get("task_run_dir_kind") or "workspace"
        if claims.get("task_run_dir_removed")=="true": result["run_dir_removed"]=True
```

3. `cli.py` audit JSON: add `"run_dir": claims.get("task_run_dir") or claims.get("task_workspace")`.
4. `receipt.py`: add `run_dir` next to `workspace` in the receipt dict and one text line `run dir:` when it differs from the workspace.
5. `tui.normalize`: `"run_dir": meta.get("run_dir")`. In `render_frame`'s detail loop add `("run dir", sel.get("run_dir") if sel.get("run_dir") not in (None, sel.get("workspace")) else None)` after `workspace`.
6. Web console: `web/src/lib/events.js` add `run_dir: meta.run_dir`; `DetailPane.jsx` add `{task.run_dir && task.run_dir !== task.workspace && <Meta label="run dir" value={task.run_dir} />}` after the workspace line. Rebuild: `cd web && npm ci && node build.mjs`, and commit the changed `maestro/web_dist/` files.
7. Queue reason in `a2a.py` (queued `task_obj.metadata.reason`), `cli.py` delegate print, `mcp_server.py` delegate text.

- [ ] **Step 4: Run to see them pass**

Run: `python -m pytest -q -n auto tests/test_core.py tests/test_tui.py tests/test_cli.py tests/test_receipt.py tests/test_a2a.py tests/test_mcp*.py`
Expected: PASS. Then run `git diff --exit-code -- maestro/web_dist/` after a second `node build.mjs` to confirm the bundle is stable.

- [ ] **Step 5: Commit**

```bash
git add maestro/ web/src tests/
git commit -m "Show each task's run directory in status, receipts, the dashboard and the console"
```

---

### Task 6: Removing worktrees — `task cleanup` and `gc`

**Files:**
- Modify: `maestro/daemon.py` (new `cleanup_worktree`), `maestro/a2a.py` (`tasks/cleanup`), `maestro/mcp_server.py` (`cleanup_task_worktree`), `maestro/cli.py` (`task cleanup` parser, `_cmd_task_cleanup`, `_normalize_argv` known commands, `_cmd_gc` ~631)
- Test: `tests/test_parallel_tasks.py`, `tests/test_cli.py`, `tests/test_a2a.py`, the existing gc test file (find with `grep -ln "_cmd_gc\|\"gc\"" tests/`)

**Interfaces:**
- Consumes: `worktrees.dirty_files`, `worktrees.remove_worktree` (Task 1); claims from Task 4.
- Produces:
  - `MaestroDaemon.cleanup_worktree(task_id: str, force: bool = False) -> dict` returning `{"task_id", "run_dir", "removed": bool, "reason": str}`; raises `ValueError` for a running task or uncommitted changes without force (message lists the files).
  - A2A method `tasks/cleanup` with params `{"id", "force"}` → `{"cleanup": <dict>}`.
  - MCP tool `cleanup_task_worktree(workspace: str, task_id: str, force: bool = False) -> str`.
  - CLI `maestro task cleanup <task> [--force]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_parallel_tasks.py
def test_cleanup_rules(tmp_path, monkeypatch, binpath):
    gate = tmp_path / "go"
    _slow_agent(binpath, gate)
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        first = d.delegate(_doc(), ws)
        second = d.delegate(_doc(), ws)
        with pytest.raises(ValueError, match="is running"):
            d.cleanup_worktree(second["task_id"])
        gate.touch()
        d.wait(first["task_id"], timeout=60)
        d.wait(second["task_id"], timeout=60)
        # The fake agent left ran-here.txt uncommitted in the worktree.
        with pytest.raises(ValueError, match="uncommitted changes: ran-here.txt"):
            d.cleanup_worktree(second["task_id"])
        result = d.cleanup_worktree(second["task_id"], force=True)
        assert result["removed"] is True and not Path(second["run_dir"]).exists()
        assert d.maestro._claims(second["task_id"])["task_run_dir_removed"] == "true"
        in_place = d.cleanup_worktree(first["task_id"])
        assert in_place["removed"] is False and "ran in the workspace" in in_place["reason"]
        assert (ws / "ran-here.txt").exists()  # the user's checkout is never touched
    finally:
        _stop(d, gate)
```

```python
# tests/test_cli.py
def test_task_cleanup_posts_cleanup(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    captured = _rpc_capture(monkeypatch, {"cleanup": {"task_id": "t", "removed": True, "run_dir": "/w", "reason": "removed"}})
    assert cli.main(["task", "cleanup", "t", "--force"]) == 0
    assert captured["method"] == "tasks/cleanup"
    assert captured["payload"] == {"id": "t", "force": True}
    assert json.loads(capsys.readouterr().out)["cleanup"]["removed"] is True
```

For `gc`, add a test next to the existing gc tests: seed two old completed tasks with `task_run_dir_kind=worktree` and real worktrees (one clean, one with an untracked file); run `cli.main(["gc", "--days", "0"])`; assert the clean worktree and its record are gone, and the dirty worktree, its record and a line naming its task in the output remain.

- [ ] **Step 2: Run to see them fail**

Run: `python -m pytest -q tests/test_parallel_tasks.py tests/test_cli.py -k cleanup`
Expected: FAIL (`AttributeError: cleanup_worktree`, argparse error).

- [ ] **Step 3: Implement**

```python
    def cleanup_worktree(self, task_id: str, force: bool = False) -> dict[str, Any]:
        """Remove a task's worktree. Never touches the workspace, never removes
        the branch, and refuses to drop uncommitted work unless ``force``."""
        with self._lock:
            record = self._tasks.get(task_id) or self._durable_record(task_id)
            if record is None:
                raise KeyError(f"Unknown task reference {task_id!r}")
            if record.get("state") in (STATE_SUBMITTED, STATE_WORKING) or task_id in self._turn_starting:
                raise ValueError(f"Task {task_id} is running; wait for it to finish or cancel it first")
            claims = self.maestro._claims(task_id)
            kind = record.get("run_dir_kind") or claims.get("task_run_dir_kind") or "workspace"
            run_dir = Path(record.get("run_dir") or claims.get("task_run_dir") or record["workspace"])
            if kind != "worktree":
                return {"task_id": task_id, "run_dir": str(run_dir), "removed": False, "reason": "this task ran in the workspace; Maestro never removes or changes your checkout"}
            if not run_dir.is_dir():
                return {"task_id": task_id, "run_dir": str(run_dir), "removed": False, "reason": "the worktree is already gone"}
            dirty = worktrees.dirty_files(run_dir)
            if dirty and not force:
                raise ValueError(f"The worktree {run_dir} has uncommitted changes: {', '.join(dirty)}. Commit them, or pass --force to remove the worktree anyway")
            worktrees.remove_worktree(Path(record["workspace"]), run_dir, force=force)
            self.maestro._write_claim(task_id, "task_run_dir_removed", "true")
            return {"task_id": task_id, "run_dir": str(run_dir), "removed": True, "reason": "removed; the branch is kept"}
```

A2A `_cleanup` mirrors `_rename_branch` in `a2a.py` (id required, `force` bool default False; `KeyError` → `ERR_TASK_NOT_FOUND`, `ValueError`/`RuntimeError` → `ERR_INVALID_PARAMS`). MCP tool mirrors `rename_task_branch` in `mcp_server.py`. CLI: `task_cleanup = task_sub.add_parser("cleanup", help="Remove a task's worktree (never the branch, never your checkout); refuses when it has uncommitted changes unless --force")`, args `task_id`, `--force`; `_cmd_task_cleanup` posts `tasks/cleanup` and prints the JSON result; add `"cleanup"` to `known_task_cmds`.

`_cmd_gc`: before deleting a task, read its claims; if `task_run_dir_kind == "worktree"` and the directory exists: if `worktrees.dirty_files(path)` is not empty, skip the task and print `kept <task_id>: its worktree <path> has uncommitted changes`; otherwise `worktrees.remove_worktree(Path(claims["task_workspace"]), path, force=False)` and continue with the normal deletion.

- [ ] **Step 4: Run to see them pass**

Run: `python -m pytest -q -n auto tests/test_parallel_tasks.py tests/test_cli.py tests/test_a2a.py tests/test_mcp*.py` plus the gc test file.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add maestro/ tests/
git commit -m "Add task cleanup for worktrees, and make gc keep worktrees with uncommitted work"
```

---

### Task 7: End-to-end check with three parallel tasks

**Files:**
- Test: `tests/test_parallel_tasks.py`

- [ ] **Step 1: Write the test**

```python
def test_three_tasks_run_at_once_each_in_its_own_directory(tmp_path, monkeypatch, binpath):
    marker_dir = tmp_path / "running"
    marker_dir.mkdir()
    gate = tmp_path / "go"
    # Each agent records that it is running, then waits for the gate. If the
    # tasks ran one after another, the test would time out waiting for 3 markers.
    _fake_bin(binpath, "codex", f'cat > /dev/null\ntouch "{marker_dir}/$$"\nwhile [ ! -f "{gate}" ]; do sleep 0.05; done\necho "$PWD" > ran-here.txt\nexit 0')
    ws = _repo(tmp_path)
    d = _daemon(tmp_path, monkeypatch)
    try:
        started = [d.delegate(_doc(title=f"t{i}"), ws) for i in range(3)]
        deadline = time.monotonic() + 30
        while len(list(marker_dir.iterdir())) < 3:
            assert time.monotonic() < deadline, "the three tasks did not run at the same time"
            time.sleep(0.05)
        gate.touch()
        finals = [d.wait(s["task_id"], timeout=60) for s in started]
        assert [f["status"]["state"] for f in finals] == ["completed"] * 3
        dirs = [Path(s["run_dir"]) for s in started]
        assert dirs[0] == ws and len(set(dirs)) == 3
        for path in dirs:
            assert (path / "ran-here.txt").read_text().strip() == str(path)
        branches = {subprocess.run(["git", "-C", str(p), "rev-parse", "--abbrev-ref", "HEAD"], text=True, capture_output=True).stdout.strip() for p in dirs}
        assert len(branches) == 3
    finally:
        _stop(d, gate)
```

- [ ] **Step 2: Run it**

Run: `python -m pytest -q tests/test_parallel_tasks.py -k three_tasks`
Expected: PASS (all earlier tasks are in place).

- [ ] **Step 3: Commit**

```bash
git add tests/test_parallel_tasks.py
git commit -m "Test that three tasks for one workspace run at the same time"
```

---

### Task 8: Documentation and the full gate

**Files:**
- Modify: `docs/design-parallel-tasks.md` (status line: "implemented in 0.15.0"), `README.md` (lines ~209, ~392 "One active task per workspace", the configuration example, the MCP tools list ~920), `docs/usage/explanation/concepts.md` (~48), `docs/usage/reference/cli.md` (`task cleanup` row and synopsis, `gc` behaviour), `docs/usage/reference/configuration.md` (`[defaults] max_parallel`), `docs/usage/reference/mcp-tools.md` (`cleanup_task_worktree`), `docs/usage/how-to/manage-in-flight-tasks.md` (where a task's work is; cleanup), `CHANGELOG.md` (Unreleased → Added/Changed), `tests/test_docs_usage.py` (`"task cleanup"` in `CLI_COMMANDS`, `"cleanup_task_worktree"` in the MCP tools tuple)

- [ ] **Step 1: Update `tests/test_docs_usage.py`** with the two new names and run `python -m pytest -q tests/test_docs_usage.py` — Expected: FAIL (docs do not mention them yet).
- [ ] **Step 2: Write the docs.** Every sentence plain and complete. The CHANGELOG entry says: what changed (a task that would wait for a busy workspace now runs in its own worktree under `~/.maestro/worktrees/<task-id>`, up to `[defaults] max_parallel`, default 4); what the user must know (that task's changes are in its worktree, not in the checkout; `run_dir` in status shows where; `max_parallel = 1` restores the old behaviour); how to remove worktrees (`task cleanup`, `gc`).
- [ ] **Step 3: Run the docs tests** — Expected: PASS.
- [ ] **Step 4: Run the full gate**

Run: `PYTHONPATH=$PWD python -m pytest -n auto -q --cov --cov-fail-under=100`
Expected: all pass, `Required test coverage of 100% reached`. (`PYTHONPATH` is only needed on machines where the package is not installed; the daemon tests start `python -m maestro.daemon_main` in a subprocess.)

Then `scripts/smoke-fake-agent.sh` and `scripts/validate-package.sh`. Expected: both exit 0.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/ CHANGELOG.md tests/test_docs_usage.py
git commit -m "Document parallel tasks, run_dir, max_parallel and task cleanup"
```
