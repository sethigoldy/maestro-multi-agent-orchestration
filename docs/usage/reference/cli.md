# CLI reference

Complete description of the `maestro` and `maestro-daemon` commands. The CLI is
version 0.12.0; verify with `maestro --version`.

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
| `1` | `task tail <id>`: the followed task ended in a non-`completed` terminal state; a daemon connection failure for `dashboard`; `peers remove` for an unknown peer; `daemon status` when the daemon is **stopped**; a failed `daemon start`/`restart` (spawn/readiness failure); or `skill install`/`uninstall` when an agent operation fails |
| `2` | Usage or runtime error. A message prefixed `maestro:` is printed to stderr (e.g. unknown task, missing daemon, invalid handoff) |
| `130` | `task tail` interrupted by Ctrl-C |

## Daemon resolution

Commands that talk to the daemon (`delegate`, `task tail`, `dashboard`) resolve
the endpoint in this order:

1. `MAESTRO_DAEMON_URL` (optionally with `MAESTRO_DAEMON_TOKEN`).
2. The `daemon.json` marker written by whichever broker started last. The
   marker's process is liveness-checked; a stale marker yields the error
   `no daemon reachable — start one with 'maestro daemon start' (or run 'maestro-daemon' in the foreground) or set MAESTRO_DAEMON_URL`.

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

## maestro daemon

```text
maestro daemon start
maestro daemon stop
maestro daemon status [--json]
maestro daemon restart
```

The lifecycle manager for the background daemon. It drives the same broker as
`maestro-daemon`, but detaches it so no terminal needs to stay open:

| Subcommand | Behavior |
|---|---|
| `start` | Starts the daemon in a detached session (survives shell exit) and returns immediately. Output goes to `<state-dir>/daemon.log`. **Idempotent**: if a live, answering daemon already exists for this state directory it is reused — never duplicated (an advisory lock serializes concurrent starts). If a process exists but does not answer HTTP, `start` refuses rather than stacking a second daemon |
| `stop` | SIGTERM first, then a grace period (`MAESTRO_DAEMON_STOP_GRACE_S`, default 10 s), then SIGKILL if required. Removes the marker on completion and cleans stale markers. **Idempotent**: stopping when nothing is running is not an error |
| `status` | Reports running/stopped with PID, port, URL, state directory, and uptime. Distinguishes *no marker*, *marker but dead process* (stale), and *alive but not answering*. Exit code: `0` running, `1` stopped. `--json` prints the machine-readable form (`running`, `pid`, `port`, `host`, `url`, `state_dir`, `started_at`, `uptime_s`) |
| `restart` | `stop` + `start` with error handling |

The `daemon.json` marker (written by the daemon itself) is the single source of
truth; `maestro daemon start` never keeps a second copy.

## skill

```text
maestro skill list
maestro skill status
maestro skill install [--agent NAME | --all]
maestro skill uninstall [--agent NAME]
```

Manages the global **`maestro-driven-development`** skill — the operational
instructions that make Maestro the default development execution backend for
supported coding agents. Each agent uses its own global mechanism: Claude Code
gets a global Agent Skill (`~/.claude/skills/maestro-driven-development/SKILL.md`);
Codex, GitHub Copilot CLI, Hermes, Pi, Cline, and OpenCode get a managed block
in their global instructions/rules file (OpenCode's is
`~/.config/opencode/AGENTS.md`); Cursor gets a global user rule
(`~/.cursor/rules/maestro-driven-development.mdc`); OpenHands gets the skill
inside `custom_instructions` of `~/.openhands/agent_settings.json`.

