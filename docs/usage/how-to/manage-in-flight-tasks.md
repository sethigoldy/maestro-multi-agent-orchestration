# How to manage in-flight tasks

This guide shows you how to steer a task after it has started: watch it live,
answer the agent's questions, send follow-up instructions, cancel work, and
block until completion. For reading finished state, see
[Inspect tasks and artifacts](inspect-tasks-and-artifacts.md).

## Know which interface can do what

| Action | CLI | MCP tools |
|---|---|---|
| Watch live output | `maestro task tail <id>` | (console/dashboard) |
| Answer an agent question | — | `answer_task_question` |
| Send a follow-up instruction | `maestro task continue <id>` | `followup` |
| Rename a finished task's branch | `maestro task rename-branch <id> <name>` | `rename_task_branch` |
| Cancel the task | — | `cancel_task` |
| Block until done or input needed | `maestro task tail <id>` (streams to the end) | `task_wait` |

Steering actions (question, follow-up, cancel) are exposed through the MCP
tools only; the CLI is for watching and reading state. If you drive Maestro
purely from the shell, your in-flight options are live-tail plus waiting —
steer later via an MCP client or by letting the task finish and reviewing.

## Watch a task live

```bash
maestro task tail <task-id>          # one task's event stream
maestro task tail --all              # everything, all tasks
```

The stream prints the agent's output lines as they happen, plus state changes:

```text
[state] working
… agent output …
[usage] {"agent": "codex", "total_cost_usd": 0.42}
[state] completed
```

If the task asks a question you will see
`[state] input-required (question: …)`. Press `Ctrl-C` to stop tailing (exit
code 130); when following one task, the exit code is 0 if it ended `completed`,
1 otherwise. For a full-screen view of all tasks at once use
`maestro dashboard` or the web console — see
[Inspect tasks and artifacts](inspect-tasks-and-artifacts.md).

## Answer an agent's question

When an agent stops mid-task to ask something, the task state becomes
`input-required` and it waits. From an MCP client:

```text
answer_task_question(workspace="/path/to/repo",
                     task_id="task-20250718-143022-a1b2c3",
                     answer="Use the existing repository layer; no new dependencies.")
```

The agent resumes on the same task branch with the question and your answer
appended to its context. Rules:

- The task must be in `input-required`; answering anything else is an error.
- The answer cannot be empty.
- Every Q&A pair is recorded in the task's transcript, so later follow-ups see
  it too.

## Send a follow-up instruction

A finished task — `completed`, `failed`, or `canceled` — can continue: the same
agent resumes on the same task branch with its previous work and Q&A in
context. This is how you send fix passes without re-delegating from scratch:

```text
followup(workspace="/path/to/repo",
         task_id="task-20250718-143022-a1b2c3",
         instruction="The tests pass, but add a docstring to double() and rerun the suite.")
```

Behavior and rules:

- The call blocks until the follow-up turn finishes or needs input — no
  polling.
- You cannot follow up while the task is still active; cancel it or answer its
  question first.
- Each follow-up decrements `max_depth_remaining` by one (default 3), so
  follow-up chains cannot nest forever. When depth reaches zero, further
  nesting is refused.
- The instruction cannot be empty.

Review the result of a follow-up exactly like the original task: status, audit,
branch diff — see [Inspect tasks and artifacts](inspect-tasks-and-artifacts.md).

## Rename a task's branch

A finished or parked task keeps the branch it was given. To rename it:

```bash
maestro task rename-branch 3 feat/login-form
```

or, from an MCP client:

```text
rename_task_branch(workspace="/path/to/repo", task_id="3", branch="feat/login-form")
```

This renames the git branch and updates the task's record, so `task list`,
`task status`, receipts and the next follow-up all use the new name. If you
already ran `git branch -m` yourself, the command sees that the old branch is
gone and the new one exists. It checks git's reflog to confirm the new branch
was renamed from the task's branch, and then only updates the record. If git
has no record of that rename, the command refuses, because the new branch may
be unrelated to the task.

You do not have to record a hand rename before the next follow-up. When a
turn starts and the task's branch is missing, Maestro looks for the rename in
git's reflog and, if exactly one branch was renamed from it, uses that branch
and updates the record. If the branch was deleted instead, the turn fails
rather than starting again on a new, empty branch; recreate the branch with
`git branch <name> <commit>` and send the follow-up again.

If the task has no branch yet, because its first turn could not create the
branch it asked for, the same command changes the name that the next turn will
create. You can also rename as part of the follow-up:

```bash
maestro task continue 3 --request "try again" --branch feat/login-form-2
```

Rules:

- The task must not be running. Wait for it to finish or park first. A
  follow-up or answer sent while a rename is under way waits for the rename
  to finish.
- The new name must not already exist, unless it is the task's branch that
  you renamed by hand. It also must not clash with an existing branch as a
  folder (`feat` and `feat/login` cannot both exist).
- Only the local branch is renamed. If you pushed the old branch, rename or
  delete it on the remote yourself.

## Cancel a task

```text
cancel_task(workspace="/path/to/repo",
            task_id="task-20250718-143022-a1b2c3",
            reason="superseded by task-…")
```

- Works on running tasks and on queued-waiting tasks (it removes them from the
  queue).
- Partial work stays on the task branch — nothing is rolled back.
- The task is marked `canceled` with your reason; already-finished tasks cannot
  be canceled (they are terminal).

## Block until completion

From an MCP client, `task_wait` blocks until the task reaches a new terminal
state or needs input, or until the timeout expires:

```text
task_wait(workspace="/path/to/repo", task_id="…", timeout=600)
```

Use it to follow up on earlier delegations instead of polling `task_status`.
The default timeout is 120 seconds; the `delegate` and `followup` tools use a
longer bound controlled by `MAESTRO_DELEGATE_TIMEOUT` (default 3600 s). See the
[MCP tools reference](../reference/mcp-tools.md) for exact return shapes.
