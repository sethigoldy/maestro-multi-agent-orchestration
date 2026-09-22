---
name: maestro-driven-development
description: Maestro is the default development execution backend. Route implementation work through the local Maestro daemon; never re-delegate from inside a Maestro task. Self-contained: includes the full CLI reference, config schema, and MCP tool contract — do not rediscover Maestro's interface at runtime.
---

# Maestro-Driven Development

Maestro is the **default implementation backend** for this agent. When the user
asks for code changes, do not implement them directly: hand the work to Maestro
and let Maestro select and run the configured implementation agent. You are the
supervisor: you prepare the request, review the result, and send follow-up
fixes.

This skill is **self-contained**: sections 9–11 document the complete CLI,
configuration schema, and MCP tool contract. Do not read Maestro source, README,
or docs to learn how to use it — everything you need is here.

## 0. Recursion guard (check FIRST, every time)

Before anything else, check your environment:

```bash
printenv MAESTRO_AGENT_CONTEXT
```

If `MAESTRO_AGENT_CONTEXT` is set to `1`, **you are already executing inside a
Maestro task** (as its implementation worker, `MAESTRO_TASK_ID` names the task).
In that case:

- Do NOT delegate this work back to Maestro. Do not run `maestro daemon ...`,
  `maestro delegate ...`, or any Maestro MCP tool for this task.
- Perform the assigned implementation directly in your working directory.
- Stop recursing: one hop maximum, always.

Only continue with the workflow below when `MAESTRO_AGENT_CONTEXT` is unset.

## 1. When to route through Maestro

**Trigger Maestro** for development work that modifies code or repository state:

- implementing a feature or new functionality
- modifying existing code (any non-trivial change)
- fixing a bug
- refactoring
- adding or fixing tests
- debugging with code changes
- changing APIs, schemas, or infrastructure code
- significant documentation changes tied to code
- repository maintenance that involves actual code modification

**Do NOT trigger Maestro** for:

- explaining or describing code
- answering conceptual or architectural questions
- summarizing files or repositories
- discussing architecture without implementing it
- inspecting a repository without making changes
- conversational clarification or planning-only turns
- final user communication (your own summary/review messages)

When in doubt about whether the request involves code modification, treat it as
implementation work and route it. Do not create a Maestro task for every user
message — only when real development work is requested.

## 2. Ensure the daemon is available

Check once per session (not on every single step):

```bash
maestro daemon status --json
```

- If `maestro` is not on PATH, or the command fails: Maestro is unavailable —
  go to **Fallback** (section 8). Do not retry repeatedly.
- If `"running": true`: continue.
- If `"running": false`: start it yourself — never ask the user to run CLI
  commands:

```bash
maestro daemon start
```

`maestro daemon start` is idempotent: if a daemon is already running it returns
the existing one without starting a duplicate. After starting, re-check with
`maestro daemon status --json`. If the daemon still does not come up after this
one start attempt, go to **Fallback**. For a full diagnosis (state dir, git,
agents, workspace) run `maestro doctor`.

**Agent environment.** Every agent inherits the daemon's full environment plus
a snapshot of your login shell (`$SHELL -lc env`) taken once per daemon
process — so profile exports such as API keys reach the agent even when the
daemon was started from a GUI, launchd, or an older terminal. If you add a new
environment variable that an agent needs, restart the daemon
(`maestro daemon restart`). Set `MAESTRO_LOGIN_ENV=0` to disable the snapshot.

## 3. Routing: how Maestro picks an agent (and when it asks you)

You never pick the implementation agent yourself — unless the user explicitly
names one. Resolution order when a handoff is submitted:

