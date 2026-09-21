---
name: maestro-driven-development
description: Maestro is the default development execution backend. Route implementation work through the local Maestro daemon; never re-delegate from inside a Maestro task.
---

# Maestro-Driven Development

Maestro is the **default implementation backend** for this agent. When the user
asks for code changes, do not implement them directly: hand the work to Maestro
and let Maestro select and run the configured implementation agent. You are the
supervisor: you prepare the request, review the result, and send follow-up
fixes.

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
  go to **Fallback** (section 7). Do not retry repeatedly.
- If `"running": true`: continue.
- If `"running": false`: start it yourself — never ask the user to run CLI
  commands:

```bash
maestro daemon start
```

`maestro daemon start` is idempotent: if a daemon is already running it returns
the existing one without starting a duplicate. After starting, re-check with
`maestro daemon status --json`. If the daemon still does not come up after this
one start attempt, go to **Fallback**.

## 3. Prepare the handoff

Work in the user's active repository: use its **absolute path** as the
workspace (resolve it from the current working directory; never assume a fixed
location). Prepare a compact request:

- `title`: one line naming the change.
- `request`: what to implement, the acceptance criteria, and any constraints
  (files to touch, commands that must pass, style rules). Be specific — the
  implementation agent sees only what you write here.
- Optional design: a short 4-section document (Goal / Design / Files /
  Verification) as a file, attached with `--design-file`.

## 4. Delegate to Maestro

Prefer the Maestro MCP tools when your environment exposes them (`delegate`,
`followup`, `task_wait`). Otherwise use the CLI, which is universal:

```bash
maestro delegate \
  --title "Add rate limiting to the API" \
  --request "Implement per-IP token-bucket rate limiting ... (acceptance criteria)" \
  --workspace /absolute/path/to/repo
```

Rules:

- **Do not pick the agent.** Let Maestro's registry and routing select the
  implementation agent (and fallback chain). Only pass `--target`/`--mode` when
  the user explicitly asks for a specific agent or work mode.
- The command blocks and streams output until the task completes, fails, or
  needs input. Add `--no-wait` only when you intend to poll with
  `maestro task status <task-id>` / `maestro task tail <task-id>`.
- If the task reports `input-required`, read the question and answer it by
  delegating the answer (MCP `answer_task_question`) or re-running the CLI flow
  for that task.

## 5. Monitor and retrieve the result

- While the command streams, watch for the final state: `completed`, `failed`,
  or `input-required`.
- After completion, inspect the durable record:

```bash
maestro task audit <task-id>      # attempts, usage, errors
maestro task receipt <task-id>    # human-readable execution receipt
```

## 6. Review and follow up

You own the final review decision:

1. Read the diff/changes in the workspace yourself (the work lands on a task
   branch or working tree).
2. Run the verification commands that matter for this repository.
3. If the result is acceptable, report it to the user (files changed, evidence,
   how to verify).
4. If not, send a fix pass **through Maestro** instead of editing the code
   yourself: MCP `followup` with the task id and a precise instruction, or CLI
   re-delegation referencing the same workspace. Repeat at most a few times; if
   fixes do not converge, stop and report the blocker to the user.

## 7. Fallback when Maestro is unavailable

If Maestro cannot be used (CLI missing, daemon fails to start after one
attempt, or repeated failures):

1. State clearly that Maestro orchestration is unavailable and why.
2. Continue with your normal behavior: implement the work directly, following
   the repository's own conventions.
3. Do not enter a retry loop — at most one daemon start attempt per session.

The user can repair Maestro later with `./install.sh` (re-run the installer) or
`maestro doctor`.

## 8. What you never do

- Never delegate from inside a Maestro task (section 0).
- Never hardcode which agent implements — that is Maestro's job.
- Never ask the user to run `maestro daemon start` or other Maestro CLI
  commands yourself when you can run them.
- Never create Maestro tasks for questions, explanations, or summaries.
