# The 90-second Maestro v0.10 demo

One script, no network, no real agent CLIs: `scripts/demo-v0.10.sh` runs the whole
product story with deterministic fake agents and prints everything it shows.
Every number below is produced by the run itself — nothing is scripted output.

A 90-second terminal recording of this exact demo (real CLI invocations, paced
to this storyboard) is checked in at [assets/demo-v0.10.gif](assets/demo-v0.10.gif)
and embedded in the README.

```sh
python3.11 -m venv .venv && .venv/bin/python -m pip install -e .   # once
scripts/demo-v0.10.sh
```

The script needs `git` and this checkout's `.venv` (or set `PYTHON=/path/to/python`).
It works from any directory; all state lives in a throwaway temp dir and is removed
on exit.

## Storyboard

### 0:00–0:10 — "Is this environment usable?" (`maestro doctor`)

The demo starts the local daemon (loopback, ephemeral port) and runs
`maestro doctor --workspace <ws>` against it. You see the environment audit in one
screen: Maestro version, Python, state dir writability, daemon reachability, git,
every discovered agent (built-in CLIs **and** the two fake agents registered for
this demo), and the workspace's auto-detected verification command
(`git diff --check` — a plain repo with no test runner). Ends with
`✓ environment is usable`.

### 0:10–0:25 — "Delegate a work-mode task"

```sh
maestro delegate --title 'Add a health-check endpoint' \
    --request 'Implement the change described in the handoff.' \
    --mode demo --workspace <ws>
```

`--mode demo` expands the `[modes.demo]` preset written to `$MAESTRO_HOME/config.toml`:
implementer `demo-impl`, verifier `demo-verify`, fixer `demo-impl`,
`max_bounces = 1`. No `--target` needed — the preset pins the implementer. The CLI
blocks until the task finishes, then prints the task id and state (`completed`).

### 0:25–0:50 — "Watch it work" (the execution)

The fakes are small shell scripts with a deterministic script:

- **demo-impl** streams one line and reports `{"cost_usd": 0.15}` usage.
- **demo-verify** is stateful via a counter file: its **first** pass ends with
  `VERDICT: FAIL` plus one concrete issue (`flaky assertion in tests/api/...`);
  later passes end with `VERDICT: PASS`.

So the task runs the full work-mode cycle: IMPLEMENT → VERIFY (gate verdict FAIL)
→ one auto-fix bounce (FIX) → deterministic re-verification → COMPLETED. The gate
verdicts are parsed from the verifier's raw output — that is the whole gate protocol.

### 0:50–1:10 — "The execution receipt" (`maestro task receipt <id>`)

```text
Maestro Execution Receipt
----------------------------------------

Task #1
Add a health-check endpoint

Status      COMPLETED
Workspace   /private/.../ws
Branch      maestro/task-...
Duration    0s
Cost        $0.30
Runs        5 (3 agent · 2 verification)

Attempts
1  IMPLEMENT demo-impl      0s       $0.15   ✓
2  VERIFY    demo-verify    0s       —       ✓
3  FIX       demo-impl      0s       $0.15   ✓

Verification
✓ PASSED (git diff --check), 2 runs

Gates
  verify: FAIL (demo-verify), 1 issue(s)
  bounces: 1

Final result
COMPLETED
```

Reading it in five seconds: the task **completed**; three agent turns happened
(impl, verify-gate, fix); the verifier's first verdict was FAIL with one issue,
which drove exactly one bounce; the deterministic check passed twice (initial +
re-run after the fix); total reported cost $0.30 = 2 × $0.15 usage lines. The ✓ on
the VERIFY attempt means the gate *run* succeeded — its *verdict* is in the Gates
section. That separation (run health vs. verdict) is deliberate.

### 1:10–1:25 — "It's durable" (daemon stop, receipt survives)

The script kills the daemon and re-reads the receipt from disk — no server
involved:

```text
durable receipt after daemon stop:
  state=completed  runs=5  cost=$0.30
  verification=PASSED  bounces=1
```

Same numbers, no daemon. The task's state lives in `$MAESTRO_HOME`, not in the
process that ran it.

### 1:25–1:30 — "Durable execution for coding agents."

That is the product in ninety seconds: delegate once; get a verifiable, auditable,
restart-proof record of what happened and what it cost.

## Replaying pieces by hand

Everything the script does is plain CLI:

```sh
maestro agents add --name demo-impl --kind generic \
    --command /path/to/demo-impl.sh\ {prompt} --input-mode arg --output-format jsonl
maestro agents add --name demo-verify --kind generic \
    --command /path/to/demo-verify.sh\ {prompt} --input-mode arg --output-format text
maestro doctor
python -m maestro.daemon_main --port 0 &        # or: maestro daemon start
maestro delegate --title '...' --request '...' --mode demo --workspace <ws>
maestro task receipt <id>                       # human
maestro task receipt <id> --json                # machine
```

## Recording screenshots

The console (`python -m maestro.daemon_main` → http://127.0.0.1:<port>) shows the
same live task and its receipt panel while the demo runs. Capture instructions for
the launch images are in [assets/README.md](assets/README.md).
