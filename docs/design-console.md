# Design: a web console you can act from

Status: approved 2026-09-25, built in 0.16.0.

## 1. The problem

The web console that the daemon serves at `http://127.0.0.1:9785/` only
showed tasks. Everything else needed the CLI or an MCP client. On 2026-09-25 a
Codex session opened the console to answer a parked task, found no way to do
it, and gave up. The console also gave no way to see a task's changes, the
verification report, which tasks were waiting and why, or whether the daemon
and its agents were healthy.

## 2. Decision

The console gets four groups of features, each in its own pull request:

1. **Act on tasks.** A "Needs you" strip lists every task waiting for input,
   with its question and a way to answer it. The detail pane gets Cancel,
   Follow-up, Remove worktree and Rename branch.
2. **Review the work.** Per task: the diff of its `run_dir` against the commit
   it started from, and the verification report.
3. **See what needs you.** Filters and search, grouping by workspace, the
   reason a queued task waits, each workspace's use of its `max_parallel`
   limit, and optional browser notifications.
4. **System view.** The daemon's version, port and uptime, the registered
   agents and whether each is installed, budget spend against the caps, and
   the effective config for each workspace.

Starting new tasks from the browser is left out: agents and the CLI already
delegate, and a form would be the riskiest write action to add.

### Alternatives considered

| Option | Why not |
|---|---|
| Keep the console read-only and improve the CLI only | The console is where a person looks first; the dead end on 2026-09-25 happened there. |
| A separate web server for the console | Two servers to secure and keep in sync. The daemon already serves the console and already guards writes. |
| New REST endpoints for every action | The daemon already has JSON-RPC methods for every action (`tasks/answer`, `tasks/cancel`, `tasks/followup`, `tasks/cleanup`, `tasks/renameBranch`, `agents/list`). The console calls those, so the CLI, MCP and the console share one code path. |

## 3. Security

The console calls the same JSON-RPC endpoint (`POST /`) as the CLI. The
daemon already refuses a POST unless its body is `application/json` (which a
cross-site page cannot send without a CORS preflight the daemon never
approves) and, when the request has an `Origin` header, unless that origin is
the daemon's own address or listed in `allowed_origins`. On a non-loopback
bind, every request needs the token, which the console sends as it already
does for reading. No new write path is added, so these rules cover every
action. The new read-only endpoints (diff, verification report, workspaces,
system) sit behind the same token check as `/tasks`.

## 4. Act on tasks

- Task metadata gains `question`, `awaiting` (`routing`, `approval`,
  `question` or `gate`), `run_dir_kind`, `queued`, `queue_reason` and
  `started_at`. Before, the console learned the question only from a live
  event, so a page opened after a task parked never showed it.
- **Needs you** lists tasks in state `input-required`, oldest first. The
  answer control depends on `awaiting`:
  - `routing`: a picker of the registered agents (name, whether the binary is
    installed, version, default model) and an optional model field; it sends
    `agent=<name> model=<model>`.
  - `approval`: Approve (sends `approved`) and Cancel task.
  - `question` and `gate`: a text box and Send answer.
- The detail pane shows only the actions allowed in the task's state:
  Cancel (queued, running or parked; asks to confirm), Follow-up (finished;
  instruction, reuse or fresh context, optional new branch), Remove worktree
  (finished worktree task; the daemon's refusal lists uncommitted files, and
  "Remove anyway" asks to confirm), Rename branch (not while running).
- Daemon errors are shown under the control that caused them, in the daemon's
  own words.
- A task first seen through a live event is fetched with `tasks/get`, so it
  shows its title, workspace and agents at once.

## 5. Review the work

- `GET /tasks/<id>/diff` returns the task's changes: the diff of its `run_dir`
  against the commit the task started from (so commits the agent made are
  included), plus untracked files. At most 400 KB of diff text is returned,
  with `truncated: true` when there was more.
- `GET /tasks/<id>/verification` returns the text of the last verification
  report.
- `GET /tasks/<id>/output` returns the latest 2000 lines of the task's agent
  logs, because the event stream carries output only while it happens and a
  page opened later would otherwise show none.
- The detail pane gets tabs: Output, Changes, Verification, Receipt.
- Below 760 pixels wide, the task list goes above the detail pane.

## 6. See what needs you

- `GET /workspaces` lists each workspace with its running, parked and queued
  task counts and its effective `max_parallel`.
- The task list gets state filters, an agent filter, text search over title
  and task id, and grouping by workspace with each group's
  `running / max_parallel`. A queued task's card shows its reason.
- Browser notifications, off until the user turns them on, fire when a task
  starts waiting for input, completes or fails.

## 7. System view

- `GET /system` returns the daemon's version, pid, port, bind address, state
  directory, uptime, budget caps and today's spend per agent.
- A System view shows it with the agents list (from `agents/list`) and the
  effective config of each workspace from `GET /workspaces`.

## 8. Testing

- Python tests for every new metadata field and endpoint.
- Node tests (as `tests/test_console_js.py` already does) for the console's
  pure logic: the JSON-RPC client, which actions a state allows, the routing
  answer text, filters, grouping and diff parsing.
- The console bundle in `maestro/web_dist/` is rebuilt, and CI checks it
  matches the source.
- A check in a real browser against a real daemon for each group.
