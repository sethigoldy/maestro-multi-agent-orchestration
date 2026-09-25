"""The Maestro MCP server: the tools a host agent (Claude Code, Codex, ...) calls.

The tools that start, wait for or change tasks go through ``get_daemon()``,
which returns a client that forwards each call over HTTP to the daemon that
owns the state directory. When no daemon owns it, the server starts a
detached background daemon (the one ``maestro daemon start`` starts, with this
server's environment), so the tasks keep running when this server exits. See
:func:`maestro.daemon.get_daemon`. Only when a background daemon cannot be
started does the daemon run inside this process; on exit the server stops
that one, so its marker and locks are released and no task is left
"working" with no process to finish it.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import daemon as _daemon
from .core import Maestro
from .daemon import get_daemon, shutdown_daemon
from .handoff import load_handoff_file, validate_handoff

mcp = FastMCP("maestro")


def _instance(workspace: str) -> Maestro:
    # The MCP server process may outlive the host agent's session/worktree that launched it.
    # Never use its cwd as the project identity; the host agent passes the active workspace.
    return Maestro(Maestro.git_root(workspace))


def _delegate_timeout() -> float:
    try:
        return float(os.environ.get("MAESTRO_DELEGATE_TIMEOUT", "3600"))
    except ValueError:
        return 3600.0


@mcp.tool()
def task_status(workspace: str, task_id: str) -> str:
    """Return current task state. Numeric task numbers such as '1' are accepted."""
    m = _instance(workspace)
    try:
        return json.dumps(m.status(task_id), indent=2)
    finally:
        m.close()


@mcp.tool()
def list_tasks(workspace: str) -> str:
    """List human-friendly tasks and their current phases."""
    m = _instance(workspace)
    try:
        return json.dumps(m.list_tasks(project_filter=str(m.project_root)), indent=2)
    finally:
        m.close()


@mcp.tool()
def delegate(workspace: str, handoff_file: str, branch: str = "") -> str:
    """Delegate a staged handoff to ANY registered agent and block until the work
    completes, fails, or needs input. No polling: this call resolves when done.

    The handoff file may be the 4-section Maestro document (JSON or TOML) or a
    legacy 0.8.x staged handoff. A [routing] mode name selects a work-mode preset
    from config that pins implementer/reviewer/verifier/fixer agents to the task's
    phases; explicit routing fields in the file win over the preset. Optional
    [[context]] entries (label + text or path, kind text/file/skill, optional
    phases) inject user-controlled context into the agent turns and are composed
    with standing [context] config entries at delegate time.

    Routing defaults: when the handoff names no target agent, Maestro resolves it
    from the project's .maestro/config.toml [defaults] table (agent, fallback,
    model, effort). If neither the handoff nor [defaults] names an agent, the
    task parks in state 'input-required' with a question listing every available
    agent — ask the user which agent and model to use, then call
    answer_task_question with e.g. 'codex' or 'agent=codex model=gpt-5.6-luna'.

    branch: name for the task's git branch (for example 'feat/login-form').
    It overrides [expectations] branch in the handoff file. When neither is
    set the branch is 'maestro/<task_id>'. The branch must not exist yet.

    Returns the final A2A task object (state, artifacts, workspace/branch
    metadata)."""
    try:
        d = get_daemon()
        doc = load_handoff_file(handoff_file)
        if branch.strip():
            doc.branch = branch
            doc = validate_handoff(doc)
        started = d.delegate(doc, workspace)
    except (ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    if started.get("queued"):
        # The task id lets the caller wait for the task with task_wait.
        return json.dumps({"queued": True, "task_id": started.get("task_id"), "reason": started.get("reason"), "ts": started["ts"]}, indent=2)
    try:
        final = d.wait(str(started["task_id"]), timeout=_delegate_timeout())
    except ValueError as exc:  # the daemon went away and no replacement could be reached
        return json.dumps({"error": str(exc), "task_id": started["task_id"]}, indent=2)
    timed_out = final["status"]["state"] not in {"completed", "failed", "canceled"} and final["status"]["state"] != "input-required"
    return json.dumps({"timed_out": bool(timed_out), **final}, indent=2)


@mcp.tool()
def task_wait(workspace: str, task_id: str, timeout: float = 120.0) -> str:
    """Block until a task reaches a new terminal state or needs input (or the
    timeout expires). Use this to follow up on earlier delegations — never poll."""
    try:
        d = get_daemon()
        final = d.wait(d.resolve(task_id), timeout=timeout)
    except KeyError as exc:
        return json.dumps({"error": exc.args[0]}, indent=2)
    except ValueError as exc:  # the daemon this server forwards to stopped answering
        return json.dumps({"error": str(exc)}, indent=2)
    return json.dumps(final, indent=2)


@mcp.tool()
def agents_list() -> str:
    """List every registered agent with its adapter kind, skills, defaults, and
    live availability (binary found? version?)."""
    try:
        return json.dumps(get_daemon().agents(), indent=2)
    except ValueError as exc:  # the daemon this server forwards to stopped answering
        return json.dumps({"error": str(exc)}, indent=2)


@mcp.tool()
def cancel_task(workspace: str, task_id: str, reason: str = "") -> str:
    """Cancel a running (or queued-waiting) task. Partial work on the task branch
    is kept; the task is marked canceled with the reason."""
    try:
        d = get_daemon()
        result = d.cancel(d.resolve(task_id), reason=reason)
    except KeyError as exc:
        return json.dumps({"error": exc.args[0]}, indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
def answer_task_question(workspace: str, task_id: str, answer: str) -> str:
    """Answer a question the agent asked mid-task (state 'input-required'). The
    agent resumes on its branch with the Q&A appended to its context."""
    try:
        d = get_daemon()
        result = d.answer_question(d.resolve(task_id), answer)
    except KeyError as exc:
        return json.dumps({"error": exc.args[0]}, indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
def rename_task_branch(workspace: str, task_id: str, branch: str) -> str:
    """Rename a finished (or parked) task's git branch and update the task's
    record, so list_tasks, task_status, receipts and later follow-ups all show
    the new name. Numeric task numbers such as '1' are accepted.

    If the branch was already renamed by hand with 'git branch -m', this only
    updates the record. If the task has no branch yet (for example its first
    turn could not create the branch it asked for), this changes the name its
    next turn creates, and the result has "pending": true. Only the local
    branch is renamed; a copy already pushed to a remote keeps its old name
    there."""
    try:
        d = get_daemon()
        result = d.rename_branch(d.resolve(task_id), branch)
    except KeyError as exc:
        return json.dumps({"error": exc.args[0]}, indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
def cleanup_task_worktree(workspace: str, task_id: str, force: bool = False) -> str:
    """Remove the git worktree of a task that ran next to a busy workspace.

    Refused while the task is running, and refused when the worktree has
    uncommitted changes unless force is true (the error lists the files). The
    task's branch and its commits are always kept. For a task that ran in the
    workspace itself this does nothing: your checkout is never removed."""
    try:
        d = get_daemon()
        result = d.cleanup_worktree(d.resolve(task_id), force=force)
    except KeyError as exc:
        return json.dumps({"error": exc.args[0]}, indent=2)
    except (ValueError, RuntimeError) as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
def followup(workspace: str, task_id: str, instruction: str, context_mode: str = "reuse", branch: str = "") -> str:
    """Send a follow-up instruction to a finished task (completed/failed/canceled).
    The same agent resumes on the same task branch with the new instruction —
    unless the task's handoff pins a fixer agent, in which case the follow-up
    runs under it.

    context_mode: 'reuse' (default) injects a compact task-knowledge snapshot
    (goal, current state, files changed, latest verification result and failures,
    known issues) so the agent continues without re-discovering the work; raw
    history stays in the durable record and is never replayed. 'fresh' skips the
    snapshot for a clean reasoning context (same task/workspace/branch).

    branch: optional new name for the task's branch. The branch is renamed
    first, exactly as rename_task_branch does, and the turn then runs on it. If
    the task has no branch yet (its first turn could not create the branch it
    asked for), this sets the name the turn creates.

    Blocks until the follow-up turn finishes or needs input — no polling."""
    try:
        d = get_daemon()
        started = d.followup(d.resolve(task_id), instruction, context_mode=context_mode, branch=branch.strip() or None)
    except KeyError as exc:
        return json.dumps({"error": exc.args[0]}, indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    try:
        final = d.wait(str(started["task_id"]), timeout=_delegate_timeout())
    except ValueError as exc:  # the daemon went away and no replacement could be reached
        return json.dumps({"error": str(exc), "task_id": started["task_id"]}, indent=2)
    timed_out = final["status"]["state"] not in {"completed", "failed", "canceled"} and final["status"]["state"] != "input-required"
    return json.dumps({"timed_out": bool(timed_out), **final}, indent=2)


def _stop_daemon_on_exit() -> None:
    """Stop this process's daemon when the server exits, including on SIGTERM.

    A normal exit runs the atexit hook. SIGTERM would end the process without
    it, so its handler stops the daemon first and then ends the process with
    the default SIGTERM action, as before.
    """
    atexit.register(shutdown_daemon)

    def _on_sigterm(signum: int, frame: object) -> None:
        shutdown_daemon()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, _on_sigterm)


def main() -> None:
    # Tasks must outlive this session: when no daemon owns the state
    # directory, start a background daemon instead of one inside this process.
    _daemon.BACKGROUND_OWNER = True
    _stop_daemon_on_exit()
    try:
        mcp.run()
    finally:
        shutdown_daemon()


if __name__ == "__main__":  # pragma: no cover
    main()
