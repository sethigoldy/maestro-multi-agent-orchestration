# Maestro multi-agent upgrade — protocol research & current-state notes

Status: round 1 groundwork, pending user interview answers.
Date: 2026-07 (session)

## 1. What exists today (Maestro v0.8.4)

- Python package (`maestro/`), ~1k LOC: `core.py` (task lifecycle, durable state,
  config, delegation), `worker.py` (background Codex subprocess + deterministic
  verification), `mcp_server.py` (FastMCP tools for Claude Code), `cli.py`,
  `models.py`.
- Topology is hardwired hub-and-spoke: **Claude Code → MCP → Maestro → Codex CLI
  (subprocess)**. One supervisor, one implementer.
- Durable state at user level: `~/.maestro/` (registry.json, state.jsonl, tasks/,
  designs/, staged/). Worktree-aware task identity (`workspace` + `project_root`).
- Handoff contract: Claude writes a design to `.maestro/staged/`, calls
  `delegate_to_codex(workspace, handoff_file)`; worker spawns Codex, runs
  deterministic verification (make/npm/go/rust/pytest autodetect or explicit
  `[verification]`), records evidence; Claude reviews via `review_task` and can
  loop with `codex_followup`.
- 100% branch coverage gate in CI.

### Gap vs the goal
The goal is **any agent ⇄ any agent** (Claude, Codex, Hermes, VS Code/Copilot,
etc.), across subscriptions and machines, behind one harness. Today:
- The implementer role is hardcoded to Codex (`worker.py` builds `codex` commands).
- There is no discovery mechanism (no registry of "which agents exist here").
- No inbound path: only Claude can initiate; Codex cannot call back or delegate.
- No protocol standard — the contract lives in MCP tool signatures + prompt files
  (CLAUDE.md / AGENTS.md), so every new agent needs bespoke glue.

## 2. Protocol landscape (verified against live docs, 2026)

### A2A — Agent2Agent Protocol (the main candidate)
- Open standard for **agent-to-agent** communication; originally Google, now under
  the **Linux Foundation / Agentic AI Foundation** (joined Aug 2026 per blog).
  Steering committee includes AWS, Cisco, Google, IBM, Microsoft, Salesforce, SAP,
  ServiceNow. Apache-2.0. Official SDKs: Python, JS/TS, Go, Java, .NET, Rust.
- Core concepts (verified at a2a-protocol.org/latest/topics/key-concepts):
  - **Agent Card**: JSON "business card" — identity, endpoint URL, capabilities,
    auth requirements, skills. Served for discovery (`.well-known` pattern).
  - **Task**: stateful unit of work with unique ID + lifecycle
    (submitted → working → input-required → completed/failed/canceled),
    supports multi-turn via `contextId`.
  - **Message / Part**: one turn; Parts hold text | raw bytes | URL | structured
    JSON, with mediaType/metadata. Modality-independent.
  - **Artifact**: tangible output of a task (file, image, structured data).
  - Transport: HTTP(S) + JSON-RPC 2.0. Interaction modes: polling, SSE streaming,
    push notifications (webhooks). Auth via standard web headers declared in the
    card. Extensions mechanism for custom protocol bindings.
- Explicitly **not**: a sub-agent/tool-call protocol, an agent framework, or a
  replacement for MCP. "MCP = agent↔tool, A2A = agent↔agent."

### MCP — Model Context Protocol
- Agent↔**tool** standard (Anthropic-originated, now broadly adopted). Already in
  use by Maestro as the Claude→Maestro surface. Relevant to us as: (a) the way
  each host agent gets Maestro's tools, and (b) some agents expose other agents'
  capabilities as MCP tools.

### Others (less mature / narrower)
- **ACP** (IBM/BeeAI Agent Communication Protocol): REST-ish agent messaging;
  smaller ecosystem than A2A post-consolidation.
- **ANP** (Agent Network Protocol): decentralized web-of-agents, DID-based;
  research-grade for our use case.
- **AGENTS.md**: de-facto standard for per-repo agent instructions (already used
  here); not a transport protocol but useful as a capability/skill signal in an
  Agent Card.

### Headless entry points of the target CLIs (what adapters wrap)
- **Codex CLI**: `codex exec` non-interactive mode, JSON event output, working-dir
  control, AGENTS.md support. (docs: developers.openai.com/codex/noninteractive)
- **Claude Code**: headless `claude -p`, structured output formats, MCP client,
  hooks, subagents.
- **VS Code / Copilot / Cursor-class editors**: editor-embedded agents; programmatic
  entry points vary (Copilot CLI, extension APIs). Needs per-product adapter or
  "editor as a human-in-the-loop terminal" fallback.
