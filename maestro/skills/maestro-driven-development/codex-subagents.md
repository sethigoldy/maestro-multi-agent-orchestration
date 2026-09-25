## Codex: run each Maestro task as a subagent

This section applies to Codex only. Maestro installs a Codex custom agent
named `maestro_worker` (the file `~/.codex/agents/maestro-worker.toml`). When
you delegate through it, each Maestro task appears in Codex's subagent panel
with its own status, instead of running as a shell command in your turn.

If you are a `maestro_worker` yourself, ignore this section and follow your
own instructions.

When your session has the `spawn_agent` tool and `maestro_worker` is one of
its agent types, use it for section 5 instead of running `maestro delegate`
yourself:

1. Prepare the handoff as in section 4.
2. Spawn one worker for each task: `spawn_agent` with
   `agent_type = "maestro_worker"`. In the message, give the workspace
   absolute path, the title, the request, and any `--target`, `--mode`,
   `--branch` or handoff file that the user asked for.
3. Wait for the worker with `wait_agent`. Its final message has the task id,
   the final state, `run_dir` and the branch.
4. Review the result as in section 7. To send a fix pass or an answer to a
   parked task, send the instruction to the same worker (with `send_input` or
   `followup_task`, whichever your session has). The worker runs
   `maestro task continue` or `maestro task answer` for you.
5. When the task is done, close the worker if your session has `close_agent`.

Independent tasks can run in parallel: spawn one worker for each. Maestro runs
extra tasks for the same workspace in their own git worktrees (section 6).

If `spawn_agent` is missing, or `maestro_worker` is not one of its agent
types, delegate with the CLI as in section 5. Everything else in this skill
applies unchanged.
