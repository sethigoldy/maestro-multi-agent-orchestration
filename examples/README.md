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
| [`quickstart.md`](quickstart.md) | The 5-minute walkthrough in prose form. |

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
