from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core import Maestro

mcp = FastMCP("maestro")


def _instance(workspace: str) -> Maestro:
    # The MCP server process may outlive the Claude session/worktree that launched it.
    # Never use its cwd as the project identity; Claude passes the active workspace.
    return Maestro(Maestro.git_root(workspace))


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
