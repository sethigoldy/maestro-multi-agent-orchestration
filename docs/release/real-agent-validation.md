# Real-agent validation (manual)

> **Status: MANUAL procedure — not part of CI.** Everything in this document
> requires authenticated, paid agent CLIs and spends real money. It is run by a
> human with the credentials; it is deliberately excluded from the automated gate
> (no real CLIs, keys, or network in tests or CI).

Purpose: prove that Maestro drives *real* coding agents end to end — discovery,
delegation, verification, receipts, and durability — on at least two different
agent backends. The deterministic smoke test (`scripts/smoke-fake-agent.sh`)
already covers the orchestration machinery with fakes; this document covers the
adapter boundary it cannot.

## Prerequisites

- A machine with Maestro installed (`pip install -e .` from a checkout, or the
  release wheel) and git.
- At least two authenticated agent CLIs on PATH. Preferred pair: **Codex**
  (`codex`) and **Claude Code** (`claude`). Any two of Codex / Claude Code /
  Cursor / GitHub Copilot CLI work; generic commands count as a third option.
- A scratch git repository to use as the workspace (never your real project on
  the first run).

## Procedure

### 1. Environment check

```sh
maestro --version
maestro doctor
```

Expected: `✓ environment is usable`, and every agent you plan to test shows
`✓ … — available` under **Agents** with a version string. Any `✗` here means the
CLI is missing, unauthenticated, or broken — fix that first; it is not a Maestro
issue.

### 2. Discovery and status

```sh
maestro agents list
```

Expected: the built-in adapters for your installed CLIs plus any registered
agents, with kinds and commands. No errors.

### 3. Start the daemon

```sh
python -m maestro.daemon_main --port 0 &     # or: maestro daemon start
maestro doctor                               # Daemon section: ✓ reachable
```

### 4. Delegate a small real task (agent A)

Use a task that is genuinely small and verifiable — e.g. "add a `health` module
that returns a dict, plus one unit test for it" in the scratch repo. Keep
`commit_policy` at its default so you can review the branch.

```sh
maestro delegate \
  --title 'Add health endpoint' \
  --request 'Create app/health.py with a health() function returning {"ok": True}, and one pytest test for it.' \
  --target codex \
  --workspace /path/to/scratch-repo
```

The CLI blocks until completion. Expected: the agent runs, makes real edits, and
the task reaches `completed` (or a clean, explained failure).

### 5. Inspect the result

```sh
maestro task list --workspace /path/to/scratch-repo
git -C /path/to/scratch-repo log --oneline -3        # task branch exists
git -C /path/to/scratch-repo diff main...HEAD        # real changes present
```

Run the project's own checks on the branch and confirm the work is what was
requested. This step is the human review that no automation replaces.

### 6. Receipt audit

```sh
maestro task receipt <task-id>
maestro task receipt <task-id> --json
```

Expected: state `COMPLETED`; at least one IMPLEMENT attempt with a real duration
and (for usage-reporting CLIs) a cost; Verification section showing the detected
command (e.g. `… -m pytest`) and its result; JSON parseable by
`python -m json.tool`. The human and JSON views must agree on every number.

### 7. Durability check

Stop the daemon, then re-read the receipt:

```sh
kill %1                                            # or: maestro daemon stop
maestro task receipt <task-id>                     # still renders, identical numbers
```

Expected: identical output with no daemon running. If anything differs, that is
a state-consistency bug — report it with both outputs.

### 8. Repeat with agent B

Repeat steps 4–7 with the second CLI (`--target claude`, or whatever you have).
The point of two backends: catch adapter-specific regressions (flag drift, output
format changes, usage-reporting differences) that a single-agent run cannot see.

### 9. Optional: work mode with real agents

If both CLIs are available, configure a preset in `$MAESTRO_HOME/config.toml`:

```toml
[modes.two-stage]
implementer = "codex"
verifier    = "claude"
max_bounces = 1
```

Delegate with `--mode two-stage` (no `--target`) and confirm the receipt shows
both agents, a gate verdict line under **Gates**, and the deterministic
verification result. This exercises the full work-mode cycle against real LLM
output — expect it to take minutes and cost real money.

## Fallback: validating with two generic agents

Without any built-in CLI, register two small scripts as generic agents (the demo
script `scripts/demo-v0.10.sh` contains a complete working example) and repeat
steps 4–7 with `--target <generic-name>`. This validates the generic adapter,
usage capture, and receipts — but not a built-in CLI's flags or output format, so
it does not replace step 8.

## Recording the outcome

When you run this for a release, record in the release notes: date, Maestro
version, agent CLIs + versions (from `maestro doctor`), which steps passed, and
any deviations. Do not paste full task content or credentials into the notes.