| Subcommand | Behavior |
|---|---|
| `list` | JSON: the managed skill, its source path, and every supported agent |
| `status` | JSON array per agent: `kind`, `display_name`, `mechanism`, `path`, `binary`, `detected` (CLI on PATH), `installed`. Codex also reports `legacy_installed`, which is true when an old block is still in `~/.codex/instructions.md`. For agents whose instructions live in one markdown file, an `error` key appears when that file exists but cannot be read, for example because it is not valid UTF-8 text; `installed` is then false because Maestro cannot tell whether its block is there |
| `install` | Installs the skill for every **detected** agent by default. `--agent NAME` targets one agent (adapter kind, binary name, or display name; installed even if not yet detected). `--all` installs for every supported agent regardless of detection. Updates are delete-then-reinstall: the existing skill is removed before the new one is written, so stale files from a previous Maestro version never survive |
| `uninstall` | Removes the skill from every agent where it is installed, or one `--agent NAME`. User-written content around a managed block is preserved; files that only ever contained the managed block are removed. An instructions file that cannot be read or is not valid UTF-8 is never rewritten: `install` and `uninstall` report an error for that agent and leave the file as it is, and the other agents are handled normally |

## delegate

```text
maestro delegate (--file FILE | --title T --request R [--target A | --mode NAME])
                 [--fallback A …] [--design-file FILE]
                 [--context TEXT …] [--context-file PATH …] [--skill DIR …]
                 [--no-wait] [--workspace DIR | --project DIR]
```

Submits a handoff to the daemon and, by default, blocks while streaming the
task's event stream.

| Option | Meaning |
|---|---|
| `--file FILE` | Handoff document file (TOML or JSON; 4-section or legacy format). Mutually exclusive in effect with the flag form — one of the two forms is required |
| `--title T` / `--request R` / `--target A` | Minimal handoff from flags. `--title` and `--request` are required in this form; `--target`/`--mode` are optional — without them the daemon resolves routing from config `[defaults]`, or parks the task with a question if no default is configured |
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
maestro task receipt <task-id|number> [--json]
maestro task continue <task-id> --request "new instruction" [--context reuse|fresh] [--no-wait]
```

Bare `maestro task <n>` is normalized to `task status`. Top-level aliases:
`maestro list …`, `maestro status <id> …`.

| Subcommand | Behavior |
|---|---|
| `list` | Prints JSON array of tasks (`task_id`, number, title, phase, workspace). Without a scope flag or `$MAESTRO_WORKSPACE`: all user-level tasks. With `--project`: tasks of that project. With an explicit `--workspace`: the project's tasks if the path is a project root or a `.claude/worktrees` parent, else exactly that workspace |
| `status` / `show` | Prints one task's state as JSON (see [Inspect tasks and artifacts](../how-to/inspect-tasks-and-artifacts.md#read-one-tasks-state) for the fields). Accepts a full task id or a numeric task number. Unknown references exit 2 |
| `tail` | Live-follows one task's event stream over SSE (no polling). `--all` follows the global stream instead of one task; with `--all` the exit code is always 0 (absent Ctrl-C) |
| `audit` | Prints the durable record as JSON: title, state, workspace, branch, origin/target agents, attempts (agent, ok, exit code, duration, usage, error), the composed context entries (`context`, with their sources), accumulated usage, error, and parsed result files. Works after daemon restarts |
| `receipt` | Prints the **execution receipt** — a human-readable summary (or stable JSON with `--json`) of what happened on one task: final state; per-attempt phase, agent, duration, cost, and ok/error; the deterministic verification result and command; work-mode gate verdicts and bounce count; totals (wall-clock or attempt-sum duration, aggregated cost when any attempt reported one), plus turn count, task-knowledge schema metadata, and continuation context stats for continued tasks. The receipt is a projection of the durable task state — it works for running, completed, failed, and canceled tasks alike, and after daemon restarts. If a daemon is reachable the receipt is served over its API (`GET /tasks/<id>/receipt`); otherwise it is built locally from the state directory. Unknown references exit 2 when no daemon can answer |
| `continue` | Continues a finished task (completed/failed/canceled) with a new instruction: the same task id, workspace, branch, and routing resume — the follow-up turn runs under the handoff's fixer agent when one is pinned. `--context reuse` (default) injects the compact [task-knowledge snapshot](../reference/configuration.md#continuation--task-continuation-context); `--context fresh` starts a clean reasoning context. Blocks and streams the turn like `delegate` unless `--no-wait` prints the submission JSON and returns immediately. Requires a reachable daemon; unknown tasks or exhausted delegation depth exit 2 |

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
maestro agents register-discovered [--dry-run]
maestro agents status <name>
```

