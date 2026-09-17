# Maestro 1.0 — Universal Agent Harness: Architecture Proposal

Status: proposed, pending user approval.
Decisions input: `docs/multi-agent-protocol-research.md` §4 (interview).

## 1. One-line vision

**Any agent can delegate to any other agent through one local harness, with
durable state, deterministic verification, and a single dashboard — speaking the
A2A standard under the hood.**

## 2. Guiding decisions (from interview)

| Decision | Choice |
|---|---|
| Protocol | A2A (Linux Foundation) for agent↔agent; MCP for agent↔tool |
| Topology | Peer-capable protocol; Maestro broker = default local deployment |
| Agent scope | Every popular agent → first-class adapters + generic adapter spec |
| Hermes | Self-hosted Nous Research model via Ollama/vLLM/LM Studio |
| v1 deployment | Single local machine; remote nodes are a later transport swap |
| Success demo | Full role swap: Claude→Codex, Codex→Claude callback, Hermes e2e, one dashboard |

## 3. Architecture overview

```text
        Host agents (whoever you talk to)
 ┌────────────┐ ┌────────────┐ ┌─────────────┐ ┌──────────────────┐
 │ Claude Code│ │  Codex CLI │ │ VS Code /   │ │ Gemini CLI, Aider,│
 │            │ │            │ │ Copilot ... │ │ Goose, ...        │
 └─────┬──────┘ └─────┬──────┘ └──────┬──────┘ └────────┬─────────┘
       │ MCP tools    │ MCP/CLI       │ MCP             │ generic spec
       ▼              ▼               ▼                ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │                     MAESTRO DAEMON (local broker)                │
 │  • A2A server: Agent Card, message/send, tasks/get, SSE stream   │
 │  • Agent registry (~/.maestro/agents/*.toml + cards)             │
 │  • Router: task → adapter launch, multi-hop subtasks            │
 │  • Durable state (extends ~/.maestro/), audit journal           │
 │  • Deterministic verification hooks (existing logic)            │
 └──────┬─────────────┬─────────────┬─────────────┬────────────────┘
        ▼             ▼             ▼             ▼
   ┌─────────┐   ┌─────────┐  ┌──────────┐  ┌──────────────┐
   │ codex   │   │claude_  │  │ hermes   │  │ a2a_remote / │
   │ adapter │   │code     │  │ adapter  │  │ generic TOML │
   │(codex   │   │adapter  │  │(Ollama/  │  │ adapters     │
   │ exec)   │   │(claude  │  │ vLLM +   │  │ (any CLI)    │
   └─────────┘   │ -p)     │  │ agent    │  └──────────────┘
                 └─────────┘  │ loop)    │
                              └──────────┘

 P2P: any Maestro daemon ⇄ any other Maestro daemon over A2A (v2 transport).
```

Key insight for "any agent can call any agent": third-party CLIs don't speak A2A
natively, so they get the capability **through Maestro's MCP tools** (which every
major host agent supports: Claude Code, Codex, VS Code/Copilot, Cursor, Gemini CLI).
The A2A surface is what Maestro nodes speak to each other and what future-native
agents will speak.

## 4. Components

### 4.1 `maestro-daemon` (new console script)
- Local broker on `127.0.0.1:<port>`; A2A JSON-RPC over HTTP + SSE streaming.
- Serves its own Agent Card at `/.well-known/agent.json` — the Maestro node is
  itself an agent (accepts tasks, routes them).
- Owns durable state in `~/.maestro/` (extends existing registry/state files;
  migrations for existing installs).
- Every inter-agent message/task journaled to the audit log (existing state.jsonl
  pattern, extended with direction + agent identity).

### 4.2 Agent registry & discovery (`~/.maestro/agents/`)
- One TOML per registered agent: adapter kind, launch config, skills, auth notes.
- `maestro agents discover` scans PATH for known CLIs (claude, codex, gemini,
  aider, ollama...) and proposes registrations.
- Agent Cards generated from registry entries; exposed via the daemon.

### 4.3 Adapters (`maestro/adapters/`)
Each adapter = (a) launch the agent's headless entry point in the task workspace,
(b) normalize its output stream into A2A Message/Task events + Artifacts,
(c) report terminal state.

