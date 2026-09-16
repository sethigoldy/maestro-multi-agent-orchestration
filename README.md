# Maestro

**Claude supervises. Codex implements. Maestro stores state locally.**

Maestro is a local multi-agent orchestration layer designed to run **behind Claude Code**. The normal user experience is simply talking to Claude. Claude researches and designs, calls Maestro automatically, Maestro starts Codex in the background, verification runs, and Claude reviews the result. Rejected reviews automatically create a new Codex fix pass.

## The user experience

```text
You
 │
 ▼
Claude Code
 │ design
 │
 │ delegate_to_codex()
 ▼
Maestro
 │ persist task + design
 ▼
Local filesystem state
 │ shared durable state
 ▼
Codex (background)
 │ implement
 ▼
Maestro
 │ git diff --check + pytest
 ▼
Claude Code
 │ review
 ├─ approved ──► complete
 └─ changes ───► Maestro → Codex fix → verify → Claude review
```

You normally **do not run `maestro` yourself**.

## Install

Python 3.11+ is recommended.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

Make sure both coding CLIs are installed and authenticated:

```bash
claude --version
codex --version
```

The project `.mcp.json` exposes Maestro to Claude Code. Approve the project MCP server once when Claude asks.

## Normal use

Start Claude Code in this repository and say:

```text
Build the new semantic-search feature. Inspect the existing architecture,
create an implementation design first, then delegate the implementation,
verify it, and review the result.
```

Claude will call `delegate_to_codex` automatically. That call returns immediately with a task number. Codex runs in the background; the task state and artifacts are persisted directly under `.maestro/`.

When Claude checks later, it calls `task_status`. If Claude rejects the implementation, it calls `review_task(..., approved=false, ...)`; Maestro starts Codex again with the review findings and re-runs verification automatically.

## CLI (optional)

The CLI is only for debugging, CI, and operators:

```bash
maestro list
maestro status 1
maestro status task-20260916-120000-a1b2c3
maestro handoff --title "..." --request "..." --design-file .maestro/designs/feature.md
maestro run 1
```

`1` is a human-friendly task number. You do not need to remember UUID-like IDs.

## Codex model and reasoning effort

Set task defaults in the project root `.maestro/config.toml` (for example, in the main repository root; Claude worktree sessions inherit it):

```toml
[codex]
model = "gpt-5.6-luna"
effort = "max"
```

Claude normally omits these fields; Maestro applies the defaults. A task can override them with the MCP parameters `model` and `effort`. Supported effort values are `low`, `medium`, `high`, `xhigh`, and `max`. GPT-5.6 Luna supports `max`. The selected values are persisted in `.maestro/state.jsonl` with the task and reported by `task_status`. Codex is invoked with `--model` and `--config model_reasoning_effort=...`. Current Codex CLI exposes both options for `exec`.

Config precedence is: user-level `~/.maestro/config.toml`, then project-root `.maestro/config.toml`, then the active worktree `.maestro/config.toml`. More specific project/worktree values override user defaults.

## Task identity and recovery

The filesystem state in `.maestro/state.jsonl` is authoritative for task state. `.maestro/tasks.json` is a rebuildable compatibility index. If the index is deleted or becomes stale, `maestro task list` (or the legacy `maestro list`) rebuilds it from the local state file, and numeric references such as `maestro status 6` continue to resolve.

The MCP servers use repository-local launchers that prefer `.venv/bin/python`, so Claude Code and the CLI use the same Python environment when the project has a virtualenv.

## User-level task state

Maestro 0.7 stores task identity and lifecycle state at the user level instead of creating a task registry in every Claude worktree. This makes `maestro task list` independent of worktree-local registry files and keeps task history available even when worktrees are created or removed.

Default locations:

```text
macOS / Linux: ~/.maestro/
Windows:       %LOCALAPPDATA%\Maestro\
```

Set `MAESTRO_HOME` to override the location on any platform. The user-level directory contains the authoritative task registry/state, configuration, locks, and migration markers. Actual designs, Codex results, handoff files, and verification reports remain in each task's workspace under `.maestro/`.

```text
~/.maestro/
├── state.jsonl
├── registry.json
├── registry.lock
├── config.toml
├── tasks/
└── migrations/

project/.claude/worktrees/<worktree>/.maestro/
├── staged/
├── designs/
└── tasks/
```

From the project root, this is now enough to list tasks across all Claude worktrees:

```bash
MAESTRO_WORKSPACE=/path/to/project maestro task list
```

You can also list everything for the current user with no workspace selector:

