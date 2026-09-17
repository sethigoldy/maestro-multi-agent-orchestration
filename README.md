# Maestro

**Claude supervises. Codex implements. Maestro keeps the work durable.**

Maestro is a local multi-agent orchestration layer designed to run behind [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

You normally talk only to Claude:

```text
You
 │
 ▼
Claude Code
 │
 │ understands request
 │ creates compact design
 │
 ▼
Maestro
 │
 │ persists task state
 │ starts Codex
 │
 ▼
Codex
 │
 │ implements
 │ runs focused checks
 │
 ▼
Maestro
 │
 │ deterministic verification
 │
 ▼
Claude Code
 │
 ├── approve → complete
 │
 └── reject → Codex follow-up/fix → verify → review
```

Claude is the supervisor and reviewer. Codex owns implementation, testing, debugging, refactoring, and mechanical changes. Maestro provides the durable task lifecycle and the bridge between them.

You normally **do not need to run Maestro manually**.

---

## Multi-agent platform (0.9)

Maestro now delegates to **any registered agent** — Codex, Claude Code, pi, Cline,
Hermes Agent, Cursor, OpenHands, or any CLI via the generic spec — with every host
agent (Claude, Kilo, Cline, ...) able to call any other through the same MCP tools.

- **Local broker daemon** (`maestro-daemon`): A2A-aligned task API
  (agent card at `GET /.well-known/agent.json`, JSON-RPC `message/send` /
  `tasks/get` / `tasks/cancel`, SSE event streams), FIFO routing with one active
  task per workspace, retry → fallback chain → escalation, per-task branches.
- **Reactive by design — no polling**: everything completes over the event bus;
  MCP `delegate`/`task_wait` block on it, dashboards stream SSE.
- **Observability**: web dashboard at `http://127.0.0.1:<port>/`,
  `maestro task tail <id>` (live), `maestro task audit <id>` (durable),
  `maestro gc` (TTL 90 days, manual only).
- **Flagship demo**: `examples/full-swap.sh` — the full role swap, one command.

See [docs/architecture-proposal.md](docs/architecture-proposal.md) for the design
and [docs/agent-onboarding.md](docs/agent-onboarding.md) for onboarding agents
without a first-class adapter.

---

## 1. What you need

Before installing Maestro, make sure you have:

* Python **3.11 or newer**
* [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
* [Codex CLI](https://github.com/openai/codex)

Check that Claude and Codex are available:

```bash
claude --version
codex --version
```

Authenticate both CLIs using their normal setup flow.

---

## 2. Install Maestro

Clone the repository and enter it:

```bash
git clone https://github.com/sethigoldy/multi-agent-orchestration.git
cd multi-agent-orchestration
```

Create a virtual environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Upgrade packaging tools and install Maestro:

```bash
python -m pip install -U pip
python -m pip install -e .
```

Verify the installation:

```bash
maestro --version
```

---

## 3. Configure Claude Code

Maestro is exposed to Claude Code through the project's `.mcp.json`.

The repository should contain:

```json
{
  "mcpServers": {
    "maestro": {
      "command": "scripts/maestro-mcp"
    }
  }
}
```

The launcher uses the repository's virtual environment:

```text
<maestro-repository>/.venv/bin/python
```

so Claude Code and your local Maestro CLI use the same Python environment.

Make sure the launcher is executable:

```bash
chmod +x scripts/maestro-mcp
```

You can verify it exists:

```bash
ls -l scripts/maestro-mcp
```

The project also contains `.claude/settings.json` with permission for Maestro's MCP tools.

Start Claude Code from the repository/project where you want to use Maestro. Approve the project MCP server when Claude Code asks.

---

## 4. Configure Codex defaults

Project defaults belong in:

```text
.maestro/config.toml
```

A typical configuration is:

```toml
[codex]
model = "gpt-5.6-luna"
effort = "max"

[storage]
backend = "filesystem"
```

Supported reasoning effort values are:

```text
low
medium
high
xhigh
max
```

You can also configure deterministic verification:

```toml
[verification]
command = ["make", "check"]
```

Configuration precedence is:

```text
~/.maestro/config.toml
        ↓
<project-root>/.maestro/config.toml
        ↓
<active-worktree>/.maestro/config.toml
```

More specific configuration overrides less specific configuration.

---

## 5. Your first Maestro task

Start Claude Code in the project you want to modify.

Then ask Claude for normal implementation work, for example:

```text
Implement semantic search for the existing document API.

First inspect the repository and understand the current architecture.
Create a compact implementation design, then delegate the implementation
to Maestro/Codex. Run the relevant tests and review the resulting diff.
```

Claude will:

1. Inspect the target project.
2. Create a compact implementation handoff.
3. Call Maestro through MCP.
4. Maestro creates a durable task.
5. Codex runs in the active workspace.
6. Maestro performs deterministic verification.
7. Claude reviews the implementation.
8. Claude can send additional work to Codex through `codex_followup`.
9. Claude makes the final approval decision.

You do not need to manually copy prompts between Claude and Codex.

---

## 6. How worktrees are handled

Claude Code can work in Git worktrees such as:

```text
project/
└── .claude/
    └── worktrees/
        └── feature-search/
```

Maestro preserves the active worktree explicitly.

A task records both:

```text
workspace
project_root
```

For example:

```text
workspace   = /path/to/project/.claude/worktrees/feature-search
project_root = /path/to/project
```

Codex runs in the task's `workspace`.

Maestro does **not** use the MCP server's own current working directory as the task workspace.

Claude should pass the active absolute worktree path as the `workspace` argument on every Maestro MCP call.

---

## 7. Where Maestro stores state

Maestro keeps its authoritative runtime state at the **user level**, not in every Claude worktree.

Default locations:

```text
macOS / Linux:
~/.maestro/

Windows:
%LOCALAPPDATA%\Maestro\
```

You can override this location:

```bash
export MAESTRO_HOME=/path/to/maestro-state
```

The user-level directory contains:

```text
~/.maestro/
├── registry.json
├── state.jsonl
├── registry.lock
├── config.toml
├── designs/
├── staged/
├── tasks/
└── migrations/
```

The important distinction is:

```text
~/.maestro/
    persistent Maestro runtime state and task artifacts

<project>/.maestro/config.toml
    project configuration

<active-worktree>/.maestro/config.toml
    optional worktree-specific configuration
```

Runtime task state and artifacts are **not recreated inside every worktree**.

The task record still remembers which worktree was used so Codex can continue working in the correct location.

---

## 8. Task listing and recovery

Task identity is stored in the user-level Maestro registry, so task lookup does not depend on keeping a separate registry inside every worktree.

List all tasks available to the current user:

```bash
maestro task list
```

List tasks for a project:

```bash
maestro task list --project /path/to/project
```

List tasks using a specific workspace:

```bash
maestro task list --workspace /path/to/project/.claude/worktrees/feature-search
```

You can also use:

```bash
export MAESTRO_WORKSPACE=/path/to/project
maestro task list
```

A task can be referenced by either its human-friendly number or task ID:

```bash
maestro task status 10
```

```bash
maestro task status task-20260917-120000-a1b2c3
```

These are equivalent aliases:

```bash
maestro task show 10
maestro status 10
```

The shorthand below is also supported:

```bash
maestro task 10
```

---

## 9. Optional CLI usage

The normal workflow is Claude → Maestro MCP → Codex, but the CLI is useful for debugging, CI, and operators.

### List tasks

```bash
maestro task list
```

### Show a task

```bash
maestro task status 10
```

### Run an implementation task

```bash
maestro run 10
```

### Send a follow-up directly to Codex

```bash
maestro codex-followup 10 "Fix the failing integration test and rerun the relevant checks."
```

### Show effective Codex configuration

```bash
maestro config
```

### Create a manual handoff

```bash
maestro handoff \
  --title "Add semantic search" \
  --request "Implement semantic search for the document API" \
  --design-file path/to/design.md
```

### Multi-agent commands (0.9)

```bash
# Start the local broker daemon (prints its port; writes daemon.json)
maestro-daemon

# Delegate a handoff to any registered agent (blocks, live-streaming output)
maestro delegate --title "Add semantic search" \
  --request "Implement semantic search for the document API" \
  --target codex --fallback hermes --workspace /path/to/repo

# Or from a handoff document file (TOML or JSON)
maestro delegate --file handoff.toml --workspace /path/to/repo

# Live-tail one task (or the global stream with --all)
maestro task tail task-20260917-120000-ab12cd
maestro task tail --all

# Durable audit record: attempts, usage/cost, errors, result files
maestro task audit task-20260917-120000-ab12cd

# Garbage collection: terminal tasks older than the TTL (manual only)
maestro gc --days 90 --dry-run
maestro gc --days 90
```

Register agents with `maestro agents add` / `maestro agents discover` (see
[docs/agent-onboarding.md](docs/agent-onboarding.md)).

The CLI is optional. Claude normally handles this through MCP.

---

## 10. Deterministic verification

After Codex finishes, Maestro performs deterministic verification.

The selection order is:

1. An explicit `[verification]` command in `.maestro/config.toml`.
2. `make check` when a `Makefile` exists.
3. Node projects with a `test` script using npm, pnpm, or yarn according to the lockfile.
4. Go projects using `go test ./...`.
5. Rust projects using `cargo test`.
6. Python projects using the selected Python environment and pytest when pytest is installed.
7. Otherwise Maestro falls back to `git diff --check`.

Example:

```toml
[verification]
command = ["make", "check"]
```

Maestro does **not** install dependencies, repair the environment, or silently change the project's tooling.

A real non-zero result from the selected verification command is recorded as a verification failure.

---

## 11. Codex follow-ups and reviews

Claude uses `codex_followup` for additional implementation work such as:

* fixing failed tests
* debugging
* refactoring
* implementing review findings
* making mechanical changes

Claude remains responsible for the final review.

The intended loop is:

```text
Codex implementation
        ↓
verification
        ↓
Claude review
        ↓
approved ───────────→ complete

rejected
        ↓
codex_followup
        ↓
verification
        ↓
Claude review again
```

Maestro does not automatically decide that a verification failure requires an implementation change. Verification is evidence; Claude makes the review decision.

---

## 12. Optional Memvara backend

Filesystem storage is the default and requires no additional memory service.

Memvara is optional.

### Install the extra

```bash
python -m pip install -e ".[memvara]"
```

### Select Memvara

In `.maestro/config.toml`:

```toml
[storage]
backend = "memvara"
```

Or temporarily for the current shell:

```bash
export MAESTRO_STORAGE=memvara
```

Supported values are:

```text
filesystem
memvara
```

Filesystem remains the default.

The Maestro Memvara backend and the Memvara MCP server are separate concepts:

```text
Maestro Memvara backend
    → stores Maestro's own task state

Memvara MCP server
    → optionally gives Claude direct access to broader semantic memory
```

You do not need the Memvara MCP server to use Maestro.

---

## 13. Migrating from older Maestro releases

Maestro can migrate legacy project-level task state into the user-level state directory.

Older project journals such as:

```text
.maestro/project-state.jsonl
```

can be imported into:

```text
~/.maestro/
```

Migration is designed to be idempotent.

Legacy Memvara task state can also be imported into the selected filesystem backend when applicable.

New installations should use the current user-level state model and do not need to create old project-local task registries.

---

## 14. Repository configuration files

The repository uses these files for its orchestration setup:

```text
.maestro/config.toml
    Project/worktree Maestro configuration

.mcp.json
    Claude Code MCP configuration

.claude/settings.json
    Claude Code MCP permissions

.claude/skills/maestro/SKILL.md
    Claude's Maestro routing instructions

CLAUDE.md
    Claude supervisor rules

AGENTS.md
    Codex implementation rules
```

Maestro's source code is under:

```text
maestro/
```

and the optional MCP launcher is:

```text
scripts/maestro-mcp
```

---

## 15. Architecture

The core components are:

```text
maestro/core.py
    Task identity, numbering, persistence, configuration,
    delegation, lifecycle, review, and migration.

maestro/worker.py
    Background Codex execution and deterministic verification.

maestro/mcp_server.py
    MCP tools used by Claude Code.

maestro/cli.py
    Optional human/operator CLI.

scripts/maestro-mcp
    Selects the repository Python environment and starts the MCP server.

.claude/skills/maestro/SKILL.md
    Minimal Claude routing contract.

CLAUDE.md
    Supervisor behavior.

AGENTS.md
    Implementation-agent rules.
```

Conceptually:

```text
                   ┌─────────────────┐
                   │   Claude Code   │
                   │   Supervisor    │
                   └────────┬────────┘
                            │ MCP
                            ▼
                   ┌─────────────────┐
                   │     Maestro     │
                   │ Task lifecycle  │
                   │ Durable state   │
                   │ Verification    │
                   └────────┬────────┘
                            │ subprocess
                            ▼
                   ┌─────────────────┐
                   │      Codex      │
                   │  Implementation │
                   │ Tests/debugging │
                   └────────┬────────┘
                            │
                            ▼
                   ┌─────────────────┐
                   │ Active Git      │
                   │ Worktree        │
                   └─────────────────┘

                   Persistent state:
                   ~/.maestro/
```

There is one supervisor (Claude), one primary implementation agent (Codex), deterministic verification, and one durable Maestro state layer.

---

## 16. Troubleshooting

### Claude cannot start Maestro MCP

Check:

```bash
ls -l scripts/maestro-mcp
```

Make it executable:

```bash
chmod +x scripts/maestro-mcp
```

Make sure the virtual environment exists:

```bash
ls -l .venv/bin/python
```

If it does not:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### Maestro is using the wrong Python

Verify:

```bash
.venv/bin/python -c 'import sys, maestro; print(sys.executable); print(maestro.__file__)'
```

The MCP server should be launched through:

```text
scripts/maestro-mcp
```

rather than a globally installed `python3` environment.

### Codex is not available

Check:

```bash
codex --version
```

and authenticate Codex using its normal CLI setup.

Maestro does not install Codex for you.

### A task cannot be found

Check the global registry:

```bash
maestro task list
```

You can also inspect:

```text
~/.maestro/
```

or set an explicit state directory:

```bash
export MAESTRO_HOME=/path/to/maestro-state
```

### Check the active workspace

```bash
git rev-parse --show-toplevel
```

Claude should pass the actual active worktree path to Maestro MCP.

---

## 17. Development

Install development dependencies:

```bash
python -m pip install -e .
python -m pip install pytest coverage
```

Run the tests:

```bash
python -m pytest -q
```

Run tests with branch coverage:

```bash
python -m coverage run --branch -m pytest -q
python -m coverage report --fail-under=100
```

The project CI enforces 100% line and branch coverage.

Generated files such as these should not be committed:

```text
build/
dist/
*.egg-info/
.venv/
.pytest_cache/
.coverage
.DS_Store
```

---

## 18. Design principles

Maestro follows a few simple rules:

**Claude decides what should be built.**

**Codex owns implementation.**

**Maestro owns task lifecycle and durable orchestration state.**

**Verification is deterministic.**

**Claude owns the final review and approval.**

**Maestro never commits code.**

**Maestro never installs project dependencies.**

**The active worktree is explicit.**

**Task state survives worktree creation, switching, and deletion.**
