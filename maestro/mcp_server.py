from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core import Maestro
from .daemon import get_daemon
from .handoff import load_handoff_file

mcp = FastMCP("maestro")


def _instance(workspace: str) -> Maestro:
    # The MCP server process may outlive the Claude session/worktree that launched it.
    # Never use its cwd as the project identity; Claude passes the active workspace.
    return Maestro(Maestro.git_root(workspace))


def _delegate_timeout() -> float:
    try:
        return float(os.environ.get("MAESTRO_DELEGATE_TIMEOUT", "3600"))
    except ValueError:
        return 3600.0


@mcp.tool()
def delegate_to_codex(
    workspace: str,
    handoff_file: str,
) -> str:
    """Commit and launch a staged Claude→Codex handoff.

    Claude should write the design once to `.maestro/staged/` and pass only the small
    metadata-file path here. The staged artifact survives validation, MCP, or subprocess
    failures so the exact same handoff can be retried without regenerating the design.
    """
    m = _instance(workspace)
    try:
        handoff = m.create_handoff_from_file(handoff_file)
        launch = m.implement_async(handoff["task_id"])
        archived = m.finalize_staged_handoff(handoff_file, handoff["task_id"])
        return json.dumps({"handoff": handoff, "launch": launch, "handoff_artifact": archived}, indent=2)
    finally:
        m.close()


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
def codex_followup(workspace: str, task_id: str, instruction: str) -> str:
    """Delegate an implementation/debugging follow-up directly to Codex."""
    m = _instance(workspace)
    try:
        return json.dumps(m.codex_followup(task_id, instruction), indent=2)
    finally:
        m.close()


@mcp.tool()
def review_task(workspace: str, task_id: str, review: str, approved: bool) -> str:
    """Record Claude's review. Rejected reviews automatically launch a Codex fix pass."""
    m = _instance(workspace)
    try:
        state = m.review(task_id, review, approved)
        if not approved:
            fix = m.fix_async(task_id, review)
            return json.dumps({"review": state, "fix": fix}, indent=2)
        return json.dumps({"review": state}, indent=2)
    finally:
        m.close()


@mcp.tool()
def delegate(workspace: str, handoff_file: str) -> str:
    """Delegate a staged handoff to ANY registered agent and block until the work
    completes, fails, or needs input. No polling: this call resolves when done.

    The handoff file may be the 4-section Maestro document (JSON or TOML) or a
    legacy 0.8.x staged handoff. Returns the final A2A task object (state,
    artifacts, workspace/branch metadata)."""
    d = get_daemon()
    try:
        doc = load_handoff_file(handoff_file)
        started = d.delegate(doc, workspace)
    except (ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    if started.get("queued"):
        return json.dumps({"queued": True, "reason": "workspace already has an active task; this handoff is next in line", "ts": started["ts"]}, indent=2)
    final = d.wait(str(started["task_id"]), timeout=_delegate_timeout())
    timed_out = final["status"]["state"] not in {"completed", "failed", "canceled"} and final["status"]["state"] != "input-required"
    return json.dumps({"timed_out": bool(timed_out), **final}, indent=2)


@mcp.tool()
def task_wait(workspace: str, task_id: str, timeout: float = 120.0) -> str:
    """Block until a task reaches a new terminal state or needs input (or the
    timeout expires). Use this to follow up on earlier delegations — never poll."""
    d = get_daemon()
    try:
        final = d.wait(d.resolve(task_id), timeout=timeout)
    except KeyError as exc:
        return json.dumps({"error": exc.args[0]}, indent=2)
    return json.dumps(final, indent=2)


@mcp.tool()
def agents_list() -> str:
    """List every registered agent with its adapter kind, skills, defaults, and
    live availability (binary found? version?)."""
    d = get_daemon()
    out = []
    for spec in d.registry.list():
        status = d.registry.status(spec.name)
        out.append({**spec.to_dict(), "status": status})
    return json.dumps(out, indent=2)


@mcp.tool()
def cancel_task(workspace: str, task_id: str, reason: str = "") -> str:
    """Cancel a running (or queued-waiting) task. Partial work on the task branch
    is kept; the task is marked canceled with the reason."""
    d = get_daemon()
    try:
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
    d = get_daemon()
    try:
        result = d.answer_question(d.resolve(task_id), answer)
    except KeyError as exc:
        return json.dumps({"error": exc.args[0]}, indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
def followup(workspace: str, task_id: str, instruction: str) -> str:
    """Send a follow-up instruction to a finished task (completed/failed/canceled).
    The same agent resumes on the same task branch with the new instruction and
    its previous Q&A in context. Blocks until the follow-up turn finishes or
    needs input — no polling."""
    d = get_daemon()
    try:
        started = d.followup(d.resolve(task_id), instruction)
    except KeyError as exc:
        return json.dumps({"error": exc.args[0]}, indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    final = d.wait(str(started["task_id"]), timeout=_delegate_timeout())
    timed_out = final["status"]["state"] not in {"completed", "failed", "canceled"} and final["status"]["state"] != "input-required"
    return json.dumps({"timed_out": bool(timed_out), **final}, indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
