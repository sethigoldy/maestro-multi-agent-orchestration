# Maestro

**Delegate coding work to any agent — and keep track of it all.**

Maestro is a local orchestration layer that runs coding agents (Codex, Claude
Code, the GitHub Copilot CLI, Cursor, OpenHands, …) on your behalf. It gives you:

- **Durable tasks** — every delegation is recorded with its full history
  (attempts, output, cost, errors) and survives restarts.
- **Any agent as a target** — first-class adapters for the major CLIs, plus a
  generic spec for anything else, REST task servers, and even *another Maestro*
  on another machine.
- **Automatic fallback** — if an agent fails or isn't available, Maestro tries
  your fallback chain instead of dying.
- **Live visibility** — a web console, a terminal dashboard, and live-tail
  commands, all event-driven (no polling).
- **Cost tracking and budget caps** — usage is recorded per attempt; optional
  per-agent and daily USD caps block new work when the budget is spent.

You can use Maestro two ways:

| Path | Who it's for | How |
|---|---|---|
| **Through Claude Code** | You want an agent to supervise agents | Claude calls Maestro over MCP; you just talk to Claude |
| **Directly** | You want to drive it yourself (CI, ops, no supervisor) | Start the daemon, use the `maestro` CLI or the web console |

Both paths share the same daemon, state, and commands. This guide covers both,
starting with the direct path since it's the foundation.

---

## Quickstart (5 minutes)

### 1. Install

Requirements: **Python 3.11+**, and at least one coding-agent CLI you want to
use (e.g. `codex`, `claude`, `copilot`) installed and authenticated with its
normal setup flow.

```bash
git clone https://github.com/sethigoldy/maestro-multi-agent-orchestration.git
cd maestro-multi-agent-orchestration
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .
maestro --version                  # sanity check
```

### 2. See which agents you can use

```bash
maestro agents discover            # scans your PATH for known agent CLIs
maestro agents list                # shows what's registered
```

Registration is per-user and lives in `~/.maestro`. Built-in kinds can be used
as `--target` directly without any registration — the daemon resolves them to
their default binary. Register an agent when you want a custom name or settings:

```bash
# A CLI with a first-class adapter, under your own name (e.g. custom binary/model):
maestro agents add my-copilot --kind copilot

# Any other CLI, via the generic spec (see "Onboarding any CLI"):
maestro agents add openclaw --kind generic \
  --command "openclaw agent exec --json --message-file -" \
  --input-mode stdin --output-format jsonl
```

`maestro agents status <name>` shows whether an agent is ready (binary found,
version checked) before you delegate to it.

### 3. Start the daemon

The daemon is the local broker: it runs tasks, streams events, and serves the
web console.

```bash
maestro-daemon                     # prints its port; writes ~/.maestro/daemon.json
```

Leave it running in a terminal (or a service manager). `--port 0` picks a free
port; `--state-dir DIR` points it at an alternate state directory.

### 4. Delegate your first task

```bash
maestro delegate \
  --title "Fix the failing test" \
  --request "Find and fix the failing unit test in tests/, then rerun the suite." \
  --target codex \
  --fallback copilot \
  --workspace /path/to/your/repo
```

This blocks and streams the agent's output live. Add `--no-wait` to return
immediately with the task id, or point at a handoff file instead of flags:

```bash
maestro delegate --file handoff.toml --workspace /path/to/your/repo
```

### 5. Watch it work

While a task runs (or after it finishes):

```bash
# Terminal dashboard — full-screen, live (j/k move, q quits)
maestro dashboard

# Live-tail one task's output stream (or --all for everything)
maestro task tail <task-id>

# The durable record: attempts, usage/cost, errors, result files
maestro task audit <task-id>
```

And in a browser: **`http://127.0.0.1:<port>/`** — the web console shows all
tasks, live output, costs, and per-attempt detail (see the port printed by
`maestro-daemon`).

That's the whole loop: **register agents → start daemon → delegate → watch.**

---

## Core concepts (plain terms)

- **Daemon** — one long-running process (`maestro-daemon`) that owns a state
  directory and an HTTP API. Everything else talks to it. One daemon per
  machine is the norm; several can coexist with different `--state-dir`s.
- **Handoff** — the work order: title, request, optional design, target agent,
  fallback chain, verification policy. You create one via CLI flags, a TOML/JSON
  file, or (in the MCP path) Claude creates it for you.