1. **Explicit target in the handoff** (`--target`, or `target_agent` in a
   `[routing]` table, or a work-mode preset's implementer) wins.
2. Otherwise Maestro reads the project's config chain —
   `$MAESTRO_HOME/config.toml` (default `~/.maestro/config.toml`), then
   `<project-root>/.maestro/config.toml`, then
   `<workspace-root>/.maestro/config.toml` (later files win per key) — and uses
   the `[defaults]` table:

   ```toml
   [defaults]
   agent    = "codex"          # default implementation agent
   fallback = ["claude_code"]  # optional fallback chain
   model    = "gpt-5.6-luna"   # optional; applied when the handoff sets none
   effort   = "max"            # optional: low | medium | high | xhigh | max
   ```

3. If **neither** the handoff nor `[defaults]` names an agent, Maestro does not
   guess: the task parks in state `input-required` with a question listing every
   registered agent (name + detected version). Read the question
   (`maestro task status <task-id>` or the MCP return value), then:
   - **Ask the user** which agent (and model) to use, and answer via MCP
     `answer_task_question(workspace, task_id, answer)` — there is no CLI answer
     command, so without MCP tools relay the question to the user instead of
     guessing. Accepted answer forms: a bare agent name (`codex`), key=value
     pairs (`agent=codex model=gpt-5.6-luna`), or JSON
     (`{"agent": "codex", "model": "gpt-5.6-luna"}`). A bad answer leaves the
     task parked for another attempt.
   - To stop being asked on future tasks, write a `[defaults]` table into
     `<project-root>/.maestro/config.toml` (see section 10).

The same parking happens for **sensitive workspaces** (question: "Approval
required: this task targets a sensitive workspace.") — confirm with the user,
then answer to resume.

## 4. Prepare the handoff

Work in the user's active repository: use its **absolute path** as the
workspace (resolve it from the current working directory; never assume a fixed
location). Prepare a compact request:

- `title`: one line naming the change.
- `request`: what to implement, the acceptance criteria, and any constraints
  (files to touch, commands that must pass, style rules). Be specific — the
  implementation agent sees only what you write here.
- Optional design: a short 4-section document (Goal / Design / Files /
  Verification) as a file, attached with `--design-file`.

A handoff file may be TOML or JSON. Minimal shape (TOML):

```toml
[routing]
target_agent = "codex"        # optional — omit to use [defaults]/ask the user
fallback     = ["claude_code"]  # optional
# mode = "economy"             # optional — work-mode preset from config [modes]

[work]
title   = "Add rate limiting"
request = "Implement per-IP token-bucket rate limiting. Acceptance: tests in tests/test_rate.py pass."
```

## 5. Delegate to Maestro

Prefer the Maestro MCP tools when your environment exposes them (`delegate`,
`followup`, `task_wait`). Otherwise use the CLI, which is universal:

```bash
maestro delegate \
  --title "Add rate limiting to the API" \
  --request "Implement per-IP token-bucket rate limiting ... (acceptance criteria)" \
  --workspace /absolute/path/to/repo
```

Rules:

- **Do not pick the agent.** Omit `--target`/`--mode` and let Maestro resolve
  routing per section 3. Pass them only when the user explicitly asks for a
  specific agent or work mode.
- The command blocks and streams output until the task completes, fails, or
  needs input. Add `--no-wait` only when you intend to poll with
  `maestro task status <task-id>` / `maestro task tail <task-id>`.
- If the task reports `input-required`, read the question (section 3 explains
  both kinds: routing and sensitive-workspace approval) and answer it — never
  cancel-and-restart to dodge a question.

## 6. Monitor and retrieve the result

- While the command streams, watch for the final state: `completed`, `failed`,
  or `input-required`.
- After completion, inspect the durable record:

```bash
maestro task audit <task-id>      # attempts, usage, errors
maestro task receipt <task-id>    # human-readable execution receipt
maestro task tail <task-id>       # live event stream (SSE)
```

## 7. Review and follow up

You own the final review decision:

1. Read the diff/changes in the workspace yourself (the work lands on a task
   branch or working tree).
2. Run the verification commands that matter for this repository.
3. If the result is acceptable, report it to the user (files changed, evidence,
   how to verify).
4. If not, send a fix pass **through Maestro** instead of editing the code
   yourself: MCP `followup` with the task id and a precise instruction, or the
   CLI equivalent `maestro task continue <task-id> --request "…"` (both reuse
   the same task, workspace, branch, and routing; both inject a compact
   task-knowledge snapshot by default — pass `--context fresh` /
   `context_mode="fresh"` for a clean reasoning context). Repeat at most a few
   times; if fixes do not converge, stop and report the blocker to the user.

## 8. Fallback when Maestro is unavailable

If Maestro cannot be used (CLI missing, daemon fails to start after one
attempt, or repeated failures):

1. State clearly that Maestro orchestration is unavailable and why.
2. Continue with your normal behavior: implement the work directly, following
   the repository's own conventions.
3. Do not enter a retry loop — at most one daemon start attempt per session.

The user can repair Maestro later with `./install.sh` (re-run the installer) or
`maestro doctor`.

## 9. CLI reference (complete)

Global options (valid on every command): `--workspace PATH` (scope tasks to a
directory; defaults to `$MAESTRO_WORKSPACE` or CWD), `--project ROOT` (scope by
project root), `--version`, `-h/--help`.

### delegate

```text
maestro delegate [--file FILE | --title T --request R] [--target AGENT] [--mode NAME]
                 [--fallback AGENT ...] [--design-file PATH] [--context TEXT ...]
                 [--context-file PATH ...] [--skill DIR ...] [--no-wait]
```

- `--file`: handoff document (TOML or JSON); `--title`/`--request` build one on
  the fly. Without `--target`/`--mode`, routing resolves from config `[defaults]`
  or parks with a question (section 3).
- `--fallback`: repeatable fallback agents, tried in order when the target fails.
- `--context TEXT` / `--context-file PATH` / `--skill DIR`: inject standing
  context into the agent turns (text entry, inlined file, or Agent Skills
  directory containing SKILL.md).
- Blocks until terminal state or `input-required`; prints `[state] …` lines and
  final task JSON.

### task

```text
maestro task list                     # all tasks (id, state, workspace)
maestro task status <task-id>         # full status incl. question when input-required
maestro task show <task-id>           # alias for status
maestro task tail <task-id>           # live event stream (SSE, no polling)
maestro task audit <task-id>          # durable audit record: attempts, usage, errors
maestro task receipt <task-id>        # execution receipt: attempts, verification, gates, totals
```

### Top-level commands

```text
maestro status <task-id>              # alias of `task status`
maestro list                          # alias of `task list`
maestro config                        # effective config JSON: codex defaults, [defaults], modes, context
maestro doctor                        # diagnose state dir, daemon, git, agents, workspace
maestro budgets                       # budget caps (MAESTRO_BUDGET_*_USD) and current spend
maestro gc                            # delete terminal tasks older than the TTL (manual only)
maestro dashboard                     # terminal SSE dashboard (no polling)
maestro peers ...                     # manage discovered/registered Maestro peers
maestro storage migrate-memvara       # import legacy filesystem state into memvara
```

### daemon

```text
maestro daemon start                  # idempotent; detaches, survives shell exit
maestro daemon stop                   # SIGTERM → grace (MAESTRO_DAEMON_STOP_GRACE_S) → SIGKILL
maestro daemon status [--json]        # running/pid/port/url or false
maestro daemon restart                # stop + start
```

### agents

```text
maestro agents list                   # registered agents (name, kind, model, effort)
maestro agents add --name N --kind K [--model M] [--effort E] ...   # register one
maestro agents remove <name>
maestro agents discover               # scan PATH for known agent CLIs
maestro agents register-discovered [--dry-run]  # register found CLIs; keeps existing registrations
maestro agents status <name>          # registration + availability (binary, version)
```

Built-in kinds: `codex`, `claude_code`, `hermes`, `pi`, `cline`, `openhands`,
`cursor`, `copilot`, `opencode`, `a2a_remote`, plus `generic` (declarative,
config-only).

### skill

```text
maestro skill list                    # the managed skill + supported agents
maestro skill status                  # per-agent detection/installation status (JSON)
maestro skill install [--agent NAME | --all]   # install/update (update = delete + reinstall)
maestro skill uninstall [--agent NAME]         # remove from one agent or everywhere installed
```

## 10. Configuration reference

Config files (TOML), merged in order — later wins per key:

1. `$MAESTRO_HOME/config.toml` (default `~/.maestro/config.toml`) — user level
2. `<project-root>/.maestro/config.toml` — project level
3. `<workspace-root>/.maestro/config.toml` — active worktree level

```toml
[defaults]                 # routing defaults (section 3)
agent    = "codex"
fallback = ["claude_code"]
model    = "gpt-5.6-luna"
effort   = "max"           # low | medium | high | xhigh | max

[codex]                    # Codex-specific defaults (shown by `maestro config`)
model  = "gpt-5.6-luna"
effort = "max"             # env fallbacks: MAESTRO_CODEX_MODEL / MAESTRO_CODEX_EFFORT

[verification]
command = ["make", "check"]  # stored for forward compatibility (string or list)

[storage]
backend = "filesystem"     # filesystem (default) | memvara; env fallback MAESTRO_STORAGE

[modes.economy]            # work-mode presets: pin agents to task phases
implementer = "codex-mini" # required — becomes the target when a handoff sets no explicit target
verifier    = "codex-mini" # optional LLM verification pass (deterministic check always runs first)
reviewer    = "codex"      # optional LLM review gate over the requested diff
fixer       = "codex-mini" # auto-fix bounces after a failed gate (default: implementer)
max_bounces = 2            # cap on auto-fix bounces; 0 parks on the first issue

[context.style]            # standing context entries, keyed by label
text = "Follow docs/STYLE.md."

[context.pdf-skill]
kind   = "skill"           # text (default) | file | skill
path   = "~/skills/pdf-processing"
phases = ["implementer"]   # optional — default: all phases
```

Relevant environment variables: `MAESTRO_HOME`, `MAESTRO_WORKSPACE`,
`MAESTRO_STORAGE`, `MAESTRO_CODEX_MODEL`, `MAESTRO_CODEX_EFFORT`,
`MAESTRO_DELEGATE_TIMEOUT` (seconds, default 3600), `MAESTRO_BUDGET_*_USD`
(per-agent budget caps), `MAESTRO_DAEMON_URL`, `MAESTRO_DAEMON_TOKEN`,
`MAESTRO_DAEMON_STOP_GRACE_S`, `MAESTRO_MAX_RETRIES`, `MAESTRO_BACKOFF_S`,
`MAESTRO_LOGIN_ENV` (set `0` to stop passing login-shell env vars to agents).

## 11. MCP tool contract

When your environment exposes the Maestro MCP server, these tools are available
(they target the daemon; always pass the workspace absolute path):

- **`delegate(workspace, file)`** — submit a handoff file and **block until the
  task completes, fails, or needs input** (no polling). Returns the final A2A
  task object (state, artifacts, workspace/branch metadata). If the workspace
  already has an active task you get `{"queued": true, …}` immediately. Routing
  defaults and the input-required question behave exactly as in section 3.
- **`followup(workspace, task_id, instruction, context_mode="reuse")`** — send a precise fix pass to a
  finished/failed task; blocks until that follow-up turn settles. `context_mode="reuse"` (default) injects the compact task-knowledge snapshot; `"fresh"` starts a clean reasoning context.
- **`task_wait(workspace, task_id)`** — block on an earlier (e.g. queued or
  `--no-wait`) task until it reaches a terminal state or needs input.
- **`task_status(workspace, task_id)`** — current status object (state, question
  when input-required, artifacts).
- **`list_tasks(workspace?)`** — tasks in the workspace (or all).
- **`agents_list()`** — registered agents with availability (name, kind, model,
  version) — use it to answer a routing question accurately.
- **`answer_task_question(workspace, task_id, answer)`** — answer an
  input-required task (routing selection or sensitive-workspace approval) and
  resume it.
- **`cancel_task(workspace, task_id)`** — cancel a running/queued task.

## 12. What you never do

- Never delegate from inside a Maestro task (section 0).
- Never hardcode which agent implements — that is Maestro's job (section 3).
- Never ask the user to run `maestro daemon start` or other Maestro CLI
  commands yourself when you can run them.
- Never create Maestro tasks for questions, explanations, or summaries.
- Never cancel-and-restart a task to dodge an input-required question — answer
  it instead.
- Never read Maestro source or docs to learn its interface — this skill is the
  complete contract.
