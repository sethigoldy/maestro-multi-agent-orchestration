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
| Send a follow-up instruction | — | `followup` |
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
- If two answers arrive at the same moment, only the first one starts the
  task again; the other gets an error saying the task is no longer awaiting
  that answer. The same error comes back when the task was canceled first.
- Every Q&A pair is recorded in the task's transcript, so later follow-ups see
  it too. This includes answers to a task parked by a work-mode gate or waiting
  for approval: the park question and your answer both reach the agent.
- A parked task keeps its workspace while it waits, but not across a daemon
  restart. If another task took the workspace in the meantime, your answer is
  accepted, the task moves to `submitted`, and it resumes when the workspace is
  free (the call reports `"queued": true`).

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
  question first. If two follow-ups arrive at the same moment, only one starts
  a turn and the other gets this "still active" error.
- A follow-up takes the task's workspace, like a new delegation. If another
  task is working in the same workspace, the follow-up waits in the queue
  (the call reports `"queued": true`) and starts when that task finishes, so
  two agents never edit the same working tree at once.
- A task that was canceled can be followed up normally. If the canceled turn
  is still finishing in the background (for example, still running its
  verification), it can no longer change the task's state, free the
  workspace, or start another agent; only the follow-up's turn can.
- With `verification = "command"`, every follow-up runs the task's original
  verification command. The follow-up's instruction is never run as a command.
- Each follow-up decrements `max_depth_remaining` by one (default 3), so
  follow-up chains cannot nest forever. When depth reaches zero, further
  nesting is refused.
- The instruction cannot be empty.

Review the result of a follow-up exactly like the original task: status, audit,
branch diff — see [Inspect tasks and artifacts](inspect-tasks-and-artifacts.md).

## Cancel a task

```text
cancel_task(workspace="/path/to/repo",
            task_id="task-20250718-143022-a1b2c3",
            reason="superseded by task-…")
```

- Works on running tasks and on queued-waiting tasks (it removes them from the
  queue). A queued task that is canceled at the moment it is being started
  stays canceled, runs no agent, and the workspace goes to the next queued
  task.
- The check that the task is still running and the cancel happen in one step.
  A task that finishes at the same moment is either completed (and the cancel
  reports that it already finished) or canceled, never both.
- Canceling a task that waits for an answer also clears the question it was
  waiting on, so a later follow-up does not inherit it.
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