- **Task lifecycle** — each handoff becomes a task that moves through
  `submitted → working → completed` (or `failed` / `canceled`). One active task
  per workspace; extra work queues FIFO and starts when the slot frees.
- **Fallback chain** — `--target codex --fallback copilot --fallback hermes`
  means: try Codex; if it fails or isn't available, try Copilot; then Hermes.
  Each attempt is recorded, including its cost (failed work still costs money).
- **Verification** — after an agent finishes, Maestro runs a deterministic check
  (your `[verification]` command, or an auto-detected test suite) and records
  the result. See "Deterministic verification" below.
- **Usage/cost** — adapters extract usage from each run (tokens, cost where the
  CLI reports it). Costs accumulate on the task and per agent, which is what
  budget caps enforce against.

---

## Watching work

| Tool | What it's for |
|---|---|
| **Web console** — `http://127.0.0.1:<port>/` | Full picture: all tasks, live SSE output, costs, per-attempt detail. React app served by the daemon itself. |
| **`maestro dashboard`** | Terminal full-screen view of the same data. Keys: `j`/down and `k`/up to move, `q`/Esc/Ctrl-C to quit. Pure event-streaming — it never polls. |
| **`maestro task tail <id>`** | Follow one task's output stream in your terminal. `--all` follows everything. |
| **`maestro task audit <id>`** | The durable record: every attempt (agent, exit code, duration, usage, error), the final state, and result files. Works after restarts. |
| **`maestro gc`** | Delete finished tasks older than a TTL (default 90 days). Manual only — `--dry-run` previews. |

---

## Agents

### First-class adapters

Each kind knows how to launch its CLI, stream output, and parse usage. Use the
kind name directly as `--target` (the binary must be on your `PATH` and
authenticated); register a named entry with `maestro agents add <name> --kind
<kind>` when you want an alias or per-agent settings. `maestro agents discover`
finds whatever is installed, and `maestro agents status <name>` shows exactly
what preflight will see before you delegate.

| Kind | Binary | Notes |
|---|---|---|
| `codex` | `codex` | OpenAI Codex CLI; autonomy flags auto-detected from `--help` |
| `claude_code` | `claude` | Anthropic Claude Code, headless mode |
| `copilot` | `copilot` | GitHub Copilot CLI (`-p … --output-format json --yolo`); usage read from its `--usage-output-file` JSON after the run |
| `cursor` | `cursor-agent` | Cursor agent print mode |
| `hermes` | `hermes` | Hermes Agent |
| `pi` | `pi` | Pi (rpc mode) |
| `cline` | `cline` | Cline CLI |
| `openhands` | `openhands` | OpenHands CLI |
| `a2a_remote` | *(URL, not a binary)* | Another Maestro daemon — see below |

### Onboarding any CLI (generic spec)

Anything that can run non-interactively from a shell works as `kind = "generic"`
— no code, just a registry entry:

```bash
maestro agents add mytool --kind generic \
  --command "mytool run --json {prompt}"     # or pipe the prompt instead:
  --input-mode arg                          #   --input-mode stdin
  --output-format jsonl                     # text | jsonl | rpc
```

- `{prompt}` in the command inserts the work order; `input_mode = "stdin"`
  pipes it on stdin instead (better for long prompts).
- `output_format = "jsonl"` makes Maestro parse `cost_usd` / usage hints from
  JSON lines automatically.
- Preflight checks the binary + version before any delegation, so a wrong entry
  fails fast with a clear error.

Full recipes and a verification checklist for new agents:
[docs/agent-onboarding.md](docs/agent-onboarding.md).

### Remote agents

**Another Maestro daemon (`a2a_remote`).** Daemons can delegate to each other
over the A2A protocol — this is how you build a small agent cluster. The full
handoff document travels with the request, so routing survives the hop:

```bash
# On machine A, register machine B's daemon (note: it's a URL, not a binary):
maestro agents add remote-b --kind a2a_remote --command http://10.0.0.5:8790
maestro delegate --title "…" --request "…" --target remote-b --workspace /path/to/repo
```

Maestro checks the remote's agent card before delegating, streams its output
and usage live, and forwards cancellation. If the remote daemon has no free
workspace slot it queues the task — Maestro reports that as a clear error rather
than hanging.

