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

Set task defaults in the tracked `.maestro/config.toml`:

```toml
[codex]
model = "gpt-5.6-luna"
effort = "max"
```

Claude normally omits these fields; Maestro applies the defaults. A task can override them with the MCP parameters `model` and `effort`. Supported effort values are `low`, `medium`, `high`, `xhigh`, and `max`. GPT-5.6 Luna supports `max`. The selected values are persisted in `.maestro/state.jsonl` with the task and reported by `task_status`. Codex is invoked with `--model` and `--config model_reasoning_effort=...`. Current Codex CLI exposes both options for `exec`.

## Task identity and recovery

The filesystem state in `.maestro/state.jsonl` is authoritative for task state. `.maestro/tasks.json` is a rebuildable compatibility index. If the index is deleted or becomes stale, `maestro task list` (or the legacy `maestro list`) rebuilds it from the local state file, and numeric references such as `maestro status 6` continue to resolve.

The MCP servers use repository-local launchers that prefer `.venv/bin/python`, so Claude Code and the CLI use the same Python environment when the project has a virtualenv.

## Local state

Every repository gets a project-local state store with no database service or external memory dependency:

```text
.maestro/
├── state.jsonl
├── tasks.json
├── config.toml
├── staged/
├── designs/
└── tasks/
```

`state.jsonl` is an append-only, human-inspectable record of lifecycle claims and events. `tasks.json` is a rebuildable index for compatibility and fast CLI listing. Designs, Codex output, verification reports, and handoff descriptors remain regular files under `.maestro/`.

Claude and Codex share the same explicit worktree, so the approved design, task state, implementation evidence, verification results, and reviews are all available from the same local filesystem.

## Using Memvara with Maestro

Filesystem storage is the default and requires no external memory service. Memvara is an optional storage backend for users who want Maestro task state persisted through Memvara instead of the local `.maestro/state.jsonl` store.

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
