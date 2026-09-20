# Maestro examples

Ready-to-adapt handoff files for `maestro delegate --file <name>`, plus the
flagship end-to-end demo. Handoffs are TOML (JSON works too) in the 4-section
format: `[handoff]` (what to do), `[routing]` (who does it), `[expectations]`
(what done looks like), `[constraints]` (guardrails). Full field reference:
[docs/usage/reference/handoff-format.md](../docs/usage/reference/handoff-format.md).

| File | Shows |
|---|---|
| [`basic.toml`](basic.toml) | The smallest complete handoff — one agent, auto-verification, no commits. |
| [`fallback.toml`](fallback.toml) | A fallback chain: primary agent plus two alternates tried in order; every attempt (and its cost) is recorded. |
| [`work-mode-production.toml`](work-mode-production.toml) | Work modes — a cheap implementer/fixer, an expensive verifier/reviewer, bounded bounces, and a compact design section. |
| [`context.toml`](context.toml) | Context injection — typed `[[context]]` entries (text, file, Agent Skill) scoped to phases. |
| [`full-swap.sh`](full-swap.sh) | The flagship demo: Claude Code supervises Codex through Maestro's MCP tools, then the CLI delegates a follow-on task to another agent; both durable audit records are printed. Requires real `claude`/`codex` (and `hermes`) CLIs. |
| [`overnight.toml`](overnight.toml) | One member of an **overnight batch**: independent tasks in separate workspaces, submitted before you go offline and inspected via receipts in the morning. No scheduler — just durability. |
| [`quickstart.md`](quickstart.md) | The 5-minute walkthrough in prose form. |

## Overnight batches (multiple independent tasks)

Maestro has no built-in scheduler by design; an "overnight batch" is simply a
set of independent delegations that run while you're away:

1. **One workspace per task.** The daemon runs one active task per workspace,
   but different workspaces proceed in parallel. Give each task its own clone
   or git worktree so they never queue behind each other.
2. **Submit them all**, either blocking (each `maestro delegate` call waits for
   its task) or fire-and-forget with `--no-wait` (the task is queued and starts
   as soon as its workspace is free):

   ```bash
   maestro delegate --file examples/overnight.toml --workspace ~/batches/task-1
   maestro delegate --file examples/overnight.toml --workspace ~/batches/task-2 --no-wait
   maestro delegate --file examples/overnight.toml --workspace ~/batches/task-3 --no-wait
   ```

3. **Walk away.** Durable state lives in `$MAESTRO_HOME` on disk; daemon
   restarts and machine sleep/wake do not lose tasks, attempts, or costs.
4. **Inspect in the morning:**

   ```bash
   maestro task list --workspace ~/batches/task-1     # one per workspace
   maestro task receipt <task-id>                     # full story per task
   maestro task receipt <task-id> --json              # machine-readable
   ```

   Tasks that finished show their receipts; tasks parked in `input-required`
   are waiting for your answer — the receipt tells you exactly what they need.

## Running one

```bash
# 1. Make sure the environment is usable:
maestro doctor

# 2. Start a daemon (if one isn't already running):
maestro-daemon

# 3. Delegate an example into a real repository:
maestro delegate --file examples/basic.toml --workspace /path/to/your/repo

# 4. Close the loop:
maestro task receipt <task-id>          # what happened, who it cost, did it verify?
```

Notes:

- Replace the agent names (`codex`, `claude_code`, …) with agents that are
  actually installed on your machine — `maestro agents discover` lists them.
- `verification = "auto"` auto-detects the project's test runner (plus
  `git diff --check`, which always runs); set it to `"none"` for scratch work,
  or `"command"` with the shell command placed in the handoff's `request` field
  for a fully custom check.
- Nothing here commits code: `commit_policy = "branch"` leaves your changes on
  a task branch for you to review; `"no-commit"` doesn't touch the branch at all.
