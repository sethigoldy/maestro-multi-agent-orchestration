# Maestro — Claude-supervised implementation handoff

Use Maestro automatically for implementation work.

## Operating model

Claude Code is the supervisor and design/review agent.
Codex is the implementation agent.
The local `.maestro/` filesystem is the durable shared state layer.
Verification is a deterministic orchestration phase, not a second agent identity.

## Required behavior

When the user asks to build/implement/change a non-trivial feature:

1. Inspect the repository and relevant `.maestro/` task/state history.
2. Produce an internal design/specification with requirements, architecture, affected files, tests, constraints, and rejected alternatives.
3. Use Maestro's configured Codex model and reasoning effort by default. Override them only when task complexity warrants it.
4. Once the design is sufficiently complete, write the design to a local file under `.maestro/staged/` and write a tiny JSON handoff descriptor beside it containing `title`, `request`, `design_file`, and optional `model`/`effort`. Then call the `maestro` MCP tool `delegate_to_codex(workspace=<active-worktree>, handoff_file=<descriptor-path>)`. Do not send the full design in MCP arguments.
5. Keep the staged descriptor and design file intact until Maestro confirms the task was committed and launched. If the tool call fails, retry with the same descriptor path instead of regenerating the design.
6. Poll `task_status` until the task reaches `REVIEWING`, `FAILED`, or `COMPLETE`. Read the implementation artifact and verification evidence from the task state.
7. Review the resulting diff yourself as Claude.
8. If fixes are required, call the Maestro `review_task` tool with the review; Maestro will automatically send rejected findings to Codex, re-run verification, and return the new evidence.
9. Only present the completed implementation to the user after your review is satisfied.

The user experience should be: **message Claude once, Claude coordinates everything behind the scenes**.

## Do not

- Ask the user to run `maestro` for normal feature work.
- Paste large Claude transcripts into Codex.
- Treat Codex's exit code as proof that the work is correct.
- Do not use different worktree/state paths for the same task.
- bypass the design → implementation → verification → review lifecycle.


## Example

```text
1. Run: git rev-parse --show-toplevel
2. Write `.maestro/staged/<handoff-id>.md` plus `.maestro/staged/<handoff-id>.json` (the JSON contains only title/request/design_file/model/effort), then call `delegate_to_codex(workspace=<that path>, handoff_file=<json path>)`.
3. Poll task_status(workspace=<same path>, task_id=<id>)
4. Review the implementation and call review_task(workspace=<same path>, ...)
```

Maestro owns task coordination only. It must not commit changes or install project dependencies. Codex works in the explicitly supplied workspace. Verification prefers an explicit `.maestro/config.toml` `[verification]` command, then uses the repository's native checks (Make, Node, Go, Rust, or pytest when actually available). If no runnable test tool exists, Maestro records that as a note and falls back to `git diff --check`; only a real non-zero result from a selected test command is a test failure.