- **Hermes**: *ambiguous — see open questions.*

## 3. Working recommendation (to confirm with user)

1. **Protocol: adopt A2A as the inter-agent standard** rather than inventing a new
   one — it already solves discovery (Agent Cards), task lifecycle, multi-turn,
   artifacts, streaming, and has SDKs in every language we need. Maestro becomes
   an **A2A server + client** plus a thin local bridge.
2. **Topology: hub-and-spoke with a local Maestro broker daemon.** Any agent can
   call any other *through* the broker; the broker owns durable task state,
   routing, verification, and audit. Pure P2P between opaque third-party CLIs is
   not feasible (they don't speak A2A natively), so adapters wrap each CLI's
   headless mode behind an A2A endpoint. The broker also makes "multiple machines"
   a later transport swap, not an architecture change.
3. **Adapters**: one per agent family — `codex` (exists today, generalize),
   `claude-code`, `hermes` (?), `vscode-copilot`, plus a generic
   `a2a-remote` adapter for anything that already speaks A2A. Each adapter
   publishes an Agent Card with skills + auth requirements.
4. **Keep the existing contract working**: current Claude→Maestro MCP surface and
   `~/.maestro/` state remain; the new layer extends them (task lifecycle maps
   cleanly onto A2A Task states).

## 4. Interview decisions (round 1 — answered)

1. **Scope**: *every popular agent out there* → adapter-based architecture:
   first-class adapters for the majors + a generic declarative adapter spec so any
   CLI can be onboarded without Python code.
2. **Hermes** = Nous Research Hermes models, self-hosted (Ollama / vLLM / LM Studio).
3. **Topology**: both — protocol is peer-capable (any A2A node ⇄ any A2A node);
   Maestro broker is the default local deployment adding state, routing, audit.
4. **Protocol**: adopt **A2A + MCP** (no new protocol to invent).
5. **Deployment**: local machine first; multi-machine later as a transport swap.
6. **Success criteria (flagship demo)**: full role swap — Claude delegates to
   Codex; Codex can call back to Claude for a design decision; local Hermes handles
   a task end-to-end; all visible in one `maestro` dashboard with durable state.

## 5. Interview round 2 — governance decisions (answered)

1. **Permissions**: all-to-all with guardrails (max depth, per-agent concurrency
   limit, full audit; optional per-hop approval for sensitive workspaces).
2. **Costs**: track usage + optional per-agent/per-day budget caps.
3. **Concurrency**: one active task per workspace; queue the rest.
4. **Routing**: explicit target + fallback chain (used only on failure).
5. **Failure**: auto-retry same agent (N×, backoff) → fallback chain → escalate to
   user with evidence. Verification failures go straight to review, never auto-retry.
6. **Context**: opaque handoffs (A2A principle); no implicit shared memory.
7. **Dashboard**: both — terminal TUI + local web UI served by the daemon.

Standing rules: Maestro never touches agent API keys; per-agent instruction files
ship with adapters; stays pip-installable (`maestro` + `maestro-daemon`).

## 6. Interview round 3 — task semantics (answered one by one)

1. Cross-review: no self-review; any *other* agent or the human may approve.
2. Mid-task questions: delegating agent answers first; escalate to human on timeout / human-only flag.
3. Handoff contract: 4-section standard document approved (handoff / routing / expectations / constraints).
4. Git: per-task branches; agents commit only to their branch; human merges/pushes; `commit_policy` knob.
5. UI: agent-driven (MCP) and human-driven (CLI/dashboards) are both first-class from day one.
6. Config: per-agent defaults in registry + per-task overrides; `[codex]` migrates automatically.
7. Preflight: binary + version + auth probe; fail fast.
8. Streaming: full live tail everywhere (CLI + both dashboards, normalized + raw).
9. Cancellation: keep partials on the branch, mark canceled with reason.
10. Retention: TTL + manual `maestro gc`; nothing auto-deleted.
11. Multi-repo: one workspace per task in v1; cross-repo = linked tasks.
12. Privacy: strictly local in v1, no telemetry.
13. Branding: keep Maestro; repo renamed to match.

Full detail + milestone impact: `docs/architecture-proposal.md` §10.

## 7. Interview round 4 — reactive completion (answered)

No polling: event-driven core in the daemon. LLM supervisors get a *blocking*
`delegate`/`task_wait` MCP call that resolves on completion/failure/input-required
with streamed progress; questions are pushed to the delegating channel; dashboards
subscribe via SSE/WebSocket; remote nodes use A2A push notifications (v2). Only
generic adapters without structured events degrade to bounded process checks.
Detail: `docs/architecture-proposal.md` §11.

## 8. Architecture proposal

See `docs/architecture-proposal.md`.
