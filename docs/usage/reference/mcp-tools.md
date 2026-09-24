# MCP tools reference

The Maestro MCP server (name: `maestro`) exposes eight tools. Every tool takes
plain arguments and returns a JSON string. State-changing tools that start work
(`delegate`, `followup`) **block** until the work completes, fails, or needs
input — they do not return a handle to poll.

Task ids are of the form `task-YYYYMMDD-HHMMSS-xxxxxx`; numeric task numbers
are accepted where a task id is expected (resolved against user-level state).

## Which daemon runs the tasks

The tools that start, wait for or change tasks (`delegate`, `task_wait`,
`followup`, `answer_task_question`, `cancel_task`, `rename_task_branch`, `cleanup_task_worktree`,
`agents_list`) need a daemon. Only one daemon owns a state directory, and the
MCP server never runs tasks beside it:

- When a daemon owns the state directory (for example one started with
  `maestro daemon start`), the MCP server sends every one of these tool calls
  to that daemon over its HTTP API (JSON-RPC, with the daemon's token when it
  has one). The tasks run in that daemon, so `maestro daemon stop`, the
  dashboard and the daemon's `tasks/cancel` control them, and one workspace
  never has two active tasks. Results and errors are the same as when the
  daemon runs inside the MCP server.
- When no daemon owns the state directory, the MCP server starts the same
  detached background daemon that `maestro daemon start` starts and forwards
  to it. The daemon is a separate process, so closing the session whose MCP
  server started it stops neither the daemon nor any task, including tasks
  that other sessions sent to it.
- The background daemon runs with the environment of the process that
  started it: the MCP server's environment here, including the `PATH` used to
  find agent binaries and the `MAESTRO_*` settings. A daemon that was started
  earlier, by `maestro daemon start` or by another MCP server, keeps its own
  environment. To give it a new one, run `maestro daemon restart` from a shell
  with the environment you want, or run `maestro daemon stop` and let the next
  tool call start a new daemon with this MCP server's environment.
- Only when a background daemon cannot be started does the MCP server run
  the daemon inside its own process. It prints a note on stderr. That daemon
  serves HTTP and owns the directory like any other, and it is stopped when
  the MCP server exits (including on SIGTERM), which marks its running tasks
  as failed.
- If the daemon stops answering while it is still alive, a tool call waits
  and asks again with growing pauses for up to 20 seconds, then returns
  `{"error": …}` saying to retry or run `maestro daemon restart`. Once the
  daemon has exited, the next tool call starts a new one. A blocking
  `delegate`, `followup` or `task_wait` that is waiting at that moment carries
  on waiting in the new daemon. One deadline covers the whole wait, however
  often the daemon changes: when it passes, the wait returns the task as it
  was last seen.
- A daemon from Maestro 0.12.0 or earlier does not have the methods the MCP
  server forwards its calls with. When such a daemon owns the state directory
  (for example, another session still runs the older version), the MCP server
  runs its tasks in a daemon inside its own process, without an HTTP endpoint
  and without taking the directory over, as older versions did. It prints a
  note on stderr. The older daemon cannot see or cancel those tasks. Restart
  the older daemon with `maestro daemon restart`, or close the sessions that
  still use the older version, to share one daemon again.

`task_status` and `list_tasks` read the durable task state directly and do not
need a daemon.

## Task object shape

`delegate`, `task_wait`, and the cancel/answer tools resolve to an A2A task
object:

```json
{
  "kind": "task",
  "id": "task-20250718-143022-a1b2c3",
  "status": { "state": "completed", "timestamp": "2025-07-18T14:33:02+00:00" },
  "artifacts": [
    { "artifactId": "<task-id>:result-codex-t1-0.json", "name": "result-codex-t1-0.json",
      "parts": [{ "kind": "url", "url": "/home/you/.maestro/tasks/<task-id>/result-codex-t1-0.json" }] }
  ],
  "metadata": {
    "workspace": "/path/to/repo",
    "run_dir": "/path/to/repo",
    "branch": "maestro/task-20250718-143022-a1b2c3",
    "origin_agent": "human",
    "target_agent": "codex",
    "title": "Fix double() in app.py",
    "usage": { "total_cost_usd": 0.42 },
    "attempts": [ { "agent": "codex", "ok": true, "exit_code": 0, "duration_s": 91.2 } ],
    "error": null
  }
}
```

`run_dir` is where the task's work is: the workspace itself, or the task's own
worktree under `~/.maestro/worktrees/<task-id>` when the task was delegated
while the workspace was busy.

For tasks delegated with a work mode (or explicit gate agents), `metadata` also
carries `gates` — one entry per LLM gate turn that ran, shaped
`{"agent": …, "ok": true|false, "issues": [ … ]}` — and `bounces`, the number of
auto-fix bounces consumed. Both are absent for tasks without gates.

