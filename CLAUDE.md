# Maestro supervisor rules

Maestro is the implementation backend for Claude Code. For normal coding work, Claude is the supervisor/reviewer and Codex is the implementation agent.

## Default routing

- Implementation, tests, debugging, refactoring, documentation changes, and mechanical code changes: delegate to Maestro/Codex.
- Claude owns requirements, architecture decisions, compact handoff design, final diff review, and user communication.
- Do not use Claude subagents for implementation when Maestro/Codex is available.
- Do not duplicate Codex implementation or test work in Claude.
- Use `codex_followup` for additional implementation/debugging/test/refactor work. Use `review_task` for the final Claude review decision.

## Zero-discovery rule

Do not read Maestro source code, README, upgrade notes, or internal implementation docs during a normal project task. Do not search for how Maestro works before invoking it. The Maestro MCP tools and this file are the operational contract.

Only inspect Maestro internals when: (1) a Maestro MCP/CLI operation fails, (2) the user explicitly asks to modify Maestro, or (3) diagnosing a Maestro bug.

Do not ask the user to run the Maestro CLI for normal work. Claude should invoke the MCP tools directly.

## Delegation

For non-trivial implementation work:
1. Understand the request and inspect the target repository.
2. Create a compact handoff/design.
3. Call Maestro immediately.
4. Poll task status.
5. Review the resulting diff.
6. Send fixes to Codex through Maestro when needed.

Use the active worktree absolute path as the `workspace` argument on every Maestro MCP call. Resolve it from the target repository; never depend on the MCP server cwd.

## Codex defaults

Use the task/project Codex defaults unless the task clearly requires an override. Supported effort values: `low`, `medium`, `high`, `xhigh`, `max`.

Maestro/Codex must not commit changes or install dependencies. Claude owns final review and commits.
