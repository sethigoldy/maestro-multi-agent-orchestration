# Tutorial: your first delegation

In this tutorial we will install Maestro, start its daemon, and delegate a real
bug fix to the Codex agent — then watch the task run live, inspect the durable
record, and review the fix on the task's own git branch. By the end you have
completed one full delegation loop: **handoff → agent work → verification →
review**.

## Prerequisites

You need exactly three things:

1. **Python 3.11 or newer** (`python3 --version`).
2. **The Codex CLI** installed and authenticated with its normal setup flow —
   `codex` must be on your `PATH`.
3. **Git**, on the default version for your OS.

We will do everything in one repository you create now, so nothing else matters.

## Step 1 — Install Maestro

Create a scratch directory and install Maestro from source:

```bash
mkdir -p ~/maestro-tutorial && cd ~/maestro-tutorial
git clone https://github.com/sethigoldy/maestro-multi-agent-orchestration.git maestro
cd maestro
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

Now check the installation:

```bash
maestro --version
```

You will see:

```text
0.12.0
```

If you do not see a version number, your virtual environment is not active —
re-run `source .venv/bin/activate` and try again.

Then check that the environment can actually run Maestro:

```bash
maestro doctor
```

Doctor is read-only and fast: it reports the state directory, daemon
reachability, git, the agent CLIs it found (you need `codex`), and the budget
caps. It exits non-zero only on a genuinely blocking problem — if you get one,
fix that first; otherwise continue.

## Step 2 — Prepare the demo repository

We need a tiny project with a bug for the agent to fix. Create it:

```bash
cd ~/maestro-tutorial
mkdir demo && cd demo && git init -q
```

Create `app.py`:

```python
def double(x):
    return x
```

And `check_app.py`:

```python
import sys

from app import double

if double(21) == 42:
    print("OK")
else:
    print(f"double(21) returned {double(21)}")
    sys.exit(1)
```

Commit the starting point so the agent's changes are easy to see later:

```bash
git add app.py check_app.py
git commit -qm "starting point with a bug"
python check_app.py
```

You will notice that the last command fails:

```text
double(21) returned 21
```

That failing check is exactly what we are about to delegate.

## Step 3 — Start the daemon

Open a **second terminal** (the first one keeps its virtual environment for the
rest of the tutorial). In the second terminal:

```bash
cd ~/maestro-tutorial/maestro
source .venv/bin/activate
maestro-daemon
```

After a moment it prints a JSON block and stays running:

```text
{
  "pid": 48213,
  "port": 53217,
  "bind": "127.0.0.1",
  "advertised_host": "127.0.0.1",
  "state_dir": "/Users/you/.maestro"
}
```

Remember the `"port"` value — you will use it in Step 6. Leave this terminal
open; the daemon must keep running while tasks execute.

## Step 4 — Delegate the fix

Back in the first terminal, delegate the bug fix:

```bash
maestro delegate \
  --title "Fix double() in app.py" \
  --request "double() in app.py returns its input unchanged; it must return twice the input. Fix it, then run 'python check_app.py' and make sure it prints OK." \
  --target codex \
  --workspace ~/maestro-tutorial/demo
```

This command blocks while the task runs, streaming the agent's output live.
The output should look something like this (the agent's own lines vary):

```text
[task] task-20250718-143022-a1b2c3 — target=codex workspace=/Users/you/maestro-tutorial/demo
[state] working
… the agent's output, one line at a time …
[state] completed
```

Notice two things while you wait:

- The first line gives you the **task id** (`task-20250718-143022-a1b2c3` in
  this example — yours will differ). Write it down; every command in the next
  steps needs it.
- `[state] completed` means the agent finished *and* Maestro's deterministic
  verification ran. If you had seen `[state] failed` instead, the same task id
  would still work for everything below — just look at Step 5 before reviewing.

If instead you see `[state] input-required (question: …)`, the agent is asking
you something; answer it from a host agent via the MCP tools (see
[Manage in-flight tasks](../how-to/manage-in-flight-tasks.md)) and the task
continues on its own.

## Step 5 — Read the durable record

The daemon terminal still shows the live stream, but the permanent record lives
in Maestro's state. In the first terminal:

```bash
maestro task status task-20250718-143022-a1b2c3
```

Replace the id with your own. You will see a JSON object; the fields that
matter right now:

```json
{
  "task_id": "task-20250718-143022-a1b2c3",
  "phase": "REVIEWING",
  "target_agent": "codex",
  "branch": "maestro/task-20250718-143022-a1b2c3",
  "verification": "PASSED: /Users/you/.maestro/tasks/task-20250718-143022-a1b2c3/verification.txt",
  "workspace": "/Users/you/maestro-tutorial/demo"
}
```

Notice that `"phase"` is `REVIEWING` — in Maestro's vocabulary, *the work is
done and awaiting your review*. That is a deliberate state: the daemon does not
declare success on the agent's word alone.

Now look at the evidence behind that claim:

```bash
maestro task audit task-20250718-143022-a1b2c3
cat ~/.maestro/tasks/task-20250718-143022-a1b2c3/verification.txt
```

The audit shows every attempt (agent, exit code, duration, usage) and the final
state; the verification report shows exactly which command Maestro ran and what
it printed. In our demo no project test runner was detected, so you will see a
note like `no project test runner detected; using git diff --check only` — that
is expected here and is why the *request itself* told the agent to run
`check_app.py`.

For the one-command summary of the whole task — attempts with durations and
costs, the verification result, and the final state — use the execution
receipt:

```bash
maestro task receipt task-20250718-143022-a1b2c3          # human-readable
maestro task receipt task-20250718-143022-a1b2c3 --json   # stable JSON for tooling
```

The receipt is a projection of the durable state: it works while a task runs,
after it ends, and even after the daemon restarts. The web console shows the
same receipt inline on each task's detail pane.

## Step 6 — Review the fix on its branch

Every task runs on its own git branch, created in the workspace before the
agent starts. Inspect what the agent did:

```bash
cd ~/maestro-tutorial/demo
git status
git diff
```

You will notice the repository is now on branch
`maestro/task-20250718-143022-a1b2c3` and that `app.py` contains the fix:

```python
def double(x):
    return 2 * x
```

Run the check yourself — this is your review, not Maestro's:

```bash
python check_app.py
# OK
```

Maestro never commits. The fix sits as an uncommitted change on the task
branch; you decide what happens next (commit it, open a pull request, or send
the agent back with corrections).

While the daemon runs, you can also watch everything in a browser: open
`http://127.0.0.1:<port>/` using the port from Step 3. The web console shows
the task, its live output, and per-attempt detail.

## Step 7 — Stop the daemon

When you are done, go to the daemon terminal and press `Ctrl-C`. It stops
cleanly; everything it recorded (tasks, attempts, verification reports) remains
in `~/.maestro` for next time.

## What you have built

You have completed a full Maestro delegation loop: installed the broker,
started the daemon, sent a handoff to an agent, watched it stream live, read
the durable audit and verification evidence, and reviewed the result on the
task's own branch. Every later feature — fallback chains, budgets, follow-ups,
remote daemons — hangs off this same loop.

**Where to go next:**

- [Delegate a task](../how-to/delegate-a-task.md) — handoff files, fallbacks,
  per-task model/effort settings.
- [Manage in-flight tasks](../how-to/manage-in-flight-tasks.md) — questions,
  follow-ups, cancellation.
- [How delegation works](../explanation/how-delegation-works.md) — the states,
  the durable state, and why Maestro is built this way.