#### Agent coverage matrix (the popular-agent list, verified against live docs)

| # | Agent | Verified integration path | v1 class |
|---|---|---|---|
| 1 | **Hermes Agent** (Nous Research) — self-improving agent, persistent memory, 40+ tools, subagents | Its CLI/gateway entry point (verified during M4 build); self-hosted | First-class adapter |
| 2 | **Claude Code** | `claude -p --output-format stream-json` + Maestro MCP injection for call-backs | First-class adapter |
| 3 | **Kilo Code** (Anaconda) — VS Code/JetBrains/CLI, Agent Manager, MCP (stdio/SSE) | CLI/Agent Manager entry point; falls back to generic spec | Generic-spec onboarding (first-class later) |
| 4 | **Cline** | Real CLI: `npm i -g cline`, headless mode, `--json` structured output, ACP support | First-class adapter |
| 5 | **pi** (Earendil) — minimal harness, 4 modes | **RPC mode: JSON protocol over stdin/stdout**; also print/JSON (`pi -p`, `--mode json`) | First-class adapter |
| 6 | **omp** | "coding agent with the IDE wired in"; docs are JS-rendered, entry point to verify during build | Generic-spec onboarding (verify first) |
| 7 | **Codex** | `codex exec --json` (generalizes today's `worker.py`) | First-class adapter |
| 8 | **OpenClaw** — persistent personal agent over messaging apps, built with pi SDK | Its gateway/CLI entry point; shares pi integration knowledge | Generic-spec onboarding (first-class later) |
| 9 | **OpenHands** | **CLI is deprecated** — integrate via Software Agent SDK / Agent Server REST+WebSocket API | First-class adapter (API-based) |
| 10 | **CodeGPT** | IDE extensions only (VS Code/JetBrains/VS), BYOK, MCP; no CLI found | IDE-resident class — out of v1 scope for headless delegation; document path (extension API / human-in-loop) |
| 11 | **Cursor** | `cursor-agent` headless CLI | First-class adapter |
| 12 | **VS Code / GitHub Copilot** | Two surfaces: (a) **Copilot via `gh copilot` CLI extension** (+ Copilot coding agent on GitHub), (b) VS Code as the *host* for editor-resident agents — Cline, Kilo, CodeGPT already run inside it and are covered by their own adapters | First-class adapter for Copilot (`gh copilot`); editor-resident class for in-editor agents |

Plus two structural adapters:

| Adapter | Wraps | Notes |
|---|---|---|
| `a2a_remote` | Any A2A endpoint | P2P to other Maestro nodes / future native agents |
| **generic** | Declarative TOML: launch cmd, input template, output event schema, workspace handling | The "every popular agent out there" path — onboard any CLI without Python code; covers Kilo/omp/OpenClaw today and everything added later |

Design consequence: M2 ships the **adapter contract** (interface + event
normalization + verification hook) proven by `codex` and `claude_code`; every
later adapter in the matrix is then a mostly-config increment, which is what makes
the full list achievable without scope explosion.

### 4.4 MCP surface (extends existing `mcp_server.py`)
- **Backward compatible**: `delegate_to_codex`, `task_status`, `list_tasks`,
  `codex_followup`, `review_task` keep working unchanged.
- **New generic tools** (available to every host agent that has Maestro MCP):
  - `agents_list()` — registry + cards + skills + live status.
  - `delegate(agent, workspace, handoff_file)` — route a staged handoff to any
    registered agent (Codex path becomes a special case of this).
  - `followup(task_id, instruction)` — multi-turn on any agent task.
  - `task_status` / `list_tasks` now report direction (origin → target) and
    subtask trees for multi-hop work.

### 4.5 Task model (extends `models.py` / `core.py`)
- Tasks gain: `origin_agent`, `target_agent`, `parent_task_id` (multi-hop),
  A2A-aligned state machine:
  `staged → submitted → working → input-required → completed | failed | canceled`.
  Existing phases map into this; old tasks migrate cleanly.
- Multi-hop = subtask with parent link: Codex→Claude callback is a new task whose
  parent is the original, carrying `contextId` semantics from A2A.

### 4.6 CLI (extends `cli.py`)
- `maestro agents list|add|remove|discover|status`
- `maestro delegate --to <agent> "request" [--workspace ...]`
- `maestro task list|show` — now cross-agent, with direction + subtasks.
- `maestro dashboard` — terminal view of live tasks across agents (v1: rich text;
  web UI later if wanted).

### 4.7 A2A implementation approach
Thin native implementation of the A2A core subset we need in v1
(`message/send`, `tasks/get`, `tasks/cancel`, `message/stream` SSE, Agent Card),
validated by a spec-conformance test suite against the published JSON schemas.
Rationale: keeps dependencies light (package today depends only on `mcp`), keeps
the 100% coverage gate achievable, and our v1 A2A peers are other Maestro nodes +
our own test clients. If exotic features become needed later, swap in the official
`a2a-sdk` behind the same internal interface.

**Framework decision (interview round 4b): no ADK.** Maestro wraps external agent
products rather than building its own agents, so an agent-development framework has
nothing for it to wrap; A2A compliance comes from spec conformance, not SDK choice.
ADK-built (or any A2A-native) agents join later via the `a2a_remote` adapter with
zero added dependencies. An LLM-driven smart-routing brain (roadmap) would use a
direct model API call if ever needed.

## 5. Governance & operations (interview round 2 — decided)

| Area | Decision |
|---|---|
| **Permissions** | All-to-all: any registered agent may delegate to any other. Maestro enforces guardrails: max delegation depth (config, default 3), per-agent concurrency limit (default 1), full audit journal of every hop. Optional per-hop human approval for workspaces flagged `sensitive = true` in config. |
| **Cost control** | Track + optional caps: per-task cost/token usage recorded wherever the agent CLI reports it (codex/claude JSON streams include usage); shown in dashboard. Optional `[budgets]` config: per-agent and per-day caps that block *new* task launches when exceeded (running tasks finish). |
| **Concurrency** | One active task per workspace; concurrent requests queue behind it (FIFO, visible in dashboard). No parallel agents in the same worktree in v1. |
| **Routing** | Explicit target + fallback chain: caller names the agent; config may declare `fallback = ["codex", "claude_code"]` per agent or per task — used only after failure, never for initial routing. |
| **Failure policy** | Staged: (1) auto-retry same agent up to N times (config, default 2) with backoff on technical failures; (2) then walk the fallback chain; (3) then mark failed and escalate to the user with full evidence (logs, verification output). Verification failure ≠ technical failure — it goes straight to review/escalation, never auto-retry. |
| **Context model** | Opaque handoffs (A2A principle): each agent sees only its own handoff + upstream artifacts/evidence. No implicit shared memory between agents; a shared per-task notes file is a possible later opt-in feature. |
| **Dashboard** | A local web UI served by the daemon on its port (single-file page, EventSource-driven — no polling), plus `maestro task tail` for live terminal monitoring of one task or the global stream. A full terminal TUI is a v2 candidate; the SSE tail covers day-to-day live viewing. |

### Standing design rules implied by the above

- **Secrets**: Maestro never reads or stores agent API keys — each CLI authenticates itself through its own normal flow. The broker only launches and observes.
- **Prompt contracts**: every host agent gets a small instruction file (CLAUDE.md exists; equivalents for Codex/Cline/pi/etc. ship with their adapters in M3/M4/M5) teaching it the Maestro MCP tools and the delegation etiquette.
- **Packaging**: stays a pip-installable Python package; entry points `maestro` (CLI) + `maestro-daemon` (broker). 1.0 is the first multi-agent release; 0.8.x behavior remains available within it.

## 6. What stays / what changes (backward compatibility)

- `~/.maestro/` state layout: extended with new files, never breaking old ones;
  migration path for existing task registries.
- Existing Claude→Maestro→Codex loop: unchanged behavior, now implemented as
  `delegate(agent="codex")` under the hood.
- CLAUDE.md / AGENTS.md contracts: keep working; gain optional new sections for
  cross-agent delegation once M3 lands.

## 7. Milestones (each ships with tests, 100% coverage gate)

### Implementation status

- **M1 — DONE**: `maestro/agents.py` (AgentSpec + validation + TOML round-trip +
  AgentRegistry: list/get/save/remove/discover/status + version probe), task model
  extension (`AgentState` A2A-aligned enum, phase mapping, Task multi-agent fields),
  `maestro agents list|add|remove|discover|status` CLI. 130 tests, 100% branch
  coverage. Live smoke test on the developer machine discovered 5 of 8 known CLIs
  (codex, claude_code, hermes, cursor-agent, gh).
- **M2 — DONE**: daemon (`maestro/daemon.py` + `maestro-daemon` console script) with
  A2A server (agent card, `message/send`, `tasks/get`, `tasks/cancel`, SSE event
  streams with keepalive), reactive event bus (`maestro/events.py`), FIFO router
  (one active task per workspace, queue promotion on release), adapter contract
  (spawn + rpc implemented; api remains a v2 seam) + codex / claude_code / generic
  adapters, handoff document schema (`maestro/handoff.py`, TOML + legacy JSON),
  per-task branches `maestro/<task_id>`, preflight (binary/version/auth fail-fast),
  retry-with-backoff → fallback chain → escalation event, cancellation with partials
  kept, `input-required` mid-task question routing, blocking MCP tools
  (`delegate`, `task_wait`, `cancel_task`, `answer_task_question`, `agents_list`).
  271 tests, 100% branch coverage. No polling anywhere: everything is event-driven
  (bus + SSE + blocking waits).
- **M3 — DONE**: MCP surface parity — `followup` tool (any agent resumes a
  finished task on its branch with reduced depth), no-self-review/self-delegation
  guard in the router, error paths surfaced as JSON on every new tool, legacy
  0.8.x tool signatures frozen by contract tests. 277 tests, 100% branch coverage.
- **M4 — DONE**: popular CLI trio — **pi** (Earendil) first-class adapter in the
  new **rpc run mode** (JSONL protocol over stdin/stdout: prompt command,
  `agent_settled` completion, usage from `message_update`, polite `abort`),
  **Cline** (`--json` headless spawn, auto-approve act mode), **Hermes Agent**
  (one-shot `-z` + `--usage-file` cost report, verified live against the
  installed v0.20.x CLI). 298 tests, 100% branch coverage.
- **M5 — DONE**: API/editor-class adapters — **Cursor** (`cursor-agent -p
  --output-format json`, live-verified against the installed CLI, including its
  undocumented `usage` token fields) and **OpenHands** (V1 headless CLI
  `--headless --json` with task-file input and explicit exit codes — the
  documented headless mode made the Agent Server REST path unnecessary for v1),
  plus generic-spec onboarding recipes in `docs/agent-onboarding.md`: OpenClaw
  (verified `openclaw agent exec --json`), Kilo Code (IDE-resident → MCP host
  path), omp (entry point unverified — verify-at-install checklist). 305 tests,
  100% branch coverage.
- **M6 — DONE**: observability + lifecycle — single-file web dashboard at `GET /`
  (EventSource over the new global `GET /events` stream; initial list from
  `GET /tasks`; no polling), `maestro task tail` (live SSE tail of one task or
  the global stream, ring-buffer catch-up for finished tasks), `maestro task
  audit` (durable attempts/usage/errors without a running daemon), `maestro gc`
  (TTL 90 days, manual only, dry-run first; deletes task dirs + registry entry +
  claims), and the flagship demo `examples/full-swap.sh` — Claude Code as a host
  agent delegating to Codex through Maestro MCP (degrading gracefully to CLI
  delegation when the claude CLI is plan-locked), then a CLI delegation to Hermes
  building on the same workspace. Live verification also exposed codex's flag
  rename (`--full-auto` → `-s workspace-write --approve-for-me` in 0.15x): the
  adapter and legacy worker now probe `codex exec --help` once and pick the flag
  set the installed CLI accepts, so old and new installs both work unmodified.
  344 tests, 100% branch coverage.

**All milestones M1–M6 are complete.**

### v2 (in progress)

- **v2-M1 — DONE**: React web console — source in `web/src` (React 18 + esbuild,
  built by `node web/build.mjs` into the committed `maestro/web_dist/`, so no
  Node toolchain is needed at runtime). Served at `GET /` and `GET /console.js`;
  strictly reactive: one `GET /tasks` fetch, then a single EventSource over
  `GET /events`. `/tasks` metadata now carries usage/attempts/error (live from
  the in-memory record; durable parsed from the `task_runtime` claim), and cost
  accumulates across attempts — failed work still costs money.
- **v2-M2 — DONE**: terminal TUI — `maestro dashboard` (stdlib-only, ANSI).
  Same reactive contract as the web console: one `GET /tasks`, then a single
  SSE stream; an SSE reader thread pushes each event through a pipe and the
  main loop blocks on `select(stdin, pipe)` — no redraw timer, no polling.
  Puts the terminal in raw mode (restored on exit), renders task list +
  detail pane with state colors/costs/attempts/transcript tail; q/ESC/^C quit,
  j/k/↑-keys navigate. Stream end (daemon stop) exits 1; unreachable daemon
  exits 1; non-tty exits 2.
- **v2-M3 — DONE**: `api` adapter mode + `a2a_remote` adapter.
  - `api` mode: generic REST task contract on BaseAdapter (`POST /tasks`,
    `GET /tasks/{id}`, `POST /tasks/{id}/cancel`; usage/output deltas, cancel,
    timeout). A `generic` agent whose command is an http(s) URL becomes an
    api-mode agent; preflight = reachability probe. REST servers without a
    push channel are polled (1s default, overridable).
  - `a2a_remote`: daemon-to-daemon delegation over the A2A wire — Agent Card
    preflight, `message/send` (full handoff data part when the daemon injected
    `maestro_handoff`, else text), then the per-task SSE stream for live
    output/usage until terminal state; queued remotes fail fast with a clear
    error; cancel via `tasks/cancel`. Shared client helpers moved to
    `maestro/a2a_client.py` (CLI now reuses them).
- **v2-M4 — DONE**: P2P peer discovery.
  - `maestro/discovery.py`: each daemon joins a shared UDP multicast group
    (group 234.5.6.7, port 9786; SO_REUSEPORT lets several nodes share one
    host) and announces presence every 5s with name/HTTP port/pid/nonce.
    Peers land in `peers.json` under the state dir; entries go stale after
    three missed heartbeats. Env knobs: `MAESTRO_DISCOVERY` (default on),
    `MAESTRO_DISCOVERY_PORT`, `MAESTRO_DISCOVERY_IF`, `MAESTRO_DISCOVERY_TTL`
    (TTL 1 = LAN; tests pin TTL 0 + loopback via an autouse fixture). If the
    shared port cannot be bound, the node falls back to an ephemeral port.
  - CLI: `maestro peers list|add|remove` — manual registration covers
    networks without multicast; manual peers never go stale.
- Remaining v2 candidates: Copilot adapter, budget caps enforcement.

| # | Deliverable | Proves |
|---|---|---|
| M1 | Task model extension + agent registry + generic adapter spec + `maestro agents` CLI | Agents are first-class citizens |
| M2 | Daemon: A2A server (card, send, get, SSE) + router + **adapter contract** + codex & claude_code adapters | Any registered agent can be delegated to; Codex can call back to Claude; existing loop unchanged |
| M3 | MCP surface extension (`agents_list`, `delegate`, `followup`) + backward-compat tests | Every host agent (Claude, Kilo, Cline, Cursor, ...) gets "call any agent" via MCP |
| M4 | Popular CLI trio: **pi** (RPC mode), **Cline** (`--json` headless), **Hermes Agent** (Nous) | The most-used local CLIs all work end-to-end |
| M5 | Editor-class adapters: **Cursor** (`cursor-agent` print mode) + **OpenHands** (V1 headless CLI) + generic-spec onboarding of OpenClaw/Kilo/omp | Full coverage matrix complete except IDE-only agents |
| M6 | Web dashboard (SSE) + `task tail` + `task audit` + `gc` + flagship demo script (`examples/full-swap.sh`) | The interview's success criteria, runnable in one command |

## 8. Risks & mitigations

- **CLI/API formats change** (codex/claude/pi/cline update their output streams):
  adapters isolate parsing; conformance fixtures per adapter version; generic spec
  lets users patch TOML without code. OpenHands' V1 headless CLI keeps its
  integration on the same spawn contract as everything else (the Agent Server
  REST/SDK path stays available for remote sandboxes).
- **Hermes Agent entry point differs from a plain model loop**: it's a persistent
  agent product with its own tools/memory — M4 verifies its headless/gateway entry
  point first and adapts to it rather than building our own loop around raw models.
- **Scope creep toward "every agent"**: the generic TOML spec is the escape valve —
  we ship 8 first-class adapters and one open spec; community/users onboard the rest.
- **100% coverage gate vs daemon complexity**: thin A2A subset + fake-agent test
  harness (scripted stub CLIs) keep behavioral tests hermetic and fast.

## 9. Remaining topics — walked through (interview round 3)

### 9.1 Security & prompt injection
- **Threat model**: every agent has shell/file access inside its task workspace;
  content agents read (repo files, web pages, upstream artifacts) may contain
  injected instructions.
- **Maestro's mitigations**:
  - Workspace pinning — each task is bound to an explicit workspace; adapters launch
    agents only there.
  - Opaque handoffs — the handoff format separates *instruction* (written by the
    delegating agent/human) from *evidence/artifacts* (data, never executed).
  - Maestro never executes agent output as commands; it launches, observes, and records.
  - Per-agent tool/approval settings stay in each CLI's own config (codex approval
    modes, claude permissions) — the registry spec can carry a `sensitive` flag that
    raises the bar (per-hop approval).
  - Full audit journal = forensic trail for anything that looks off.
