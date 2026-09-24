# Core concepts: tasks, attempts, agents, and receipts

This page explains the four objects at the heart of Maestro — **Task**,
**Attempt**, **Agent**, and **Execution Receipt** — and how they relate. If
you only read one explanation, read this one: every command, screen, and JSON
field in Maestro is a view over these objects.

## The relationship in one picture

```text
  Agent (registered capability)          Task (one unit of work)
  ┌────────────────────────┐             ┌──────────────────────────────┐
  │ name: "codex-mini"     │   runs      │ #42 "Fix the failing test"   │
  │ kind: codex            ├────────────►│ state: completed             │
  │ command/binary, model  │  as any of  │                              │
  └────────────────────────┘  its roles  │ attempts (ordered):          │
                                         │  1. IMPLEMENT  codex     ✓   │
  Agent = *who can do work*              │  2. VERIFY     codex-mini ✓  │
  Task  = *what was done, and how*       │  3. FIX        codex-mini ✓  │
                                         │ verification: PASSED         │
                                         │ gates: verify PASS, …        │
                                         └──────────────┬───────────────┘
                                                        │ projected (read-only)
                                                        ▼
                                        Execution Receipt
                                        (maestro task receipt / HTTP / console)
```

## Task

A **task** is one unit of delegated work — the durable object. It is created
when a handoff is accepted by the daemon and it owns everything that happens:

- **Identity**: a task id (`task-YYYYMMDD-HHMMSS-xxxxxx`), a per-user numeric
  number, a title, and the workspace (and project root) it ran in.
- **State**: an A2A-aligned lifecycle — `submitted → working → completed`,
  with `failed`, `canceled`, and `input-required` (parked for a human answer or
  fix) along the way.
- **Evidence**: its attempts, verification result, gate verdicts, usage/cost,
  error text, and per-task artifacts (logs, results, `verification.txt`).

Tasks live at the **user level** under `~/.maestro` (or `$MAESTRO_HOME`) in an
append-only claim journal plus a registry — not inside any project. That is why
a task survives worktree creation/switching/deletion and daemon restarts: the
daemon re-derives "what is true" from the journal, and nothing important ever
lived only in a terminal buffer.

More than one task can run in a workspace at the same time. The first task runs in the workspace itself. A task delegated
while the workspace is busy runs in a git worktree of its own, under
`~/.maestro/worktrees/<task-id>`, on its own branch, and starts at once. Each
task stays in the directory where it first ran, so its follow-ups continue
there. `run_dir` in the task's status tells you where its work is. At most 4
tasks run at the same time for one workspace (`[defaults] max_parallel`), and
tasks beyond that wait in a queue. The full design is in
`docs/design-parallel-tasks.md`.

## Attempt

An **attempt** is one run of one agent on one phase of a task. It records:

- which **agent** ran it, and in which **role** (`implement`, `verifier`,
  `reviewer`, `fix`) — the role determines the phase label shown everywhere
  (IMPLEMENT / VERIFY / REVIEW / FIX);
- whether it succeeded (`ok`), its exit code, duration, and error text;
- the **usage** the agent reported (tokens, cost) — costs are taken as
  reported, never fabricated: an attempt with no cost reporting shows `—`.

Every delegation produces at least one attempt. Fallback chains add attempts
when a target fails or is unavailable (the failed hop is recorded too — failed
work still costs money). Work modes add verifier/reviewer/fixer attempts as
gate turns. This is why the receipt's "Attempts" section is the single place to
see *exactly who did what, in what order*.

## Agent

An **agent** is a registered capability: how Maestro can launch one coding tool
and read its output. Two layers:

- **Built-in kinds** (`codex`, `claude_code`, `copilot`, `cursor`, `hermes`,
  `pi`, `cline`, `openhands`, `a2a_remote`) — each with a first-class adapter
  that knows the CLI's invocation, output protocol, and usage format. Usable by
  kind name directly; the binary must be on `PATH`.
- **Registered agents** — named entries in `~/.maestro` (via
  `maestro agents add`) that can alias a built-in kind under your own name with
  custom settings, or define a **generic** spec: any non-interactive CLI as a
  command template (`{prompt}` or stdin) with a text/jsonl/rpc output format.

An agent is *capability*, not *role*: the same agent can implement one task and
verify another. Which role it plays in a given task is decided by the handoff's
routing (target/fallback) and, for work modes, by the preset's phase pins.
`maestro agents status <name>` shows exactly what preflight will see before you
delegate; `maestro doctor` lists availability of every known kind at once.

## Execution receipt

The **execution receipt** is a *projection* — a read-only, point-in-time summary
of one task's durable state. It is not a second database: nothing new is stored
when you print it, and it can be rebuilt from the claim journal alone (which is
why it works after a daemon restart).

The receipt answers five questions, in order:

1. **What was the task?** — number, title, workspace, branch, final state.
2. **Who worked on it?** — every attempt with phase, agent, duration, cost, and
   ok/error.
3. **Did it actually work?** — the deterministic verification result (and the
   command that ran), plus any work-mode gate verdicts and bounce count. An LLM
   verdict can only *add* failures; a failed deterministic check is final.
4. **What did it cost?** — aggregated duration and cost, with costs shown only
   when at least one attempt reported them (`cost_reported`).
5. **What is the outcome?** — the final state, with the first error line when
   the task failed.

It has three surfaces, all over the same data:

- `maestro task receipt <id|n>` (human-readable) and `--json` (stable JSON for
  CI and tooling);
- the daemon's HTTP API (`GET /tasks/<id>/receipt`) — used by the CLI when a
  daemon is reachable;
- the web console, which shows it inline on each task's detail pane.

## How they fit together

- You **delegate** a handoff → the daemon creates a **Task**.
- The task's routing picks an **Agent** (and fallbacks); each run of one is an
  **Attempt** in a role.
- Deterministic verification and any gate turns append their own attempts and
  verdicts to the task.
- Everything is journaled as it happens; the **Execution Receipt** is what you
  (or your CI) read back — anytime, from any surface, including after restarts.

For the lifecycle states in depth: [How delegation works](how-delegation-works.md).
For the exact receipt JSON fields: [CLI reference — task receipt](../reference/cli.md#task).
