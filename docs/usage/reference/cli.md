# CLI reference

Complete description of the `maestro` and `maestro-daemon` commands. The CLI is
version 0.8.4; verify with `maestro --version`.

## Global options

| Option | Meaning |
|---|---|
| `--version` | Print the version and exit |
| `--workspace DIR` | Scope task commands to this workspace. Mutually exclusive with `--project`. Default: `$MAESTRO_WORKSPACE`, else the current directory. An empty value scopes to the home directory |
| `--project DIR` | Scope task commands to this project root (all of its worktrees). The path must exist and be a directory |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | `task tail <id>`: the followed task ended in a non-`completed` terminal state; a daemon connection failure for `dashboard`; or `peers remove` for an unknown peer |
| `2` | Usage or runtime error. A message prefixed `maestro:` is printed to stderr (e.g. unknown task, missing daemon, invalid handoff) |
| `130` | `task tail` interrupted by Ctrl-C |

## Daemon resolution

Commands that talk to the daemon (`delegate`, `task tail`, `dashboard`) resolve
the endpoint in this order:

1. `MAESTRO_DAEMON_URL` (optionally with `MAESTRO_DAEMON_TOKEN`).
2. The `daemon.json` marker written by whichever broker started last. The
   marker's process is liveness-checked; a stale marker yields the error
   `no daemon reachable — start one with 'maestro-daemon' or set MAESTRO_DAEMON_URL`.

Non-loopback daemons record their auth token in the marker, so local CLI calls
are authorized automatically.

## maestro-daemon

```text
maestro-daemon [--port N] [--bind IF] [--state-dir DIR]
```

Starts the broker daemon and blocks until SIGINT/SIGTERM. Prints one JSON line
on startup: `pid`, `port`, `bind`, `advertised_host`, `state_dir`, and `token`
when authentication is enabled.

| Option | Default | Meaning |
|---|---|---|
| `--port N` | `0` | Port to bind; `0` picks a free port |
| `--bind IF` | `127.0.0.1` | Listen interface. `127.0.0.1` (loopback, no token), `0.0.0.0`/`::` (all interfaces — token auth enabled, primary LAN IP advertised), or an explicit IP (advertised and dialed as-is; token auth enabled) |
| `--state-dir DIR` | `~/.maestro` or `$MAESTRO_HOME` | State directory for this daemon |

## delegate

```text
maestro delegate (--file FILE | --title T --request R (--target A | --mode NAME))
                 [--fallback A …] [--design-file FILE]
                 [--context TEXT …] [--context-file PATH …] [--skill DIR …]
                 [--no-wait] [--workspace DIR | --project DIR]
```

Submits a handoff to the daemon and, by default, blocks while streaming the
task's event stream.