- **Honest gap**: no harness fully eliminates prompt injection; the backstop is the
  existing human review loop before completion + workspace isolation.

### 9.2 First-time setup & developer experience
- `maestro init` (one-shot wizard): scans PATH for known CLIs, registers found
  agents with sane defaults, writes a starter user-level config (guardrails,
  budgets, fallback chains), prints next steps. Zero-to-"any agent callable" in one
  command.
- Config layout: user-level `~/.maestro/config.toml` (guardrails, budgets, fallbacks)
  + project `.maestro/config.toml` (verification commands, `sensitive` flag) — the
  existing precedence chain is kept and extended.
- Per-agent instruction files: each adapter ships a contract snippet;
  `maestro agents contract <agent>` prints it for pasting into that agent's
  AGENTS.md/CLAUDE.md equivalent (or auto-appends with confirmation).

### 9.3 Adapter execution modes (implementation detail, settled)
The M2 adapter contract supports three run modes so the whole matrix fits one design:
- **spawn** — one-shot process in the workspace (codex, claude_code, cline, cursor,
  generic). Default mode.
- **rpc** — long-lived stdin/stdout JSON protocol (pi's RPC mode).
- **api** — HTTP service with its own lifecycle (OpenHands Agent Server REST/WS;
  Hermes Agent if it runs as a resident gateway; Copilot coding agent on GitHub,
  PR-scoped and async, if needed).
M4/M5 verify each product's actual entry point before finalizing its adapter.

### 9.4 Remote / multi-machine (v2 preview — no v1 architecture cost)
- A remote node is the same daemon elsewhere: discovered by Agent Card URL,
  authenticated with tokens declared in the card. Bind address stays `127.0.0.1`
  by default; LAN/remote exposure is a config change plus token issuance.
- State remains local per node; cross-node tasks carry A2A `contextId`; artifacts
  transfer as A2A artifact parts (URL refs or inline bytes).
- v1 keeps the network layer clean so this is a transport swap, not a re-architecture.

### 9.5 Testing & quality bar
- **Hermetic fake agents**: scripted stub CLIs emitting realistic streams
  (codex-style JSON events, claude stream-json) — adapters are tested against
  fixtures, never against real subscriptions in CI.
- **A2A conformance suite**: daemon endpoints validated against the published A2A
  JSON schemas; kept green per release.
- **100% branch coverage gate** stays (existing CI).
- **Live smoke tests**: opt-in local target (e.g. `make live`) that exercises real
  authenticated CLIs on a developer's machine — never in CI.

### 9.6 Roadmap beyond v1 (sequenced by the data v1 collects)
1. Smart routing by skill/cost — registry skills + usage tracking make this a
   config toggle, not new plumbing.
2. Opt-in shared per-task notes (context bank) for teams of agents on one task.
3. Remote nodes + team features: shared project state, review queues.
4. Experience learning: record which agent succeeded at which task type; inform
   default fallback chains over time (the "self-improving" direction).
5. Web UI polish: live streaming of agent output per task.

### 9.7 Versioning & release
- **1.0.0** = multi-agent GA. 0.8.x state files remain readable; new directories
  (`agents/`, daemon state) are additive. Migration notes ship in the README.

## 10. Interview round 3 — task semantics (decided, one question at a time)

| # | Topic | Decision |
|---|---|---|
| 1 | Cross-review | **No self-review**: an agent's own task can only be reviewed/approved by a *different* registered agent or by the human. Any agent may act as reviewer when asked. |
| 2 | Mid-task questions | A2A `input-required` routes to the **delegating agent first** (it has the context); on timeout or a human-only flag it escalates to the human in the dashboard. |
| 3 | Handoff contract | Standard 4-section document approved: `[handoff]` (title, request, design, context pointers), `[routing]` (target_agent, fallback, origin_agent, parent_task), `[expectations]` (artifacts, verification, commit_policy, budget_hint), `[constraints]` (sensitive, max_depth_remaining). Carried as structured A2A data parts. |
| 4 | Git policy | **Per-task branches**: each task runs on its own branch/worktree; agents may commit only to that branch. Merging/pushing to main stays with the human. `commit_policy` knob: `no-commit \| branch \| pr`. |
| 5 | Primary UI | **Both first-class from day one**: agent-driven (MCP tools) and human-driven (CLI + dashboards) get equal investment every milestone — no second-pass polish. |
| 6 | Agent config | Per-agent defaults in the registry entry (model, effort/thinking, timeouts); any delegation can override per task. Existing `[codex]` config migrates automatically. |
| 7 | Preflight | Binary + version + cheap authenticated probe before launch; unhealthy agent fails fast with a clear message. |
| 8 | Live streaming | Full live tail everywhere: `maestro task tail <id>` + per-task streams in both dashboards (normalized events AND raw agent output). |
| 9 | Cancellation | Keep partials on the task branch; mark task `canceled` with reason; evidence/artifacts stay attached. Nothing destroyed. |
| 10 | Retention | Configurable TTL + explicit `maestro gc` (default 90 days; raw logs shorter). No automatic deletion ever. |
| 11 | Multi-repo | Strictly one workspace per task in v1; cross-repo work = linked parent/child tasks sharing a context. |
| 12 | Privacy | Strictly local in v1: state, audit, dashboards never leave the machine; no telemetry. Remote nodes only in v2 with opt-in + tokens. |
| 13 | Branding | Keep **Maestro**; repo renamed to match (user performs the GitHub rename; docs/README reference it). |

### Milestone impact from round 3
- **M2** gains: handoff document schema in core, per-task branch creation, preflight
  probes, cancellation semantics, `input-required` question routing.
- **M3** gains: no-self-review enforcement in the review loop; strict MCP↔CLI parity
  for every capability (round-3 rule #5).
- **M6** gains: live tail + web dashboard, `maestro gc`, durable audit view.

## 11. Interview round 4 — reactive completion (no polling)

Principle: **event-driven core; polling only as a degraded fallback.** The daemon
runs an internal event bus; every task state change emits an event. Consumers:

| Consumer | Mechanism | Polling? |
|---|---|---|
| LLM supervisors (via MCP) | `delegate` tool call **blocks and resolves on completion/failure/input-required**, streaming progress meanwhile; `task_wait(task_id)` blocks until next state change or timeout for follow-ups | No — one blocking call, no check-loops for the model to write |
| Mid-task questions | Event bus pushes `input-required` to the delegating channel: blocking delegation surfaces it in-tool; otherwise dashboard escalation (round-3 #2) | No — pushed |
| Human dashboards (TUI + web) | SSE/WebSocket subscription to the event bus; `maestro task tail <id>` streams | No — subscribed |
| Remote Maestro nodes (v2) | A2A push notifications (webhooks + tokens) and/or SSE streaming per the A2A spec | No — pushed/streamed |
| Generic adapters with no structured events | Degraded mode: bounded process-state checks (alive? exited?) + parsed text | Bounded fallback only, documented per adapter |

Every first-class adapter is event-driven by construction (codex JSON events,
claude stream-json, pi RPC, Cline `--json`, OpenHands WebSocket). The 0.8.x
pattern of the supervisor looping on `task_status` is retired; `task_status`
remains for one-shot inspection, not as a wait mechanism.