```bash
maestro task list
```

## Migrating from 0.6.x

Maestro 0.7 automatically imports the 0.6 project-level `project-state.jsonl` journal into the user-level registry the first time that project is opened. The migration is idempotent and preserves the original task number as `legacy_task_number` while assigning a globally unique user-level task number.

If an older release left a worktree-local `~/.maestro/memory.db`, Maestro can also import its Maestro task claims into the selected filesystem backend. This does not switch the project back to Memvara.

## Local task artifacts

Task artifacts remain workspace-local so designs and implementation evidence stay alongside the code they describe. User-level state contains metadata and references, not a second copy of the full design/result files.

## Using Memvara with Maestro

Filesystem storage is the default and requires no external memory service. Memvara is an optional user-level storage backend for users who want Maestro task state persisted through Memvara instead of `~/.maestro/state.jsonl`.

### Install the optional Memvara dependency

Install Maestro with the `memvara` extra:

```bash
python -m pip install -e ".[memvara]"
```

Or, for a normal package installation:

```bash
python -m pip install "maestro[memvara]"
```

### Select the Memvara backend

Set the backend in `.maestro/config.toml`:

```toml
[storage]
backend = "memvara"
```

You can also select it for the current shell with:

```bash
export MAESTRO_STORAGE=memvara
```

The configuration file is the persistent choice; `MAESTRO_STORAGE` is useful for temporary overrides and CI/testing. The supported values are `filesystem` and `memvara`. Filesystem remains the default when neither is configured.

### Memvara MCP configuration

When Claude also needs direct access to Memvara for broader semantic/project memory, expose Memvara as a separate MCP server in the project `.mcp.json`. A typical setup is:

```json
{
  "mcpServers": {
    "maestro": {
      "command": "python3",
      "args": ["-m", "maestro.mcp_server"]
    },
    "memvara": {
      "command": "python3",
      "args": ["-m", "memvara.server"],
      "env": {
        "MEMVARA_DB": ".maestro/memory.db",
        "MEMVARA_USER": "developer",
        "MEMVARA_TENANT": "default"
      }
    }
  }
}
```

The exact Memvara server options can depend on the Memvara version you install. Maestro's `memvara` storage backend and the Memvara MCP server are separate concerns: the backend stores Maestro's task state, while the MCP server can give Claude direct semantic-memory tools.

### When to use each backend

Use **filesystem** for the simplest, fully local setup, easy inspection/debugging, offline use, and CI. Use **Memvara** when you specifically want Maestro's task state to participate in a shared Memvara-backed memory layer. You can switch back at any time with:

```bash
export MAESTRO_STORAGE=filesystem
```

or:

```toml
[storage]
backend = "filesystem"
```

## Architecture

- `maestro/core.py` — task identity, numbering, state, delegation and review lifecycle
- `maestro/worker.py` — detached Codex implementation/fix worker and deterministic verification
- `maestro/mcp_server.py` — tools used automatically by Claude Code
- `maestro/cli.py` — optional human/operator CLI
- `.claude/skills/maestro/SKILL.md` — Claude's automatic orchestration behavior
- `.mcp.json` — project MCP configuration
- `CLAUDE.md` — supervisor rules
- `AGENTS.md` — Codex implementation rules

### Conceptual model

There is one supervisor (Claude), one primary implementation agent (Codex), deterministic verification, and a durable local filesystem state layer. A future version can add specialist agents without changing the user-facing workflow.


## Git worktrees (important)

Maestro never uses the MCP server process's current working directory as the project identity. Claude must resolve the active worktree with:

```bash
git rev-parse --show-toplevel
```

and pass that absolute path as `workspace` on every Maestro MCP call. This matters because Claude Code can switch between Git worktrees while the long-lived Maestro MCP process remains started from an earlier worktree.

Codex is always launched with the task's explicit `workspace`, and task artifacts, logs, designs, and verification reports are written there. Maestro never `cd`s based on its own startup directory, never commits, and never installs dependencies.

## Verification behavior

Maestro does not repair or provision environments. Verification is deterministic and environment-aware:

1. An explicit `[verification]` command in `.maestro/config.toml` wins.
2. Otherwise Maestro uses `make check` when a Makefile exists.
3. Node projects with a `test` script use npm/pnpm/yarn according to the lockfile. Go uses `go test ./...`; Rust uses `cargo test`.
4. Python projects use the workspace `.venv` (or `MAESTRO_PYTHON`, then the Maestro interpreter) and run pytest only when pytest is actually installed.
5. When no runnable test command is available, Maestro records a verification note and falls back to `git diff --check` rather than falsely reporting a test failure.

