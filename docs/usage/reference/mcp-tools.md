# MCP tools reference

The Maestro MCP server (name: `maestro`) exposes eight tools. Every tool takes
plain arguments and returns a JSON string. State-changing tools that start work
(`delegate`, `followup`) **block** until the work completes, fails, or needs
input — they do not return a handle to poll.

Task ids are of the form `task-YYYYMMDD-HHMMSS-xxxxxx`; numeric task numbers
are accepted where a task id is expected (resolved against user-level state).

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

For tasks delegated with a work mode (or explicit gate agents), `metadata` also
carries `gates` — one entry per LLM gate turn that ran, shaped
`{"agent": …, "ok": true|false, "issues": [ … ]}` — and `bounces`, the number of
auto-fix bounces consumed. Both are absent for tasks without gates.

States: `submitted`, `working`, `input-required`, `completed`, `failed`,
`canceled`. Terminal states: `completed`, `failed`, `canceled`. The mapping to
human-level phases is in [How delegation works](../explanation/how-delegation-works.md#task-lifecycle).

## delegate

```text
delegate(workspace: str, handoff_file: str) -> str
```

Loads the handoff file (4-section TOML/JSON or legacy 0.8.x JSON — see
[handoff format reference](handoff-format.md)) and submits it to the daemon for
the given workspace. Blocks until the task reaches a terminal state or
`input-required`, or until the timeout expires.

A handoff file may carry `[routing] mode = "NAME"` (or the explicit
`review_agent`/`verify_agent`/`fix_agent`/`max_bounces` fields) to run the task
under a work-mode preset — see [Configure work modes](../how-to/configure-work-modes.md).
Signature unchanged: the routing lives in the file.

A handoff file may also carry `[[context]]` entries (label + `text` or `path`,
optional `kind` and `phases`) to inject user-controlled context into the agent
turns — see [Context injection in the README](../../../README.md#context-injection).
Signature unchanged: the context lives in the file.

- Timeout: `MAESTRO_DELEGATE_TIMEOUT` seconds (default 3600). On expiry the
  result carries `"timed_out": true` alongside the current task object.
- If the workspace already has an active task, returns immediately with
  `{"queued": true, "reason": "workspace already has an active task; this handoff is next in line", "ts": …}`.
- Errors (unknown file, invalid handoff, self-delegation, depth exhausted,
  budget cap, non-git workspace) return `{"error": "<message>"}`.

## followup

```text
followup(workspace: str, task_id: str, instruction: str) -> str
```

Resumes a finished task (`completed`/`failed`/`canceled`) with a new
instruction; the same agent continues on the same task branch with its previous
Q&A in context. When the task was delegated under a work mode, follow-ups run
under that mode's `fixer` (the agent already pinned to fixing) instead of the
original implementer. Blocks like `delegate` (same timeout and `timed_out`
semantics). Each follow-up decrements `max_depth_remaining` by one.

Errors: unknown task (`KeyError` text), empty instruction, task still active
(`"…cancel it or answer its question before following up"`), depth exhausted —
each returned as `{"error": …}`.

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
on its branch with the Q&A appended to its context. Returns
`{"task_id": …, "state": "working"}`. Errors: unknown task, task not in
`input-required`, empty answer — returned as `{"error": …}`.

## Notes

- The server process may outlive the session that launched it; every tool takes
  an explicit `workspace`, and state tools never rely on the server's cwd.
- Delegation enforces no self-delegation: a handoff whose `target_agent` equals
  its `origin_agent` is refused. When a host agent delegates through these
  tools, set `origin_agent` to that agent's name in the handoff and choose a
  different target.
- Budget caps (`MAESTRO_BUDGET_*_USD`) are enforced at launch; exhausted caps
  surface as `delegate` errors.
