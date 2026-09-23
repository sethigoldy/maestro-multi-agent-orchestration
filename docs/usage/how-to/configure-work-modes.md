# How to configure work modes

This guide shows you how to set up work-mode presets — named profiles that pin
specific agents (and their models) to the phases of a task cycle, so cost and
quality profiles are config instead of prompt discipline. By the end you will
have an "economy" mode where a cheap model implements, verifies, and fixes, and
an expensive model reviews only the requested changes.

For the design rationale and invariants (why an LLM verdict can never override
a failed deterministic check), see
[How delegation works](../explanation/how-delegation-works.md#work-modes-gates-and-bounces).
Field-by-field reference: [handoff format](../reference/handoff-format.md) and
[configuration](../reference/configuration.md#modes--work-mode-presets).

## Prerequisites

A running daemon (`maestro-daemon`) and at least two registered agents — or one
registered agent plus a built-in kind — so the cheap and expensive tiers exist
as names Maestro can route to.

## Step 1: Register your model tiers as agents

A tier is just a registry entry with its own name, model, and effort. Two
entries of the same CLI are all it takes:

```bash
# Cheap tier: small model, low effort — does the implementation work
maestro agents add --name codex-mini --kind codex --model gpt-5-mini --effort low

# Expensive tier: big model, default effort — reviews only
maestro agents add --name codex-pro --kind codex --model gpt-5
```

Verify both pass preflight before relying on them in a preset:

```bash
maestro agents status codex-mini
maestro agents status codex-pro
```

Any registered agent or built-in kind can fill any slot — including the same
agent in two slots (e.g. `verifier = "codex-mini"` with a different
`implementer`).

## Step 2: Define a preset in your config

Presets live in `[modes.<name>]` tables in the existing config chain —
`~/.maestro/config.toml` for you, or `<project>/.maestro/config.toml` for a
project (project wins per preset name). Example project config:

```toml
# .maestro/config.toml
[modes.economy]
implementer = "codex-mini"     # required — the agent that implements
verifier    = "codex-mini"     # optional LLM verification pass
reviewer    = "codex-pro"      # expensive: verifies requested changes only
fixer       = "codex-mini"     # optional → defaults to implementer
max_bounces = 2                # optional → default 2; 0 = no auto-fix, park on first issue
```

Every key except `implementer` is optional — each omission falls back to what
Maestro already does:

| Slot | Phase | Omitted means |
|---|---|---|
| `implementer` (required) | implementing | the handoff's explicit target, or its default |
| `verifier` | verifying | deterministic check only (today's behavior) |
| `reviewer` | reviewing | no LLM review turn; you/supervisor review as before |
| `fixer` | fixing | bounces go back to the task's implementer (the handoff's explicit target when it names one, otherwise the preset's `implementer`) |

Check that Maestro parsed them:

```bash
maestro config
# {"model": …, "effort": …, "modes": {"economy": {"implementer": "codex-mini", "verifier": "codex-mini", "reviewer": "codex-pro", "fixer": "codex-mini", "max_bounces": 2}}}
```

## Step 3: Delegate with the preset

From flags — `--mode` stands in for `--target` (the preset's implementer
becomes the task's target):

```bash
maestro delegate \
  --title "Add pagination to the documents API" \
  --request "Add cursor-based pagination; page_size defaults to 50." \
  --mode economy \
  --workspace /path/to/repo
```

From a handoff file:

```toml
[routing]
mode = "economy"
```

A flag beats the file (`maestro delegate --file h.toml --mode other`), and any
explicit routing field beats the preset for that task:

```toml
[routing]
mode = "economy"
review_agent = "claude-max"   # only this review turn uses a different agent
```

## Step 4: Read per-phase cost

Every gate and fix turn runs as its own attempt, attributed to its own agent.
Watch where the money goes:

```bash
maestro task audit <task-id>
# attempts: [{"agent": "codex-mini", …}, {"agent": "codex-mini", …},
#            {"agent": "codex-pro", …}, …]   ← one entry per turn, per agent
maestro budgets
# codex-mini: $0.4120    ← cheap tier's total spend
# codex-pro:  $0.9870    ← expensive tier spent only on review turns
```

The task record also carries `gates` (each LLM gate's verdict and issues) and
`bounces` (auto-fix bounces consumed), visible in `maestro task status <id>`
and in the MCP task metadata.

## Step 5: Handle a parked task

When a gate fails, Maestro bounces the work to the fixer — re-verify,
re-review — up to `max_bounces` times. If issues remain at the cap (or a gate
turn itself can't run), the task parks in `input-required` with a question that
lists every unresolved issue. It keeps its workspace slot while parked; answer
it, follow up, or cancel:

```bash
maestro task status <task-id>     # shows state input-required + the question
maestro task tail <task-id>       # stream: you'll see the (question: …) annotation
```

- **Answer it** — resume on the task branch with your instructions appended:
  `answer_task_question` via MCP, or the equivalent in your supervisor's flow.
  The answer runs under the implementer.
- **Follow up** — once terminal, `followup` continues the work; for a
  work-mode task it runs under the mode's `fixer`, since that agent is already
  pinned to fixing.
- **Cancel** — stop it and free the workspace slot; partial work stays on the
  task branch.

A parked question also names what blocked: a failed deterministic check
("Deterministic verification: FAILED"), a gate turn that crashed, an agent that
asked its own question, or a missing parsable `VERDICT:` line in a gate's
output. Read it — retrying the same delegation just spends another bounce cap.

## Troubleshooting

- **"Unknown work mode 'x'. Defined modes: …"** — the preset name is misspelled
  or defined in a config file that isn't in scope for this workspace. Check
  `maestro config`.
- **"Unknown reviewer agent 'y'. Registered agents: …"** — the slot names an
  agent that isn't registered (or was removed). Register it, or fix the preset.
- **"review_agent 'a' cannot equal the implementer"** — self-review is refused;
  pick a different review agent.
- **The task parks immediately with "no parsable VERDICT line"** — your gate
  agent's output must end with a `VERDICT: PASS` or `VERDICT: FAIL` line (plus
  optional `ISSUES:` bullets). Check its raw output in the task's result files.
