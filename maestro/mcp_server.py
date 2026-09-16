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
def delegate_to_codex(workspace: str, title: str, request: str, design: str, model: str | None = None, effort: str | None = None) -> str:
    """Persist Claude's approved design and asynchronously start Codex.

    Optional model/effort override the Maestro Codex defaults for this task. Reasoning effort accepts low, medium, high, or xhigh.
    This is the normal Claude Code entry point. It returns immediately with a human-friendly
    task number; Claude can use task_status later rather than blocking on implementation.
    """
    m = _instance(workspace)
    try:
        handoff = m.create_handoff(title, request, design, model=model, effort=effort)
        launch = m.implement_async(handoff["task_id"])
        return json.dumps({"handoff": handoff, "launch": launch}, indent=2)
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
        return json.dumps(m.list_tasks(), indent=2)
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


if __name__ == "__main__":
    main()