A real non-zero result from the selected test command is still recorded as a verification failure. Maestro never installs dependencies or changes the environment.

For repositories with a precise check, configure it explicitly:

```toml
[verification]
command = ["make", "check"]
```

## Low-token handoff staging

Claude does not send the full design as an MCP argument. It writes the design once under `.maestro/staged/` and sends Maestro only a tiny JSON descriptor. The staged descriptor survives tool, validation, or subprocess failures, so the exact handoff can be retried without regenerating or re-sending the design. On successful launch Maestro archives the descriptor under the task directory.

## Project-level task listing

When `MAESTRO_WORKSPACE` points at the project root, `maestro task list` scans all Claude worktrees and merges task records from `.maestro/tasks`, `tasks.json`, and `state.jsonl`. This means a task can appear even while Claude is still writing its normal registry/index state.

```bash
MAESTRO_WORKSPACE=/path/to/project maestro task list
MAESTRO_WORKSPACE=/path/to/project maestro task task-<id>
```

An explicit worktree path still limits inspection to that one workspace.

## CLI workspace selection

The CLI operates on the target repository's `.maestro` state. When running Maestro from
a separate checkout (for example the Maestro source repository itself), pass the target
workspace explicitly or set `MAESTRO_WORKSPACE`: 

```bash
maestro task list --workspace /path/to/your/project
maestro task status 20260916-134921-245a01 --workspace /path/to/your/project

export MAESTRO_WORKSPACE=/path/to/your/project
maestro task list
```

`maestro task show <id>` is an alias for `maestro task status <id>`. The legacy
`maestro list` and `maestro status <id>` commands remain supported.

### CLI workspace and project discovery (0.5.5)

The CLI can target the same workspace that Claude/MCP uses:

```bash
export MAESTRO_WORKSPACE=/path/to/project/.claude/worktrees/my-task
maestro task list
maestro task status 10
maestro task 10
```

`MAESTRO_WORKSPACE` is used when `--workspace` is omitted. An explicit `--workspace` takes precedence over the environment variable.

To inspect a project and all existing Maestro workspaces under `.claude/worktrees/` without creating new task databases:

```bash
maestro task list --project /path/to/project
maestro task status <task-id> --project /path/to/project
```

Project discovery includes the project root and each immediate `.claude/worktrees/*` directory that already contains Maestro state (`.maestro/state.jsonl` or `.maestro/tasks.json`).

## Project-root task discovery

When `MAESTRO_WORKSPACE` points at the project root, Maestro automatically discovers tasks across the Git worktrees used by Claude. Discovery uses `git worktree list` and also checks `.claude/worktrees/*`, so newly-created or partially-initialized worktrees are visible without passing their individual path.

```bash
export MAESTRO_WORKSPACE=/path/to/project
maestro task list
```

A specific worktree can still be queried directly with `--workspace`.


## Storage backends

Maestro 0.7 stores task identity and lifecycle state at **user scope**. This is the authoritative task registry, so listing tasks no longer requires scanning every Claude worktree.

On macOS/Linux the default directory is `~/.maestro/`. On Windows it is `%LOCALAPPDATA%\Maestro`. Set `MAESTRO_HOME` to override it on any OS. The registry and task state live there; designs, logs, Codex results, and verification artifacts remain in each task workspace under `.maestro/`.

### Filesystem (default)

```toml
[storage]
backend = "filesystem"
```

### Memvara (optional)

```bash
pip install "maestro[memvara]"
```

```toml
[storage]
backend = "memvara"
```

Or set `MAESTRO_STORAGE=memvara`. The Memvara backend also uses user-level task state, so switching worktrees does not change task visibility.

### Task listing

From a project root:

```bash
MAESTRO_WORKSPACE=/path/to/project maestro task list
```

That shows tasks for the project, regardless of which `.claude/worktrees/*` created them. A direct worktree path scopes the list to that worktree. You can also use `--project /path/to/project` explicitly.

### Migrating from 0.6.x

The first 0.7 run for a project imports the 0.6 `.maestro/project-state.jsonl` task journal into `~/.maestro/`. Existing local task numbers are retained as `legacy_task_number`; 0.7 assigns unique user-level task numbers. The migration is one-time per project.
## 0.8.1

Fixes the `codex_followup` worker action mismatch. The worker CLI now accepts `followup` and executes the Codex follow-up path correctly. Maestro also detects an immediate worker exit and records the task as failed with the exit code and log path instead of reporting a successful dispatch.

