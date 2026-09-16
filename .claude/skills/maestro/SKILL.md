# Maestro — Claude-supervised implementation handoff

Use Maestro automatically for implementation work.

## Operating model

Claude Code is the supervisor and design/review agent.
Codex is the implementation agent.
Memvara is the durable shared memory layer.
Verification is a deterministic orchestration phase, not a second agent identity.

## Required behavior

When the user asks to build/implement/change a non-trivial feature:

1. Inspect the repository and relevant Memvara history.
2. Produce an internal design/specification with requirements, architecture, affected files, tests, constraints, and rejected alternatives.
3. Use Maestro's configured Codex model and reasoning effort by default. Override them only when task complexity warrants it.
4. Once the design is sufficiently complete, call the `maestro` MCP tool `delegate_to_codex` automatically. Do not ask the user to run `maestro`.
5. Maestro persists the design in self-hosted Memvara and launches Codex in the background. The delegation call returns immediately.
6. Poll `task_status` until the task reaches `REVIEWING`, `FAILED`, or `COMPLETE`. Read the implementation artifact and verification evidence from the task state.
7. Review the resulting diff yourself as Claude.
8. If fixes are required, call the Maestro `review_task` tool with the review; Maestro will automatically send rejected findings to Codex, re-run verification, and return the new evidence.
9. Only present the completed implementation to the user after your review is satisfied.

The user experience should be: **message Claude once, Claude coordinates everything behind the scenes**.

## Do not

- Ask the user to run `maestro` for normal feature work.
- Paste large Claude transcripts into Codex.
- Treat Codex's exit code as proof that the work is correct.
- Give Claude and Codex different Memvara scopes for the same project.
- bypass the design → implementation → verification → review lifecycle.


## Example

```text
1. Run: git rev-parse --show-toplevel
2. Call delegate_to_codex(workspace=<that path>, ...). Optional `model` and `effort` override the configured Codex defaults.
3. Poll task_status(workspace=<same path>, task_id=<id>)
4. Review the implementation and call review_task(workspace=<same path>, ...)
```

Maestro owns task coordination only. It must not commit changes or install project dependencies. Codex works in the explicitly supplied workspace; verification uses the repository's existing `make check` when available, otherwise the project's existing pytest configuration. Environment/setup failures are evidence for Claude to interpret, not a reason to silently modify the environment.
