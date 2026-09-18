# How to delegate a task

This guide shows you how to author a handoff — the work order Maestro sends to
an agent — and launch it: via CLI flags, via a handoff file, with fallbacks,
per-task settings, work-mode presets, budgets, and queueing. For the complete
field-by-field description of the handoff document, see the
[handoff format reference](../reference/handoff-format.md).

## Prerequisites

A running daemon (`maestro-daemon`), at least one registered or built-in agent
([register-an-agent.md](register-an-agent.md)), and a target workspace — a
directory that is a git repository unless you use `commit_policy = "no-commit"`.

## Author the handoff with CLI flags

The minimal delegation:

```bash
maestro delegate \
  --title "Fix the failing test" \
  --request "Find and fix the failing unit test in tests/, then rerun the suite." \
  --target codex \
  --workspace /path/to/repo
```

`--title`, `--request`, and `--target` are required together. Add options as
the job needs them:

- `--fallback <agent>` — repeatable; tried in order if the target fails or is
  unavailable.
- `--design-file <path>` — attaches a design document (its full text becomes
  the handoff's authoritative design).
- `--no-wait` — return immediately after enqueueing instead of streaming.

This command blocks by default and streams the agent's output live; see
[Manage in-flight tasks](manage-in-flight-tasks.md) for steering it while it
runs.

## Author the handoff as a file

For anything beyond a title, request, and target, write a handoff file (TOML
or JSON) and pass `--file`:

```bash
maestro delegate --file handoff.toml --workspace /path/to/repo
```

A complete TOML example using the 4-section format:

```toml
[handoff]
title = "Add pagination to the documents API"
request = """
Add cursor-based pagination to GET /documents. Keep existing callers working;
the new page_size parameter defaults to 50.
"""
design = """
- New query params: cursor (opaque token), page_size (int, default 50).
- Response gains next_cursor when more results exist.
- Reuse the existing repository layer; no schema changes.
"""
context_files = ["src/api/documents.py", "tests/test_documents.py"]
context_notes = "The API is FastAPI; follow the error shapes in src/api/errors.py."

[routing]
target_agent = "codex"
fallback = ["copilot"]
origin_agent = "human"        # who asked for this work (you, or a supervising agent)

[expectations]
artifacts = ["code", "tests"]
verification = "auto"         # auto | command | none
commit_policy = "branch"      # branch (default) | no-commit | pr
budget_hint = 5.0             # recorded on the task; caps are enforced via env vars

[constraints]
sensitive = false             # true pauses the task for approval before any agent runs
max_depth_remaining = 3       # how many further nested delegations/follow-ups remain

[agent_settings]
model = "gpt-5.6-luna"        # per-task override of the agent's registry defaults
effort = "high"
```

The same shape works as JSON with identical keys. Legacy 0.8.x staged handoff
files (`{"title", "request", "design_file", "model", "effort"}`) are still
accepted and converted automatically — see the
[handoff format reference](../reference/handoff-format.md#legacy-08x-format).

## Per-task model and effort

The CLI flags do not expose model/effort; per-task overrides live in the
handoff file's `[agent_settings]` table (as above). Precedence, most specific
first:

1. `agent_settings` in this handoff (per task)
2. the agent's registry entry (per agent, set at registration)
3. the agent adapter's own defaults

`effort` accepts `low`, `medium`, `high`, `xhigh`, `max`.

## Delegate with a work mode

A work-mode preset pins agents to the phases of the task cycle (cheap model
implements, expensive model reviews, …). If you haven't defined presets yet,
do that first: [Configure work modes](configure-work-modes.md).

From flags — `--mode` stands in for `--target` (the preset's implementer
becomes the target):

```bash
maestro delegate \
  --title "Add pagination to the documents API" \
  --request "Add cursor-based pagination; page_size defaults to 50." \
  --mode economy \
  --workspace /path/to/repo
```

From a handoff file — set `[routing] mode`:

```toml
[routing]
mode = "economy"
```

Overriding one slot for a single task: any explicit routing field beats the
preset, so this reviews with a different agent while keeping the rest of the
preset:

```toml
[routing]
mode = "economy"
review_agent = "claude-max"   # just this review turn uses the expensive tier
```

And from flags, `--target` beats the preset's implementer:
`maestro delegate --title … --request … --target codex --mode economy`.

What you get: after the implementer finishes, Maestro runs deterministic
verification, then the preset's verifier and reviewer turns (if set). A failed
verdict or a failed deterministic check bounces the work to the fixer — up to
`max_bounces` times — and if the cap is hit the task parks in `input-required`
with the unresolved issues listed; answer it, follow up, or cancel. Per-phase
cost shows up in `maestro task audit <id>` (each attempt names its agent) and
in `maestro budgets`.

## Choose the commit policy

- `branch` (default) — Maestro creates and checks out branch
  `maestro/<task-id>` in the workspace before the agent starts. The workspace
  must be a git repository; delegation is refused with a clear error otherwise.
- `no-commit` — no branch is created; the agent works directly in your working
  tree. Use for non-git directories or throwaway sandboxes.
- `pr` — same branch behavior as `branch`; kept for handoff documents that
  express the intent to end with a pull request (creating it is still your job).

## Budgets and launch-time refusal

Budget caps are set in the daemon's environment, not per delegation:

```bash
export MAESTRO_BUDGET_PER_AGENT_USD=25   # cumulative cap per agent name
export MAESTRO_BUDGET_DAILY_USD=100      # all agents, resets at UTC midnight
maestro-daemon
```

When a cap is exhausted, new delegations are refused with a clear error;
running tasks always finish. `budget_hint` in the handoff is recorded on the
task for visibility but does not enforce anything by itself. Check current
spend anytime with `maestro budgets`. Details:
[configuration reference](../reference/configuration.md#budget-caps).

## Queueing and no-wait

One workspace runs one active task at a time. If you delegate to a busy
workspace, the new handoff is queued FIFO and starts when the slot frees:

```bash
maestro delegate --file next.toml --workspace /path/to/repo
# {
#   "queued": true,
#   "reason": "workspace already has an active task; this handoff is next in line"
# }
```

With `--no-wait`, the command returns immediately after enqueueing and prints
the daemon's response (task id, or the queued notice). Follow the task with
`maestro task tail <task-id>` or check it later with `maestro task status`.

## Rules that will refuse your delegation

These fail fast, before any agent runs — read the error rather than retrying:

- **Self-delegation** — a handoff whose `target_agent` equals its
  `origin_agent` is refused (an agent cannot delegate to itself for
  self-review). If you are delegating from a host agent via MCP, set
  `origin_agent` to that agent's name and pick a different target.
- **Depth exhausted** — `max_depth_remaining` at or below zero refuses further
  nesting; each follow-up decrements it by one (default 3).
- **Workspace not a directory**, or **not a git repository** with
  `commit_policy = "branch"`.
- **Budget cap exceeded** for the target agent.
- **Work-mode problems** — an unknown `mode` name, an unknown agent in any
  preset slot or explicit gate field (the error lists the registered agents),
  or `review_agent == target_agent` (self-review is not a gate).

## Delegate from a host agent (MCP path)

If a supervising agent (Claude Code, or any MCP client) drives Maestro, it
calls the `delegate` tool with a handoff *file* — same daemon, same queueing,
same states; the call blocks until the work completes, fails, or needs input.
See the [MCP tools reference](../reference/mcp-tools.md#delegate).
