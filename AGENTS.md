# Agent collaboration protocol

Codex owns implementation, tests, debugging, refactoring, and follow-up fixes. Claude owns requirements, architecture, final review, and user communication.

When a task has an approved Maestro handoff, use that handoff as the canonical design and inspect only the active target repository. Do not inspect Maestro internals unless Maestro itself is being changed or is failing.

Before implementation:
1. Read the approved handoff from the active Maestro task.
2. Inspect current git state.
3. Implement the approved design.
4. Run tests and record exact evidence.
5. Record important deviations in task artifacts.
