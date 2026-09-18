# How to inspect tasks and artifacts

This guide shows you how to read what happened: a task's current state, the
durable audit trail, live views of all work, the artifacts on disk, and the git
branch a task produced — plus how to clean up old tasks.

## Read one task's state

```bash
maestro task status <task-id>        # or a numeric task number: maestro task status 3
```

`maestro status <id>` and `maestro task show <id>` are aliases; bare
`maestro task <n>` is normalized to status. The output is JSON with, among
others:

| Field | Meaning |
|---|---|
| `phase` | `DESIGNED` (submitted), `IMPLEMENTING` (working), `REVIEWING` (done, awaiting your review), or `FAILED` (failed **or** canceled) |
| `workspace` / `branch` | where the task ran and the branch it created (`maestro/<task-id>`) |
| `origin_agent` / `target_agent` | who asked for the work and which agent did it |
| `verification` | `PASSED: <report path>` or `FAILED: <report path>` (or absent when verification was skipped) |
| `result` | path to the agent's result file, if any |
| `model` / `effort` | effective per-task settings, when recorded |

Phases are the human-level view; the underlying A2A states (`submitted`,
`working`, `input-required`, `completed`, `failed`, `canceled`) are what the
daemon and MCP tools report. The mapping is documented in
[How delegation works](../explanation/how-delegation-works.md#task-lifecycle).

## List tasks

```bash
maestro task list                    # every task in your user-level state
maestro task list --project /path    # one project (all its worktrees)
maestro task list --workspace /path  # one exact workspace
```

Scoping defaults to `$MAESTRO_WORKSPACE` or the current directory; see the
[CLI reference](../reference/cli.md#task-list) for the exact rules. Each entry
carries `task_id`, number, title, phase, and workspace — enough to pick what
to inspect next.

## Read the durable audit trail

```bash
maestro task audit <task-id>
```

This is the record that survives restarts: final state, workspace, branch,
origin/target agents, **every attempt** (agent, exit code, duration, usage,
error), accumulated usage/cost, the final error if any, and the parsed result
files. Use it to answer "why did this fail?" — each fallback hop and retry is a
separate attempt entry, so you can see exactly which agent was tried, in what
order, and what each one cost (failed work included).

## Watch everything live

- **Terminal dashboard** — `maestro dashboard`: full-screen, event-driven
  (no polling). `j`/down and `k`/up move between tasks; `q`, Esc, or Ctrl-C
  quits.
- **Web console** — `http://127.0.0.1:<port>/` (the port printed by
  `maestro-daemon`): all tasks, live output, costs, per-attempt detail. From
  another machine: `http://host:port/?token=<t>`.
- **Live-tail one task** — `maestro task tail <id>` (`--all` for everything).

## Find the artifacts on disk

Everything a task produced lives under your state directory, per task:

```text
~/.maestro/tasks/<task-id>/
├── result-<agent>-t<turn>-<attempt>.json   # one file per attempt (agent output, exit code, usage)
├── verification.txt                        # the deterministic check: command run + full output
└── … adapter logs for the task's runs
```

The claim journal (`~/.maestro/state.jsonl`) additionally holds the durable
`task_runtime` snapshot (state, attempts, usage, error) that `task audit` and
the status fallbacks read. The [configuration reference](../reference/configuration.md#where-state-lives)
documents the full layout.

## Review the work on its branch

With the default `commit_policy = "branch"`, each task runs on branch
`maestro/<task-id>` in its workspace:

```bash
git -C /path/to/repo status
git -C /path/to/repo diff            # uncommitted changes, if the agent did not commit
git -C /path/to/repo log --oneline   # commits, if the agent committed
```

Maestro never commits; deciding what to do with the branch (commit, merge,
open a pull request, discard) is yours. If you work in git worktrees, remember
that the task recorded the *worktree path* it ran in — check that directory,
not just the main checkout.

## Clean up old tasks

```bash
maestro gc --dry-run                 # preview
maestro gc --days 30                 # delete terminal tasks older than 30 days
```

Only finished tasks are eligible (`COMPLETE`/`FAILED` phases — canceled tasks
land in `FAILED`); active and queued work is never touched. `gc` is manual; it
never runs automatically. The default TTL is 90 days.