| Subcommand | Behavior |
|---|---|
| `list` | JSON array of registered agent specs (user-level, `~/.maestro/registry.json`) |
| `add` | Registers an agent. `--kind` must be one of: `codex`, `claude_code`, `hermes`, `pi`, `cline`, `openhands`, `cursor`, `copilot`, `opencode`, `a2a_remote`, or `generic`. `--skill` is repeatable. For `generic`: `--command` is required in practice (supports `{prompt}` substitution); `--input-mode stdin` pipes the prompt instead; `--output-format jsonl` enables usage/cost parsing from JSON lines. For `a2a_remote`: `--command` is the daemon URL and `--token` its bearer token |
| `remove` | Unregisters by name; exits 2 if not registered |
| `discover` | Scans `PATH` for known agent CLIs and reports findings (does not register) |
| `register-discovered` | Converts discovery results into registrations programmatically: every found CLI that is not registered yet gets a default spec (`source="discovered"`). **Idempotent and conservative** — existing registrations (custom names, models, tokens, skills) are preserved exactly as-is. `--dry-run` reports what would be registered without writing |
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

Prints the effective config for the resolved scope as JSON: Codex defaults,
routing `[defaults]`, work-mode presets, and standing context entries —
`{"model": …, "effort": …, "defaults": {"agent": …, "fallback": […], "model":
…, "effort": …}, "modes": {NAME: {"implementer": …, "verifier": …, "reviewer":
…, "fixer": …, "max_bounces": N}}, "context": {LABEL: {"label": …, "kind": …,
"text"|"path": …, "phases": […]}}}` — `model`/`effort` may be `null`,
`defaults` is `{}` when no `[defaults]` table is configured, `modes` is `{}`
when no presets are defined, and `context` is `{}` when no entries are. Entries
carry their source (`user config` / `project config`) after the config chain
merges them. See [Configuration reference](configuration.md).

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

## doctor

```text
maestro doctor [--workspace DIR | --project DIR] [--json]
```

Diagnoses whether this environment can run Maestro. Fast, deterministic, and
strictly read-only: it never modifies the environment, installs anything, or
changes `PATH`. The report covers:

- **Maestro / Python / system** — version, interpreter, platform.
- **State** — the state directory (`$MAESTRO_HOME` or `~/.maestro`): existence,
  writability, effective config paths, and storage backend; an error line when
  the configuration is invalid or the directory unusable.
- **Daemon** — reachability resolved like the CLI (env URL or liveness-checked
  `daemon.json` marker), with auth status (`none`, `token`, or `missing` on a
  401). A missing daemon is reported, not failed.
- **Git** — installed and versioned.
- **Agents** — every known CLI kind (found/version/status) plus registered
  agents; missing optional agents are reported, never treated as failures.
- **Workspace** — the target directory: git root, project type (python/node/go/
  rust/make), read/write access, and the auto-detected verification command.
- **Budget** — configured caps and current spend.

`--json` prints the report as stable JSON (top-level keys `maestro`, `python`,
`system`, `state`, `daemon`, `git`, `agents`, `workspace`, `budget`, plus `ok`
and, when not ok, `problems`). Exit code: `0` when the environment is usable,
`1` when a genuinely blocking problem exists (unusable state directory, invalid
configuration, or an explicitly passed workspace that does not exist). Missing
optional agents and a missing daemon never affect the exit code. Tokens are
never printed — only whether authentication applies.