States: `submitted`, `working`, `input-required`, `completed`, `failed`,
`canceled`. Terminal states: `completed`, `failed`, `canceled`. The mapping to
human-level phases is in [How delegation works](../explanation/how-delegation-works.md#task-lifecycle).

## delegate

```text
delegate(workspace: str, handoff_file: str, branch: str = "") -> str
```

Loads the handoff file (4-section TOML/JSON or legacy 0.8.x JSON — see
[handoff format reference](handoff-format.md)) and submits it to the daemon for
the given workspace. Blocks until the task reaches a terminal state or
`input-required`, or until the timeout expires.

A handoff file may carry `[routing] mode = "NAME"` (or the explicit
`review_agent`/`verify_agent`/`fix_agent`/`max_bounces` fields) to run the task
under a work-mode preset — see [Configure work modes](../how-to/configure-work-modes.md).
Signature unchanged: the routing lives in the file.

Routing defaults: when the handoff names no target agent, Maestro resolves it
from the config chain's `[defaults]` table (`agent`, `fallback`, `model`,
`effort` — see [Configuration reference](configuration.md#defaults--routing-defaults)).
If neither the handoff nor `[defaults]` names an agent, the call returns a task
in state `input-required` whose question lists every available agent; ask the
user which agent and model to use, then call `answer_task_question` (accepted:
a bare agent name, `agent=… model=…` pairs, or JSON). Sensitive workspaces
park the same way with an approval question.

A handoff file may also carry `[[context]]` entries (label + `text` or `path`,
optional `kind` and `phases`) to inject user-controlled context into the agent
turns — see [Context injection in the README](../../../README.md#context-injection).
Signature unchanged: the context lives in the file.

`branch` names the task's git branch, for example `"feat/login-form"`. It
overrides `[expectations] branch` in the handoff file. When neither is set, the
branch is `maestro/<task-id>`. The branch must not exist yet, and its name must
not clash with an existing branch as a folder (with a branch `feat` present,
`feat/login` cannot be created, and the reverse). An invalid, existing or
clashing name returns `{"error": …}` before any agent runs. The name applies to
this daemon's workspace only: when the target is a remote daemon, the forwarded
handoff leaves it out and the remote uses its own default branch.

- Timeout: `MAESTRO_DELEGATE_TIMEOUT` seconds (default 3600). On expiry the
  result carries `"timed_out": true` alongside the current task object.
- If the task cannot start yet, returns immediately with
  `{"queued": true, "reason": …, "ts": …}`. The reason says why: the workspace
  is at its limit of running tasks (`[defaults] max_parallel`), or the task
  works in place (`no-commit`) or has its work in the workspace, and another
  task is using the workspace. A task delegated while the workspace is busy
  usually does not queue: it runs in a worktree of its own.
- Errors (unknown file, invalid handoff, self-delegation, depth exhausted,
  budget cap, non-git workspace, invalid, existing or clashing `branch`) return
  `{"error": "<message>"}`.

## followup

```text
followup(workspace: str, task_id: str, instruction: str, context_mode: str = "reuse", branch: str = "") -> str
```

Resumes a finished task (`completed`/`failed`/`canceled`) with a new
instruction; the same agent continues on the same task branch. When the task
was delegated under a work mode, follow-ups run under that mode's `fixer` (the
agent already pinned to fixing) instead of the original implementer. Blocks
like `delegate` (same timeout and `timed_out` semantics). Each follow-up
decrements `max_depth_remaining` by one.

`context_mode`: `"reuse"` (default) injects a compact **task-knowledge**
snapshot — goal, current state, files changed, latest verification result and
failures, known issues, and a bounded tail of the last turn's output — projected
from durable state, so the agent continues without re-discovering the work; raw
history stays in the task record and is never replayed. `"fresh"` skips the
snapshot for a clean reasoning context (same task/workspace/branch). See
[Configuration: `[continuation]`](./configuration.md#continuation--task-continuation-context)
to disable reuse or resize its budget.

`branch`: optional new name for the task's branch. The branch is renamed first,
with the same rules as `rename_task_branch`, and the turn then runs on it. If
the task has no branch yet (its first turn could not create the branch it asked
for), this sets the name the turn creates. An empty string leaves the branch as
it is.

Errors: unknown task (`KeyError` text), empty instruction, task still active
(`"…cancel it or answer its question before following up"`), depth exhausted,
any `rename_task_branch` refusal when `branch` is set — each returned as
`{"error": …}`. A refused `branch` changes nothing and starts no turn. When
two follow-ups for the same task arrive at the same moment, only one starts a
turn; the other gets the "still active" error.

## rename_task_branch

```text
rename_task_branch(workspace: str, task_id: str, branch: str) -> str
```

Renames a task's git branch and updates the task's record, so `list_tasks`,
`task_status`, receipts and later `followup` turns all use the new name.
Accepts a task id or a task number. If the branch was already renamed by hand
with `git branch -m`, the call only updates the record.

If the task has no branch yet, because it has not started or its first turn
could not create the branch it asked for, the call changes the name that the
next turn will create. The new name must be one that can be created, as at
delegation.

Returns `{"task_id": …, "old_branch": …, "branch": …, "git_renamed": true|false}`.
`git_renamed` is `false` when only the record changed. When the task had no
branch yet, the result also has `"pending": true`, and `old_branch` is the name
the next turn would have created.

Only the local branch is renamed. A copy already pushed to a remote keeps its
old name there.

Errors, each returned as `{"error": …}`: unknown task; the task is still
running (`submitted` or `working`) or a turn is starting; the task uses
`commit_policy = "no-commit"`, so it never has a branch; an invalid branch
name; the new branch already exists or clashes with an existing branch as a
folder; neither the old nor the new branch exists; the old
branch is gone and git's reflog shows no rename from it to the new branch (the
new branch may be unrelated to the task).

## cleanup_task_worktree

```text
cleanup_task_worktree(workspace: str, task_id: str, force: bool = False) -> str
```

Removes the git worktree of a task that ran next to a busy workspace. The
task's branch and its commits are always kept. Accepts a task id or a task
number.

Returns `{"task_id": …, "run_dir": …, "removed": true|false, "reason": …}`.
`removed` is `false`, with a reason, when the task ran in the workspace itself
(Maestro never removes your checkout) or the worktree is already gone. A later
`followup` on a task whose worktree was removed creates the worktree again from
its branch; only committed work comes back.

Errors, each returned as `{"error": …}`: unknown task; the task is running (`submitted` or
`working`) or a turn is starting; the worktree has uncommitted changes and
`force` is not true (the message lists the files); git refused to remove the
worktree (for example, it is locked).

## task_wait

```text
task_wait(workspace: str, task_id: str, timeout: float = 120.0) -> str
```

Blocks until the task reaches a new terminal state or `input-required`, or the
timeout expires (default 120 s). Returns the task object; on timeout, the
current task object. Unknown tasks return `{"error": …}`. Use this to follow up
on earlier delegations — never poll.

## task_status

```text
task_status(workspace: str, task_id: str) -> str
```

Returns the human-level state record (the same JSON as `maestro task status`):
`task_id`, `task_number`, `title`, `phase`, `workspace`, `branch`,
`origin_agent`, `target_agent`, `model`, `effort`, `result`, `verification`,
`project_root`, and — for work-mode tasks — `gates` (per-gate agent, ok, issue
count) and `bounces`. Numeric task numbers are accepted.

The workspace argument is resolved to its git root, so it must be inside a git
repository.

## list_tasks

```text
list_tasks(workspace: str) -> str
```

Returns the JSON array of tasks for the project that contains `workspace`
(same resolution as `task_status`). Each entry carries `task_id`, number,
title, phase, and workspace.

## agents_list

```text
agents_list() -> str
```

No arguments. Returns a JSON array of every registered agent: its spec (name,
kind, display name, skills, model/effort defaults, timeout) plus live status
(binary found, version; for `a2a_remote` the remote agent card check).

## cancel_task

```text
cancel_task(workspace: str, task_id: str, reason: str = "") -> str
```

Cancels a running or queued-waiting task. Partial work on the task branch is
kept; the task is marked `canceled` with the reason (default `"canceled by
user"`). Returns `{"task_id": …, "state": "canceled"}`. Errors: unknown task,
or the task already finished — returned as `{"error": …}`.

## answer_task_question

```text
answer_task_question(workspace: str, task_id: str, answer: str) -> str
```

Answers a question asked mid-task (state `input-required`). The agent resumes
on its branch with the Q&A appended to its context. The answer starts a new
turn, so the task moves to `submitted` straight away and then to `working`.
Returns `{"task_id": …, "state": "working"}`, or
`{"task_id": …, "state": "submitted", "queued": true}` when another task holds
the workspace, or `{"task_id": …, "state": "input-required"}` when the task
still has no agent chosen and now asks the routing question. Errors: unknown task, task not in `input-required`, empty
answer, and an answer that lost a race (another answer given at the same
moment, or a cancel, reached the task first) — returned as `{"error": …}`.

## Notes

- The server process may outlive the session that launched it; every tool takes
  an explicit `workspace`, and state tools never rely on the server's cwd.
- Delegation enforces no self-delegation: a handoff whose `target_agent` equals
  its `origin_agent` is refused. When a host agent delegates through these
  tools, set `origin_agent` to that agent's name in the handoff and choose a
  different target.
- Budget caps (`MAESTRO_BUDGET_*_USD`) are enforced at launch; exhausted caps
  surface as `delegate` errors.
