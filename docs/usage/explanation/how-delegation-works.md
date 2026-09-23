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
   │     input-required (question answered → submitted → working)
   │            │
   ├──▶ canceled (user cancel, running or queued)
   └──▶ failed   (every agent in the target→fallback chain failed)
```

- **submitted** — accepted and registered; about to start (or paused for
  approval when `sensitive = true`, which goes straight to `input-required`).
- **working** — an agent is running on the task branch. Retries and fallback
  hops stay inside this state; each attempt is recorded separately.
- **input-required** — the agent asked a question (or a sensitive task awaits
  approval). The task waits here indefinitely until answered. An answer starts
  a new turn: the task goes back to `submitted` and then to `working`.
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
  journaled), and `daemon.json` tells clients which broker is actually alive.
  Clients confirm that the marker's process is the daemon that wrote it (it
  holds the owner lock) and that it answers, instead of talking to a stale
  marker. A daemon that starts alone fails every leftover "working" task,
  since nothing else can still be running it; when other daemon processes
  share the state, each task's runtime snapshot names the daemon process that
  runs it (pid and start time), and the task is failed only when that process
  is certainly gone. The MCP server never runs tasks beside a daemon that owns
  the state: it forwards its tool calls to that daemon, and when none exists it
  starts a background daemon and forwards to that.
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

## Agent processes

Each agent runs in its own process group. When a run ends, for any reason
(success, failure, timeout or cancel), Maestro stops every process still left
in that group. A task is a batch run, so nothing it starts in the background,
such as a dev server or a watcher, outlives it.

Stopping a group always starts with SIGTERM, so the processes in it can clean
up, for example by removing a `.git/index.lock` file. Whatever is still
running two seconds after the SIGTERM gets SIGKILL. After a successful run
this adds at most two seconds, and nothing at all when the agent left no
process behind. After a timeout or cancel, the agent's own process gets up to
five seconds to exit before it gets SIGKILL, and up to five more seconds to be
reaped, so stopping a group takes at most about ten seconds. Maestro checks that the group still exists
before each signal, and never signals a group it has already seen gone,
because its id could by then belong to an unrelated process.

If the agent exits while a background child still holds its output open,
Maestro keeps reading for two seconds after it notices the exit (it checks at
least every half second) and then treats the run as finished, even if the
child is still printing. Output that arrives during those two seconds is
recorded like any other output. After the leftover processes are stopped,
Maestro also reads everything still waiting, including a last line that has no
trailing newline. That line is often the agent's question or error, so it
reaches the log, question detection and the error message. One case is not
covered: a process that moved itself out of the group (for example with
`setsid`) and still holds the output open. Maestro does not stop such a
process, and it waits at most five seconds for the output to close; a last
line without a newline is then lost.

A cancel reaches a running agent within about half a second, even while the
agent prints nothing. A spawned agent is then stopped straight away, as
described above.

An RPC agent first gets its abort command. If the agent is still taking its
start command, Maestro waits up to one second for that write to finish, and
sends no abort if it does not, because the two would get mixed up. From that
point the agent has five seconds to stop and close its output. The abort is
written from a separate thread, so an agent that has stopped reading its input
cannot block the run. When the five seconds end and the agent is still running
(whether it ignored the abort, never read it, or got none), Maestro stops its
process group at once, without waiting any longer for it to exit on its own.
That stop is the one described above: SIGTERM, up to five seconds for the
agent's own process and two for the rest of the group, then SIGKILL. So an RPC
agent that ignores the abort but exits on SIGTERM is stopped about five
seconds after the cancel. The worst case, an agent that also ignores SIGTERM,
is about fifteen seconds, plus up to one second when the start command was
still being written. If the agent exits within the five seconds, only the
processes it left behind are stopped.

Agent output that is not valid UTF-8 is read with the bad bytes replaced, so
it never stops a run.

## Verification: evidence, not vibes

The design stance is that an agent reporting success is a *claim*, and claims
get checked. After a successful run, the daemon executes a deterministic check
in the task's workspace — auto-detected (a makefile `check` rule → `make check`,
Node test script per lockfile, Go, Cargo, pytest) or explicitly configured via the
handoff's `verification` mode — plus `git diff --check` unconditionally. Both
must pass for the task to reach `completed`; the full command output is saved
to `verification.txt`.

This is deliberately narrow: Maestro never installs dependencies or changes
tooling. Choosing the command changes nothing either: Maestro reads the
makefile to find an explicit `check` rule and never runs make to ask, because
even a `make -n` dry run can create files. When a project has no test runner
at all, the check degrades to the whitespace check with an explicit note, and
that check alone passes only if the agent left changes in the workspace. When
a project has tests but the test runner is not installed (for example, pytest
is missing from the selected Python interpreter), verification fails and the
report says how to fix it. When a project had a Python test suite at the start
of the turn, verification fails if pytest then collects no tests, and it also
fails if pytest is missing even though the tests are gone now, so an agent
cannot pass by deleting or hiding the tests. Maestro does not invent a
pass in any of these cases. The alternative — trusting agent
self-reports — would make "completed" mean "the agent stopped", which in
multi-agent chains is exactly where errors compound: each downstream consumer
inherits the previous agent's unverified claim. Verification is what makes a
`completed` task safe to build on.

## Work modes: gates and bounces

A **work mode** is a named preset that pins agents to the phases of a task
cycle — cheap model implements, expensive model reviews, and so on. The design
question it answers: how do you make cost/quality profiles *data* instead of
prompt discipline? Today, "have the cheap model implement and the expensive
model review" is something a supervisor has to orchestrate by hand across
several delegations; a preset expresses it once in config, and every task that
names it gets the same shape automatically.

The mechanics extend the lifecycle rather than replacing it:

- **Gates run after the implementer, in order.** Deterministic verification
  first (when `verification != "none"`), then an optional LLM verifier turn,
  then an optional reviewer turn. Each gate is one agent turn with a strict
  output contract — a trailing `VERDICT: PASS` or `VERDICT: FAIL` line plus
  optional `ISSUES:` bullets — so the daemon can parse the outcome without
  trusting prose. A gate that cannot produce a parsable verdict parks the task
  immediately rather than guessing; an unparseable review is treated as "no
  signal", not "pass".
- **LLM gates add failures, they never remove them.** The invariant that keeps
  this honest: a failed deterministic check can be *confirmed* by an LLM but
  never overridden. A reviewer that says PASS while the tests fail still parks
  the task. This is why the deterministic gate always runs first — it is the
  floor, and everything above it can only raise the bar.
- **Bounces are bounded auto-fix.** When a gate (or the deterministic check)
  fails, the work goes to the fixer — by default the implementer — for one
  attempt, then re-verification and re-review run again. The loop is capped by
  `max_bounces` (default 2; `0` disables it), so a mode can never spend
  unboundedly on iteration. Each bounce is a recorded attempt attributed to its
  agent, which is what makes per-phase cost visible in audits and budgets.
- **Exhaustion parks, it does not fail.** When the cap is hit with issues
  remaining, the task moves to `input-required` with every unresolved issue
  listed — and keeps its workspace slot while parked, exactly like a mid-task
  question. The distinction matters: `failed` means "no agent could do this",
  while a parked gate means "the work needs a judgment call" (fix these
  specific issues, or approve the risk). Answering resumes under the
  implementer; follow-ups after a terminal state run under the mode's fixer,
  since that agent is already pinned to fixing.

Two refusals keep the gates meaningful: `review_agent == target_agent` is
rejected at delegation time (self-review manufactures the appearance of an
independent signal — the same epistemic rule as no-self-delegation), and a
preset's agents are checked against the live registry when the task starts, so
a stale preset fails fast with the registered list rather than mid-cycle.

## Context injection

The design question context injection answers: how does the user inject their
own context into agent turns when the supervising agent may not think to add it?
Handoffs have always carried `design`, `context_notes`, and `context_files` —
but those were advisory, unattributed, and invisible to gate turns. Context
entries make the channel first-class: typed, labeled, layered, and recorded.

The layering follows the same specificity order as everything else in Maestro
(config → handoff):

- **Standing context is config.** `[context.<label>]` tables in the user or
  project config reach every task in that scope — repo conventions, shared
  skills — written once instead of repeated per delegation. This is the
  "the supervisor may not add it" channel: it does not depend on any agent's
  judgment about what to include.
- **Per-task context is handoff.** `[[context]]` entries override standing
  entries by label and are composed at delegate time; the merged list is stored
  on the task record, so what each turn received is inspectable (`task audit`)
  rather than hidden in prompt assembly.
- **Phases are scope.** Entries can target `implementer`, `verifier`, or
  `reviewer` turns — the same phase vocabulary as work modes — so a reviewer's
  checklist never bloats the implementer's prompt, and vice versa.

Two invariants keep it safe:

- **Context is data, not code.** Maestro stages skill directories and copies
  oversized files into the task dir; it never executes context content. The
  trust level equals config: entries point at paths the user manages, and a
  skill is treated like installing software.
- **Bounded by degradation.** Inlined files are capped (8KB) and the rendered
  block is capped (32KB); overflow degrades to artifact references or a visible
  dropped-labels note — context can bloat a prompt, but it cannot break a task.

The one adapter-specific detail: for Claude Code targets, standing entries ride
in the system prompt (`--append-system-prompt-file`) instead of the task
message — because they are config-level instructions that apply across every
task in scope, not part of this task's request; per-task handoff entries stay
in the user block with the request. Staged skills are passed via `--add-dir`
(the discovery mechanism of the [Agent Skills open standard](https://agentskills.io/)).
Maestro's other adapters pass everything as a labeled block in the prompt —
same data, different channel.

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
