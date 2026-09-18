# How delegation works

This is an explanation of *why* Maestro is shaped the way it is: one daemon as
broker, a role-agnostic handoff document, durable user-level state, and
deterministic verification. For how to operate any of this, see the
[how-to guides](../README.md#how-to-guides); for exact commands and fields, see
the [reference](../README.md#reference).

## Why a daemon at all

Maestro's first engine (0.8.x) delegated by spawning a worker subprocess per
task: stage a handoff file, launch `python -m maestro.worker`, let it run an
agent CLI to completion. That worked for the one case it was built for — a
supervising agent handing work to Codex — and it had two structural problems.

The first was **state**. A spawned worker has no memory of its own; everything
durable had to be written by convention (staged files, result files), and the
moment the process died or the machine rebooted, "what is happening right now"
was unrecoverable. There was no queue, no way to answer an agent mid-task, no
way to resume a finished task with context.

The second was **roles**. The worker's prompts hardcoded who was doing what:
"Claude is the supervisor, Codex implements." The engine could not express any
other arrangement — Copilot supervising Hermes, a human delegating to Claude
Code, one Maestro daemon delegating to another — without new code.

The current design answers both with the same move: **one long-running broker
per machine** (`maestro-daemon`) that owns the state and runs tasks as
adapters against registered agents. A delegation is a message to the daemon,
not a process launch. Consequences:

- State survives by construction — the daemon *is* the state, persisted to an
  append-only journal (below).
- Any registered agent can be target or origin; roles are data in the handoff,
  not assumptions in prompts.
- One active task per workspace with FIFO queueing falls out of the broker
  model: the daemon simply tracks which workspace slot is busy.

The trade-off is that you now run a service. In practice it is cheap (one
process, no external dependencies), it prints everything you need on startup,
and `daemon.json` makes "which daemon is live" a one-file question.

## Task lifecycle

A task moves through six A2A states; the transitions are driven by the daemon,
never by callers:

```text
submitted ──▶ working ──▶ completed
   │            │  ▲         
   │            ▼  │         
   │     input-required (question answered → back to working)
   │            │
   ├──▶ canceled (user cancel, running or queued)
   └──▶ failed   (every agent in the target→fallback chain failed)
```

- **submitted** — accepted and registered; about to start (or paused for
  approval when `sensitive = true`, which goes straight to `input-required`).
- **working** — an agent is running on the task branch. Retries and fallback
  hops stay inside this state; each attempt is recorded separately.
- **input-required** — the agent asked a question (or a sensitive task awaits
  approval). The task waits here indefinitely until answered.
- **completed** — an agent finished successfully *and* verification ran. Note
  that "completed" already includes the deterministic check; it is not merely
  "the agent said done." A task whose check fails still reaches `completed` —
  with a `FAILED: <report>` marker on its verification field — because the
  agent's turn succeeded and the evidence says otherwise. Reviewing that marker
  (or sending a follow-up) is the origin side's job; silently marking the task
  `failed` would conflate "the work needs rework" with "no agent could do it."
- **failed** — every agent in the target→fallback chain failed (or preflight
  rejected each of them). The last error is recorded, and an escalation event
  is published.
- **canceled** — user-canceled while running or queued; partial work remains on
  the branch.

Callers see a coarser human-level phase, written to durable state at every
transition:

| A2A state | Phase | Meaning in plain terms |
|---|---|---|
| `submitted` | `DESIGNED` | accepted, not yet running |
| `working` | `IMPLEMENTING` | an agent is working |
| `input-required` | `REVIEWING` | paused for a question/approval |
| `completed` | `REVIEWING` | work done, awaiting your review |
| `failed` / `canceled` | `FAILED` | terminal negative (audit tells which) |

`REVIEWING` deliberately covers both "waiting on you" and "you have something
to look at": in Maestro's model, a finished task is not *done* until the
origin side has reviewed it. That is why the phase name is about review rather
than success.

## Role-agnostic routing

The handoff carries `origin_agent` (who asked) and `target_agent` (who does
the work) as plain data, plus a fallback chain. Nothing in the daemon, the
adapters, or the prompts assumes who fills either role — the prompt given to an
implementation agent says "you are the implementation agent for a Maestro task"
and nothing more about who is supervising.

Two rules enforce sanity rather than identity:

- **No self-delegation.** `target_agent == origin_agent` is refused. The reason
  is epistemic, not organizational: an agent reviewing its own work has no
  independent signal, so a self-loop would manufacture the appearance of
  cross-review without any of it. If you want iteration, that is what
  follow-ups are for — explicit, bounded turns on the same task.
- **Bounded depth.** `max_depth_remaining` (default 3) decrements with each
  nested delegation or follow-up, so agent chains cannot recurse forever.

The historical alternative — a dedicated supervisor role baked into the engine
— was removed rather than generalized: keeping "Claude supervises Codex" as a
special case while claiming any-agent routing would have left two engines and
two sets of assumptions in one codebase. One engine, roles as data.

## Durable state: the claim journal

All task state lives at the **user level** (`~/.maestro`), not inside projects
or worktrees. The core structure is an append-only journal (`state.jsonl`) of
claims: subject `maestro:task:<id>`, a predicate (`task_status`, `task_branch`,
`task_workspace`, `task_runtime`, …), and an object. "Current state" means the
latest claim per predicate — history is never overwritten, only extended.

Why this shape:

- **Worktrees come and go.** A task's workspace may be deleted after the work
  is merged; the record must outlive it. User-level state does exactly that,
  and each record stores both the workspace path and the project root so tasks
  stay attributable across worktree churn.
- **The daemon is one of several readers.** CLI commands, MCP tools, the web
  console, and a *new* daemon process all read the same journal. When a daemon
  restarts, tasks from earlier runs remain visible (their runtime snapshot was
  journaled), and `daemon.json` — liveness-checked against the recorded pid —
  tells clients which broker is actually alive instead of letting them talk to
  a stale marker.
- **Append-only makes audits free.** `task audit`, budget accounting, and
  failure forensics all read the journal rather than reconstructing it.

The per-task directory (`~/.maestro/tasks/<id>/`) holds bulky artifacts —
per-attempt result files, the verification report, adapter logs — kept out of
the journal so state stays cheap to scan.

## Workspaces and branches

One workspace runs one active task; additional handoffs for that workspace
queue FIFO and start when the slot frees. This is a concurrency guarantee, not
a throughput limit: two different workspaces run concurrently, and each task
gets its own branch (`maestro/<task-id>`) so parallel work never interleaves in
one working tree.

The branch is created *before* the agent starts, which means the review story
is simple: whatever the agent did — or failed to do — is inspectable on that
branch, and nothing is ever committed by Maestro itself. Commit policy
(`branch`/`pr` vs `no-commit`) only controls whether that isolation exists;
with `no-commit`, the agent works directly in your tree, for sandboxes and
non-git directories where a branch would be ceremony without value.

## Verification: evidence, not vibes

The design stance is that an agent reporting success is a *claim*, and claims
get checked. After a successful run, the daemon executes a deterministic check
in the task's workspace — auto-detected (Makefile → `make check`, Node test
script per lockfile, Go, Cargo, pytest) or explicitly configured via the
handoff's `verification` mode — plus `git diff --check` unconditionally. Both
must pass for the task to reach `completed`; the full command output is saved
to `verification.txt`.

This is deliberately narrow: Maestro never installs dependencies or changes
tooling, and a missing test runner degrades to the whitespace check with an
explicit note rather than inventing a pass. The alternative — trusting agent
self-reports — would make "completed" mean "the agent stopped", which in
multi-agent chains is exactly where errors compound: each downstream consumer
inherits the previous agent's unverified claim. Verification is what makes a
`completed` task safe to build on.

## Retries, fallbacks, and money

Each agent in the target→fallback chain gets `1 + MAESTRO_MAX_RETRIES`
attempts (default 3 total) with linear backoff before the chain moves on. Every
attempt — including failures — is recorded with its exit code, duration, and
usage. Two properties follow:

- **Forensics.** A failed task shows exactly which agents were tried, in what
  order, and why each stopped. "It just failed" is not a state; it is an audit
  trail that ends in `failed`.
- **Honest costs.** Usage is attributed per attempt and per agent, and failed
  work counts toward spend. That is what makes budget caps meaningful: the cap
  enforces against *what actually ran*, at launch time only (running tasks
  always finish), so a runaway chain stops starting new work instead of
  killing in-flight work mid-commit.

## Where this leads

The same daemon, handoff format, and state model are what let a delegation hop
machines: the `a2a_remote` adapter sends the full handoff document to another
Maestro daemon over the A2A protocol (token-authenticated when bound beyond
loopback), and the receiving daemon runs it exactly as if it were local. Peer
discovery is an informational layer on top — a roster of who is on the network,
with explicit registration still required for actual delegation. The design
question that shaped all of this was "what is the smallest broker that makes
any-agent-to-any-agent delegation durable and reviewable?" Everything above is
the answer to that question; the research behind the protocol choices lives in
[multi-agent-protocol-research.md](../multi-agent-protocol-research.md) and the
milestone history in [architecture-proposal.md](../architecture-proposal.md).
