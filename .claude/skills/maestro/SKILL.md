---
name: maestro
description: Route implementation work to Maestro/Codex without reading Maestro internals.
---

# Maestro

Use this skill as the operational contract for Maestro. Do not open Maestro source/docs unless diagnosing Maestro itself.

## Route implementation work

1. Identify the target repository/worktree.
2. Prepare a compact design/handoff; do not implement substantial code yourself.
3. Call `delegate` with the active absolute `workspace` — it blocks until the work completes, fails, or needs input.
4. Review the returned task result and the diff/verification evidence yourself.
5. Use `followup` for implementation/debugging/test/refactor fixes; use `task_wait` to follow up on earlier delegations.

## Do not

- Do not launch Claude subagents for implementation when Maestro/Codex is available.
- Do not reread the Maestro README/source before every delegation.
- Do not duplicate Codex's implementation, test, or debugging loop.
- Do not ask the user to invoke the Maestro CLI during normal work.

## Escalation

Inspect Maestro internals only when a Maestro operation fails or the user asks to change/debug Maestro.
