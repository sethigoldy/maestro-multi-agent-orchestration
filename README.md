# Maestro

**Claude supervises. Codex implements. Memvara remembers.**

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
Self-hosted Memvara
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

Claude will call `delegate_to_codex` automatically. That call returns immediately with a task number. Codex runs in the background; the task state and artifacts are persisted in self-hosted Memvara plus `.maestro/tasks/`.

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

Claude normally omits these fields; Maestro applies the defaults. A task can override them with the MCP parameters `model` and `effort`. Supported effort values are `low`, `medium`, `high`, `xhigh`, and `max`. GPT-5.6 Luna supports `max`. The selected values are persisted in Memvara with the task and reported by `task_status`. Codex is invoked with `--model` and `--config model_reasoning_effort=...`. Current Codex CLI exposes both options for `exec`.

## Task identity and recovery

Memvara is the authoritative task registry. `.maestro/tasks.json` is only a cache for fast CLI startup. If it is deleted or becomes stale, `maestro task list` (or the legacy `maestro list`) rebuilds it from Memvara, and numeric references such as `maestro status 6` continue to resolve.

The MCP servers use repository-local launchers that prefer `.venv/bin/python`, so Claude Code and the CLI use the same Python environment when the project has a virtualenv.

## Shared memory

Every repository gets a project-local store:

```text
.maestro/
├── memory.db
├── tasks.json
├── designs/
└── tasks/
```

Maestro explicitly uses `NullLLM()` for its internal state store because orchestration state is written as structured Memvara claims. It does not need Memvara to extract arbitrary prose just to persist lifecycle state.

Claude Code and Codex use the same project Memvara store when connected through the repository's MCP configuration, so the approved design, decisions, implementation evidence, verification results, and review history are shared.

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

There is one supervisor (Claude), one primary implementation agent (Codex), deterministic verification, and a durable memory/state layer (Memvara). A future version can add specialist agents without changing the user-facing workflow.


## Git worktrees (important)

Maestro never uses the MCP server process's current working directory as the project identity. Claude must resolve the active worktree with:

```bash
git rev-parse --show-toplevel
```

and pass that absolute path as `workspace` on every Maestro MCP call. This matters because Claude Code can switch between Git worktrees while the long-lived Maestro MCP process remains started from an earlier worktree.

Codex is always launched with the task's explicit `workspace`, and task artifacts, logs, designs, and verification reports are written there. Maestro never `cd`s based on its own startup directory, never commits, and never installs dependencies.

## Verification behavior

Maestro does not repair or provision environments. It runs `make check` when the repository has a Makefile; otherwise it uses pytest when the repository has Python test configuration. A setup/dependency/network failure is recorded as verification evidence and returned to Claude for interpretation.

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

Project discovery includes the project root and each immediate `.claude/worktrees/*` directory that already contains Maestro state (`.maestro/memory.db` or `.maestro/tasks.json`).