**REST task servers (`api` mode).** A generic agent whose command is an `http(s)`
URL switches to API mode automatically: Maestro submits, polls for status, and
reports output/costs against this small contract (no push channel needed):

```text
POST /tasks            {"task_id", "prompt", "workspace"}        → 202 {"id": …}
GET  /tasks/{id}       → {"state", "output", "usage", "error"}   (polled ~1/s)
POST /tasks/{id}/cancel
```

`state` is `working` until it reaches a terminal state: `completed`, `failed`,
or `canceled`. The base URL can also come from per-agent settings
(`api_base_url`) instead of the command.

---

## P2P discovery

When daemons share a network, they find each other automatically. In plain
terms: every daemon periodically gives a small "I'm here!" shout (UDP multicast
on port 9786) carrying its name and HTTP port; every other daemon that hears it
records the peer in `~/.maestro/peers.json`. Peers heard recently are **live**;
a peer silent for ~15 seconds is marked **stale**.

```bash
maestro peers list                 # discovered + manually added peers, with status
maestro peers add --name lab --url http://10.0.0.5:8790   # manual registration
maestro peers remove lab
```

**When you need `peers add` instead of auto-discovery:**

- The network blocks multicast/broadcast (many corporate networks, some Wi-Fi,
  VPNs) — the shout never travels.
- You want a stable name for a long-lived remote daemon.
- A machine runs several daemons and you want explicit control.

Manually added peers keep their URL forever and never go stale; discovered
peers refresh automatically. To tune or disable discovery:

| Variable | Default | Meaning |
|---|---|---|
| `MAESTRO_DISCOVERY` | `1` | Set `0` to turn discovery off entirely |
| `MAESTRO_DISCOVERY_PORT` | `9786` | UDP port for the presence channel |
| `MAESTRO_DISCOVERY_IF` | default interface | Interface to shout on (e.g. `127.0.0.1` for loopback only) |
| `MAESTRO_DISCOVERY_TTL` | `1` | Hop distance: `0` = this machine only, `1` = LAN |
| `MAESTRO_NODE_NAME` | `maestro-node` | The name this daemon announces under |

---

## Budget caps

Cap how much Maestro can spend so a runaway task chain doesn't drain your
account. Caps are per-daemon (set them in the environment where you start the
daemon):

```bash
export MAESTRO_BUDGET_PER_AGENT_USD=25     # cumulative cap per agent name
export MAESTRO_BUDGET_DAILY_USD=100        # cap across all agents, resets at UTC midnight
maestro-daemon
```

How it behaves:

- Enforcement is **at launch time only**: when a budget is exhausted, new
  delegations to that agent (or any agent, for the daily cap) are refused with
  a clear error. **Running tasks always finish.**
- Spend counts what actually ran — each attempt's usage, including failed
  attempts (failed work still costs money).
- Check where you stand anytime:

```bash
maestro budgets
# per-agent cap: $25.0000
#   codex: $3.4120
# daily cap: $100.0000 — spent today (UTC): $7.8834
```