| Option | Meaning |
|---|---|
| `--file FILE` | Handoff document file (TOML or JSON; 4-section or legacy format). Mutually exclusive in effect with the flag form — one of the two forms is required |
| `--title T` / `--request R` / `--target A` | Minimal handoff from flags. `--title` and `--request` are always required in this form, plus either `--target` or `--mode` |
| `--mode NAME` | Apply the work-mode preset of that name (a `[modes.NAME]` config table) — it pins the implementer/verifier/reviewer/fixer agents for this task. With `--file`, a flag value overrides the file's `[routing] mode` |
| `--fallback A` | Fallback agent, tried in order after the target fails or is unavailable. Repeatable |
| `--design-file FILE` | File whose text becomes the handoff's authoritative design |
| `--context TEXT` | Add a `text` context entry to the handoff. Repeatable; labels are auto-numbered (`context-1`, …) |
| `--context-file PATH` | Add a `file` context entry for that path (inlined when ≤8KB, otherwise artifact-referenced). Repeatable; label = file stem |
| `--skill DIR` | Add a `skill` context entry: an [Agent Skills](https://agentskills.io/) directory containing a `SKILL.md`, staged for the task. Repeatable; label = directory name |
| `--no-wait` | Return immediately after enqueueing; prints the daemon's response (task id or queued notice) and exits 0 |

Output while waiting: a `[task] <id> — target=… workspace=…` line (`mode=…`
instead of `target=…` when a work mode is set), then stream lines — agent
output, `[state] <state>` (with `— <error>` or `(question: …)` annotations),
and `[usage] {…}`. Exit code follows the final state (0 for `completed`, 1
otherwise; 130 on Ctrl-C).

If the workspace already has an active task, the handoff is queued FIFO and the
command prints `{"queued": true, "reason": …}` and exits 0.

## task

```text
maestro task list [--workspace DIR | --project DIR]
maestro task status <task-id|number> [--workspace DIR | --project DIR]
maestro task show <task-id|number>        # alias of status
maestro task tail <task-id> [--all]
maestro task audit <task-id>
```

Bare `maestro task <n>` is normalized to `task status`. Top-level aliases:
`maestro list …`, `maestro status <id> …`.

| Subcommand | Behavior |
|---|---|
| `list` | Prints JSON array of tasks (`task_id`, number, title, phase, workspace). Without a scope flag or `$MAESTRO_WORKSPACE`: all user-level tasks. With `--project`: tasks of that project. With an explicit `--workspace`: the project's tasks if the path is a project root or a `.claude/worktrees` parent, else exactly that workspace |
| `status` / `show` | Prints one task's state as JSON (see [Inspect tasks and artifacts](../how-to/inspect-tasks-and-artifacts.md#read-one-tasks-state) for the fields). Accepts a full task id or a numeric task number. Unknown references exit 2 |
| `tail` | Live-follows one task's event stream over SSE (no polling). `--all` follows the global stream instead of one task; with `--all` the exit code is always 0 (absent Ctrl-C) |
| `audit` | Prints the durable record as JSON: title, state, workspace, branch, origin/target agents, attempts (agent, ok, exit code, duration, usage, error), accumulated usage, error, and parsed result files. Works after daemon restarts |

## dashboard

```text
maestro dashboard
```

Full-screen terminal view of the local daemon, driven by SSE. Keys: `j`/down
and `k`/up move between tasks; `q`, Esc, or Ctrl-C quits. Exits 1 if no daemon
is reachable (message on stderr).

## agents

```text
maestro agents list
maestro agents add --name N --kind K [--display-name S] [--skill S …]
                   [--command CMD] [--input-mode arg|stdin]
                   [--output-format text|jsonl|rpc]
                   [--workspace-policy cwd|flag] [--token T]
maestro agents remove <name>
maestro agents discover
maestro agents status <name>
```

| Subcommand | Behavior |
|---|---|
| `list` | JSON array of registered agent specs (user-level, `~/.maestro/registry.json`) |
| `add` | Registers an agent. `--kind` must be one of: `codex`, `claude_code`, `hermes`, `pi`, `cline`, `openhands`, `cursor`, `copilot`, `a2a_remote`, or `generic`. `--skill` is repeatable. For `generic`: `--command` is required in practice (supports `{prompt}` substitution); `--input-mode stdin` pipes the prompt instead; `--output-format jsonl` enables usage/cost parsing from JSON lines. For `a2a_remote`: `--command` is the daemon URL and `--token` its bearer token |
| `remove` | Unregisters by name; exits 2 if not registered |
| `discover` | Scans `PATH` for known agent CLIs and reports findings (does not register) |
| `status` | Registration + availability check for one agent: binary found, version, (for `a2a_remote`) remote agent card |

## peers

```text
maestro peers list
maestro peers add --name N --url URL
maestro peers remove <peer>
```

Manages the peer roster (`~/.maestro/peers.json`). `add` requires an
`http(s)` URL (exit 2 otherwise); manually added peers are marked `manual` and
never go stale. `list` prints key, status (`live`, `stale <N>s`, or `manual`),
name, and URL per peer; discovered peers go stale after ~15 s of silence.

## budgets

```text
maestro budgets
```

Prints configured caps and current spend from the claim journal: per-agent
totals (with an `(EXHAUSTED)` flag) and today's (UTC) total against the daily
cap. Prints a notice when no caps are configured (`MAESTRO_BUDGET_PER_AGENT_USD`
/ `MAESTRO_BUDGET_DAILY_USD`).

## config

```text
maestro config [--workspace DIR | --project DIR]
```

Prints the effective Codex defaults, work-mode presets, and standing context
entries for the resolved scope as JSON: `{"model": …, "effort": …, "modes":
{NAME: {"implementer": …, "verifier": …, "reviewer": …, "fixer": …,
"max_bounces": N}}, "context": {LABEL: {"label": …, "kind": …, "text"|"path":
…, "phases": […]}}}` — `model`/`effort` may be `null`, `modes` is `{}` when no
presets are defined, and `context` is `{}` when no entries are. Entries carry
their source (`user config` / `project config`) after the config chain merges
them. See [Configuration reference](configuration.md).

## storage

```text
maestro storage migrate-memvara [--workspace DIR | --project DIR]
```

Imports legacy Memvara state into the current backend; prints a JSON summary.

## gc

```text
maestro gc [--days N] [--dry-run]
```

Deletes terminal tasks (phases `COMPLETE`/`FAILED`; canceled tasks land in
`FAILED`) older than `N` days (default 90). Age is taken from the task's state
directory or registry record. `--dry-run` lists what would be deleted without
deleting. Prints a JSON summary (`removed`, `kept`). Manual only — never runs
automatically.