Agents with no cost reporting (most CLIs don't emit USD) simply contribute $0;
caps still work for the agents that do report costs (Codex, Claude Code, …).

---

## CLI reference

Global options: `--workspace DIR` or `--project DIR` scope task commands to a
location (default: `$MAESTRO_WORKSPACE` or the current directory).

| Command | What it does |
|---|---|
| `maestro-daemon [--port N] [--state-dir DIR]` | Start the local broker daemon |
| `maestro delegate --title … --request … --target A --fallback B --workspace DIR` | Delegate a handoff (blocks, live output). `--file handoff.toml` instead of flags; `--no-wait` returns immediately |
| `maestro dashboard` | Terminal full-screen dashboard (SSE-driven) |
| `maestro task list [--project DIR]` | List tasks (number or id) |
| `maestro task status <id\|n>` | Show one task's current state (`task show`, `status`, and bare `task <n>` are aliases) |
| `maestro task tail <id> [--all]` | Live-tail a task's event stream |
| `maestro task audit <id>` | Durable record: attempts, usage, errors, result files |
| `maestro agents list \| add \| remove \| discover \| status <name>` | Manage registered agents |
| `maestro peers list \| add --name N --url U \| remove NAME` | Discovered/registered peers |
| `maestro budgets` | Show budget caps and current spend |
| `maestro handoff --title … --request … --design-file F [--model M] [--effort E]` | Create + launch a Codex handoff directly (no daemon) |
| `maestro codex-followup <id> "instruction"` | Send a follow-up to the task's Codex run |
| `maestro run <id>` | Launch implementation for an existing task |
| `maestro config` | Show effective Codex defaults |
| `maestro storage …` | Manage storage backends |
| `maestro gc [--days N] [--dry-run]` | Delete terminal tasks older than the TTL (manual only) |

Handoff files are TOML or JSON with the same fields as the flags: `title`,
`request`, `design`, `target_agent`, `fallback` (list), `commit_policy`
(`branch` default — requires a git repo; use `no-commit` otherwise),
`sensitive`, `budget_hint`.

---

## Configuration

### Project config: `.maestro/config.toml`

Precedence (most specific wins): `~/.maestro/config.toml` →
`<project-root>/.maestro/config.toml` → `<active-worktree>/.maestro/config.toml`.

```toml
[codex]
model = "gpt-5.6-luna"
effort = "max"            # low | medium | high | xhigh | max

[verification]
command = ["make", "check"]   # run this after each agent finishes (optional)

[storage]
backend = "filesystem"    # filesystem (default) | memvara
```

### Environment variables

| Variable | Default | What it does |
|---|---|---|
| `MAESTRO_HOME` | `~/.maestro` | State directory (registry, tasks, claims, peers, daemon marker) |
| `MAESTRO_WORKSPACE` | cwd | Default workspace for task commands |
| `MAESTRO_DAEMON_URL` | from `daemon.json` | Point CLI commands at a specific daemon (e.g. another machine's) |
| `MAESTRO_MAX_RETRIES` | `2` | Retry attempts per agent before falling back |
| `MAESTRO_BACKOFF_S` | `1.0` | Seconds between retries |
| `MAESTRO_DELEGATE_TIMEOUT` | `3600` | Max seconds the MCP server waits for a delegated task |
| `MAESTRO_BUDGET_PER_AGENT_USD` | off | Cumulative USD cap per agent (see Budget caps) |
| `MAESTRO_BUDGET_DAILY_USD` | off | Daily USD cap, all agents, UTC day |
| `MAESTRO_DISCOVERY` / `_PORT` / `_IF` / `_TTL` | on/9786/default/1 | P2P discovery tuning (see P2P discovery) |
| `MAESTRO_NODE_NAME` | `maestro-node` | Announced name for discovery |
| `MAESTRO_STORAGE` | — | Override storage backend for the shell |
| `MAESTRO_CODEX_MODEL` / `MAESTRO_CODEX_EFFORT` | — | Override Codex model/effort for the shell |

---

## Where Maestro stores state

Authoritative runtime state lives at the **user level**, not inside each
project or worktree:

```text
~/.maestro/                      (or $MAESTRO_HOME)
├── registry.json                # registered agents
├── state.jsonl                  # durable claim journal (task history)
├── daemon.json                  # which daemon is running (port, pid)
├── peers.json                   # discovered/registered peers
├── config.toml                  # user-level config
└── tasks/<task-id>/             # per-task artifacts (logs, results)
```

Project-level files are configuration only: `<project>/.maestro/config.toml`
(and optionally the same inside a worktree). Task state survives worktree
creation, switching, and deletion; each task records exactly which workspace it
ran in.

**Worktrees:** if you work in Git worktrees (e.g. Claude Code's), pass the
*active worktree path* as `--workspace` — never rely on the current directory
of whatever process is calling Maestro. The task records both the workspace and
the project root.

---

## Deterministic verification

After an agent finishes, Maestro runs a deterministic check before reporting
completion. Selection order:

1. `[verification] command` in `.maestro/config.toml`, if set.
2. `make check` when a `Makefile` exists.
3. Node projects with a `test` script (npm/pnpm/yarn per lockfile).
4. Go → `go test ./...`; Rust → `cargo test`.
5. Python → pytest, when installed.
6. Fallback: `git diff --check`.

Maestro never installs dependencies or changes your tooling; a real non-zero
result is recorded as a verification failure in the task record.

---

## Using Maestro through Claude Code (MCP)

The most common setup: you talk to Claude, and Claude delegates implementation
to Maestro's agents. Setup:

1. **Expose Maestro as an MCP server** in your project's `.mcp.json`:

   ```json
   {
     "mcpServers": {
       "maestro": { "command": "scripts/maestro-mcp" }
     }
   }
   ```

   `chmod +x scripts/maestro-mcp` — the launcher pins the repository's
   `.venv` so Claude and your CLI share one Python environment. Approve the
   server when Claude Code asks; `.claude/settings.json` in this repo grants
   the tool permissions.

2. **Give Claude the supervisor rules.** This repo ships `CLAUDE.md` (Claude's
   routing rules) and `AGENTS.md` (implementation-agent rules). Copy or adapt
   them into projects where you want the same division of labor: *Claude decides
   what to build and reviews; the implementation agent does the work.*

3. **Just ask Claude** for normal implementation work:

   ```text
   Implement semantic search for the existing document API.
   Inspect the repository first, create a compact design, then delegate the
   implementation through Maestro. Run the tests and review the diff.
   ```

Claude creates a compact handoff, calls Maestro's `delegate` tool (which goes
through the same daemon as the CLI), waits on the event stream, reviews the
result, and can send follow-ups (`codex_followup`) until it approves. You never
copy prompts between tools by hand.

The MCP tools mirror the CLI: `delegate`, `task_wait`, `task_status`,
`followup`, … — everything the CLI can do, Claude can do.

---

## Troubleshooting

**"no daemon reachable — start one with 'maestro-daemon'"**
No running daemon was found (or its marker is stale). Start one, or point at a
specific one: `export MAESTRO_DAEMON_URL=http://host:port`.

**A task shows the target agent "not implemented" / preflight failed**
The agent's binary isn't on your PATH or isn't authenticated. Check with
`maestro agents status <name>` — it shows exactly what preflight found. Register
it (`maestro agents add …`) if it was never registered.

**Delegation refused: "Budget cap exceeded"**
A cap is exhausted (see `maestro budgets`). Raise the cap or wait for the daily
reset; already-running tasks are unaffected.

**`peers list` is empty on a LAN**
The network likely blocks multicast — use `maestro peers add --name … --url …`.
Verify your daemon is announcing at all: set `MAESTRO_DISCOVERY_TTL=0` and
`MAESTRO_DISCOVERY_IF=127.0.0.1` to confirm loopback discovery works before
debugging the network.

**Two daemons on one machine don't see each other**
Discovery relies on shared-UDP-port semantics that are verified on macOS; on
other systems, register peers manually instead.

**The web console shows nothing / 404**
The daemon serves the console from its bundled `web_dist/`. Check the daemon is
the version you expect (`maestro --version`) and that `daemon.json` points at
the port you're browsing.

**Wrong Python picked up by the MCP server**
Always launch through `scripts/maestro-mcp`; verify with
`.venv/bin/python -c 'import maestro; print(maestro.__file__)'`.

**A task can't be found after a restart**
Tasks are durable per state directory — make sure you're using the same
`MAESTRO_HOME` as when the daemon ran, then `maestro task list`.

---

## Development

```bash
python -m pip install -e .
python -m pip install pytest coverage
python -m coverage run --branch -m pytest -q
python -m coverage report --fail-under=100     # CI enforces 100% line+branch
```

The web console's built artifacts are committed in `maestro/web_dist/`, so
Node is **not** needed to use Maestro. To rebuild the console after changing
`web/src/`:

```bash
cd web && npm install && npm run build   # rewrites maestro/web_dist/
```

The flagship end-to-end demo (full role swap, one command) is
`examples/full-swap.sh`. Design rationale and the milestone history live in
[docs/architecture-proposal.md](docs/architecture-proposal.md); agent onboarding
recipes in [docs/agent-onboarding.md](docs/agent-onboarding.md).

---

## Design principles

- **The supervisor decides what to build; implementation agents do the work.**
  (With direct use, *you* are the supervisor.)
- **Maestro owns task lifecycle and durable state** — nothing important lives
  in a terminal buffer.
- **Reactive, not polling**: events flow over SSE everywhere; dashboards and
  waits subscribe instead of spinning.
- **Verification is deterministic.** Evidence, not vibes.
- **Maestro never commits code and never installs dependencies.**
- **The workspace is always explicit** — passed in, recorded, never guessed.
