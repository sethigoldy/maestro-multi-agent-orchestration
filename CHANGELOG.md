# Changelog

All notable changes to Maestro are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semantic versioning.

## [Unreleased]

### Security

- **A web page can no longer drive the local daemon.** A daemon on 127.0.0.1 needs no token, and until now it accepted any request that reached it. A page open in your browser could send a plain-text POST to it and start an agent in any folder, and a DNS-rebinding page could read the task list and live agent output. A loopback daemon now refuses requests whose `Host` header is not `127.0.0.1`, `localhost` or `::1`. Every daemon now requires `Content-Type: application/json` on POST, which a browser cannot send cross-site without a preflight that the daemon never approves, and refuses a POST whose `Origin` is not exactly the daemon's own address. Maestro's own CLI, MCP server, remote-agent adapter and web console already send requests that pass these checks.
- **Tokens are kept private.** The `daemon.json` marker and agent registry entries are now written with mode 0600, and an existing world-readable marker is tightened on the next start. `maestro agents list`, `maestro agents add` and the MCP `agents_list` tool show a stored token as `<redacted>`; before, `agents_list` put remote-agent tokens into the supervising model's transcript. Bearer tokens are compared in constant time. The foreground `maestro-daemon` still prints its token at startup, because that is how you give it to another machine.
- **A token file is replaced, never rewritten in place.** Unix checks file permissions only when a file is opened, so a local user who had opened an older world-readable `daemon.json` or registry entry could read a new token through that open handle after the rewrite. Maestro now writes the new content to a fresh file created with mode 0600 in the same directory and moves it over the old one, and removes that temporary file if anything fails. Agent registry entries left world-readable by older versions are set to 0600 whenever the registry is loaded; an entry the current user cannot change is skipped.
- **A reverse proxy can be allowed to POST to the daemon.** The `Origin` check is an exact-origin rule: the origin's host and port must equal the request's `Host` header. A proxy that changes `Host` to the daemon's own address broke browser POSTs through it. List the proxy's public origin with `maestro-daemon --allow-origin https://maestro.example.com` (repeatable) or in `MAESTRO_DAEMON_ALLOWED_ORIGINS` (comma-separated), which also works for `maestro daemon start` and the MCP server's daemon. An invalid entry stops the daemon from starting. Requests without an `Origin` header are not affected.
### Added

- **Choose the task branch name when you delegate.** Until now every task branch was called `maestro/<task-id>`, and the only way to follow a repository's naming convention was to rename the branch by hand afterwards. A handoff can now name the branch: `[expectations] branch = "feat/login-form"` in the file, `maestro delegate --branch feat/login-form` on the CLI, or `delegate(..., branch="feat/login-form")` over MCP (the flag and the MCP argument override the file). The name is checked against git's branch-name rules when the handoff is read, and delegation is refused if the branch already exists, so an agent never starts work on a branch that holds something else. Delegation is also refused when the name clashes with an existing branch as a folder: git keeps `feat/login` as a file inside a folder called `feat`, so with a branch `feat` present, `feat/login` cannot be created, and with `feat/x` present, `feat` cannot be created. Before this check, such a name passed delegation and the turn then failed in `git checkout -b`. Setting a branch together with `commit_policy = "no-commit"` is refused, because that policy creates no branch. Every later turn of the task, including follow-ups, stays on the named branch. If git cannot create the branch when the turn starts (for example, two queued handoffs asked for the same name, or someone created it after delegation), the task fails with that reason instead of letting the agent work on whatever branch was checked out. The failure message names the commands that fix it: `maestro task rename-branch <task> <new-name>` changes the name the next turn will create, and `maestro task continue <task> --request "…" --branch <new-name>` does that and starts the turn in one step. The name applies only to the workspace of the daemon that runs the task. When the task runs on a remote daemon (an `a2a_remote` agent), the handoff Maestro forwards to it has `branch` removed, because the remote gets a new request on every attempt and every turn and would otherwise refuse each one after the first as "already exists". The remote daemon puts its work on its own default branch, `maestro/<remote-task-id>`.

- **Rename a task's branch after it has run.** `maestro task rename-branch <task> <new-name>`, the MCP tool `rename_task_branch`, and the A2A method `tasks/renameBranch` rename the git branch and update the task's durable record in one step. Before this, a branch renamed with `git branch -m` left `maestro task list`, `task status` and receipts showing the old, deleted name, and a follow-up would recreate the old branch from the current checkout. If the branch was already renamed by hand, the command only updates the record, after checking git's reflog to confirm the new branch was renamed from the task's branch; an unrelated branch that happens to exist is refused. If the task has no branch yet, for example because its first turn could not create the branch it asked for, the command changes the name that the next turn will create, and its result has `"pending": true`. The rename is refused while an agent is working on the task or a turn is starting, when the new name already exists or clashes with an existing branch as a folder, and when neither name exists. The check and the rename run under the daemon's lock, and follow-ups and answers start their turn under the same lock, so a follow-up sent at the same moment waits for the rename to finish instead of checking out the old name. It works after a daemon restart, and the CLI writes the state directory directly when no daemon is running. The daemon publishes a `branch` event so the terminal dashboard shows the new name straight away. Only the local branch is renamed; a copy already pushed to a remote keeps its old name.

- **Rename the branch as part of a follow-up.** `maestro task continue <task> --request "…" --branch <new-name>`, the MCP `followup` tool's `branch` argument, and the `branch` parameter of the A2A method `tasks/followup` rename the task's branch first, with the same rules as `rename-branch`, and then run the turn on it. For a task that has no branch yet, they set the name the turn creates.

### Fixed

- **A task turn no longer runs on the wrong branch when checking out its branch fails.** Before, if `git checkout` of the task's existing branch failed (for example, an uncommitted edit on another branch would be overwritten), the turn carried on in whatever branch was checked out while the record still named the task branch. The agent could then work on `main`. The turn now fails with git's reason before any agent runs.

- **A later turn no longer creates a new, empty branch when the task's branch is missing.** Before, if the task's branch had been renamed or deleted, the next follow-up ran `git checkout -b` with the old name, which quietly created a fresh branch from whatever was checked out, without the task's earlier commits. Now, when the recorded branch is missing, the turn looks in git's reflog for a rename. If exactly one branch was renamed from it, the turn uses that branch, updates the task's record and publishes a `branch` event. If git has no record of a rename (the branch was deleted), or records more than one branch that came from it (a renamed branch was later copied with `git branch -c`), the turn fails before any agent runs, and the message says how to fix it. The first turn of a task still creates its branch as before.

### Fixed

- **A second daemon no longer takes over a state directory that a live daemon owns.** Before, the MCP server's built-in daemon or a foreground `maestro-daemon` started on the same state directory as a running daemon, overwrote its `daemon.json` marker, and marked that daemon's running tasks as failed. The daemon that serves HTTP now holds a lock file (`daemon.owner.lock`) for its whole life. A second `maestro-daemon` refuses to start, names the running daemon, and exits with status 1. The MCP server's built-in daemon does not refuse, because the MCP tools must keep working when `maestro daemon start` has already started a background daemon: it runs its own tasks without an HTTP endpoint, prints a note on stderr, and leaves the other daemon's marker alone. A daemon now marks leftover "working" tasks as failed at startup only when no other daemon process is using the state directory.
- **`maestro daemon stop` no longer signals an unrelated process.** After a crash, the marker's pid can be reused by another program. `stop` used to send it SIGTERM and then SIGKILL, and `start` refused to start because the pid looked alive. `status`, `stop` and `start` now confirm that the pid is the daemon that wrote the marker: it must hold the owner lock, or, for a marker written by an older version, its HTTP endpoint must answer with a Maestro agent card. When that cannot be confirmed, the marker is treated as stale: `stop` removes it without signalling anything and says so, and `start` starts a new daemon.
- **P2P discovery accepts less from the network.** The presence socket was bound to every address, so anyone who could reach UDP port 9786 could add a peer with any name and any URL. The receive socket is now bound to the multicast group and the send socket to `MAESTRO_DISCOVERY_IF`, and a loopback interface ignores announcements from other hosts. An advertised host must be an IP address (a value such as `169.254.169.254/latest/meta-data#` is dropped), the port must be valid, and names lose control characters, so `maestro peers list` cannot print terminal escape codes. `peers.json` holds at most 256 peers, prunes discovered peers unheard for an hour, and is rewritten only when something changed instead of on every packet. A daemon that listens on loopback only no longer runs discovery unless `MAESTRO_DISCOVERY=1` is set.
- **The HTTP API refuses requests that could tie it up.** A negative `Content-Length` made the handler wait until the client hung up, and any declared size was read into memory. A negative or non-numeric `Content-Length` now gets 400 and a body over 8 MiB gets 413, before anything is read. Each event-stream (SSE) client used to have a queue with no limit, which grew for as long as the client stopped reading; the queue now holds at most 2048 events, and a client that falls further behind, or that accepts no data for 30 seconds, is disconnected and can reconnect to catch up. `EventBus.subscribe` takes an optional `maxsize` for this; callers that do not pass it keep an unbounded queue.
- **Malformed A2A requests get an error reply instead of a dropped connection.** A JSON-RPC request whose `params` was not an object raised an exception inside the handler, and the client saw the connection close with no reply. Such a request now gets error `-32602`, a message whose `parts` is not a list is treated as having no parts, and any other unexpected exception in the dispatcher becomes error `-32603` (HTTP 500), with the traceback written to the daemon's stderr.

## [0.12.0] — 2026-09-22

### Fixed

- **Tasks no longer get stuck reporting `working`/IMPLEMENTING after completion or interruption** — three independent holes let a finished (or dead) task keep its old state forever:
  1. *Crashed turn threads* — a turn runs in a bare daemon thread, so any uncaught exception between the start of work and the terminal transition (adapter spawn failure, disk error writing the result file, git/subprocess failure inside verification or the knowledge refresh) killed the thread silently: the task stayed `working` with no thread left to drive it, its workspace slot leaked (later handoffs queued behind a ghost), and CLI output ended on `[state] working`. Turns are now wrapped in a crash guard that ends the task `failed` with a "turn crashed" error and frees the slot; parked (input-required) and already-terminal tasks are never overridden. The terminal knowledge refresh is also exception-isolated so a projection failure can no longer swallow the terminal state event.
  2. *Daemon restarts mid-turn* — in-memory task state was rebuilt from nothing at startup, so a task whose process died while working (SIGKILL/OOM/reboot) reported `working` forever. The daemon now reconciles durable state at startup: tasks left `working` are marked `failed` ("daemon stopped or crashed while the task was running — re-delegate, or continue this task to resume"), and queued-but-never-started tasks (the queue is in-memory) are failed too. Parked input-required tasks are left alone.
  3. *Misleading durable fallbacks* — a task with no live record and no usable status claim defaulted to `working` (and `wait()` blocked on it for the full timeout). Unknown/missing state now resolves to `failed`. The phase claim is also lossy (input-required and completed both map to REVIEWING), so all durable views now prefer the runtime snapshot's own state — written on every transition — over the phase mapping.
  `stop()` additionally marks in-flight turns `failed` before shutting down, so a graceful stop never leaves durable state claiming `working`.

- **Flaky 100% coverage gate on the a2a_remote timeout path (again, for good)** — the SSE wait loop had two equivalent timeout exits (deadline already expired at the top of the loop vs. an empty queue read), and which one fired depended on event timing; branch coverage of the pre-check line therefore rode on whether a localhost HTTP round-trip took more or less than 1 ms, flaking the CI gate (this run: Python 3.11). The wait is now clamped to `max(remaining, 0)` — required anyway, since `queue.get(timeout=<negative>)` raises `ValueError` instead of `Empty` — and the redundant pre-check is gone, leaving a single timeout exit that every timeout test hits deterministically on any machine.

### Changed

- **Implementation prompts now state the run is non-interactive** — Maestro turns are batch runs with nobody available to answer mid-run, but nothing told the agent that; an agent that stopped with "please approve this design" ended its turn there and the task completed without the work (certified `PASSED` on pre-0.11 builds). Implementation prompts now carry an explicit EXECUTION MODE directive: do not stop to ask for approval or confirmation, make reasonable decisions within the request's scope, complete the work in this run, and list open questions in the final report so the supervisor can answer them on a follow-up turn (the existing Q&A channel). Together with the 0.11 zero-work guard, a stalled turn can no longer complete as `verification: PASSED`.

## [0.11.0] — 2026-09-22

### Added

- **Task continuation + context reuse** — a finished task (completed/failed/canceled) can now be continued with a new instruction on the *same* task id, workspace, branch, and routing: CLI `maestro task continue <task-id> --request "…" [--context reuse|fresh] [--no-wait]`, MCP `followup(…, context_mode=…)`, A2A `tasks/followup`. In the default **reuse** mode the daemon projects a compact, versioned **task-knowledge** snapshot (goal, current state, files changed, latest verification result and failures, known issues, bounded tail of the last turn's output) from durable state and injects it as one labeled context entry — raw history stays in the task record and is never replayed. The snapshot budget is configurable (`[continuation] max_tokens`, `MAESTRO_CONTINUATION_MAX_TOKENS`); sections are dropped or truncated deterministically until it fits. Knowledge is persisted as a durable `task_knowledge` claim when a turn reaches a terminal state, so continuation works **after daemon restarts** (the task record is reconstructed from claims). `--context fresh` skips the snapshot for a clean reasoning context; `[continuation] enabled = false` makes all follow-ups fresh. Receipts now report turn count, knowledge schema metadata, and honest per-turn context stats (`mode`, `knowledge_chars`, `context_chars`, `raw_history_bytes`, labeled token estimate, reduction ratio); `maestro config` prints the effective `[continuation]`. Delegation depth still bounds continuation chains (each follow-up decrements `max_depth_remaining`).

### Fixed

- **Zero-work turns no longer certify as `verification: PASSED`** — when no project test runner is detected, the deterministic verifier falls back to `git diff --check`, which passes trivially on an untouched workspace. A turn that left no changes behind (e.g. an agent that stopped to ask for approval a non-interactive batch run can never receive) therefore completed as `PASSED` with nothing built. The fallback verifier now requires evidence of work — working-tree changes or new commits since the turn's baseline HEAD (recorded per turn, durable across restarts) — and fails with an explicit "no changes detected" report that includes the tail of the agent's output. Explicit verification commands and real test runners keep their existing semantics; committed work is recognized as evidence.

- **Dashboard layout on terminals narrower than 100 columns** — `maestro dashboard` rendered every frame for a fixed 100-column width, so on narrower terminals each line wrapped and the whole screen cascaded into a scattered layout. The TUI now detects the real terminal width (`$COLUMNS`, then a TIOCGWINSZ query on the stdout fd, falling back to 80) and re-queries it on `SIGWINCH` with an immediate redraw; the frame renderer also clamps its width input and truncates detail-pane values (title, workspace, branch, error, transcript lines) so no line in the frame can wrap.

- **Agents now inherit login-shell environment variables** — spawned agents
  previously received only the daemon process's environment, so profile exports
  (API keys such as `GROVE_API_KEY`) were missing whenever the daemon was
  started from a GUI, launchd, or an older terminal. `worker_environment` now
  layers a one-per-process snapshot of `$SHELL -lc env` under the daemon's own
  environment (explicit daemon values still win on conflict). Disable with
  `MAESTRO_LOGIN_ENV=0`; tune the snapshot timeout with
  `MAESTRO_LOGIN_ENV_TIMEOUT_S`. Restart the daemon after adding new variables.

- **Flaky 100% coverage gate on the a2a_remote timeout branch** — the SSE wait
  loop has two equivalent timeout exits (deadline already expired at the top of
  the loop vs. an empty queue read), and which one fired depended on event
  timing, so CI occasionally failed the coverage gate. Added a deterministic
  test that exercises the top-of-loop branch (an early event followed by
  silence).

### Changed

- **Flagship demo GIF re-recorded with a normal monospace font** — the previous
  recording rendered with wide letter spacing; the new one uses Menlo with zero
  tracking. The demo's fake implementer now leaves a real working-tree change
  (required by the new evidence-of-work verification rule), and the recording is
  reproducible from `scripts/demo-gif.tape` + `scripts/demo-gif-setup.sh`.

## [0.10.0] — 2026-09-21

### Added

- **Routing defaults + interactive routing question** — when a handoff names no
  target agent, Maestro now resolves routing from the project's
  `.maestro/config.toml` `[defaults]` table (`agent`, `fallback`, `model`,
  `effort`; later config files win per key). If neither the handoff nor
  `[defaults]` names an agent, the task parks in `input-required` with a
  question listing every registered agent (name + version) and resumes via
  `answer_task_question` / `maestro answer` — answers may be a bare agent name,
  `agent=… model=…` pairs, or JSON. `maestro delegate` flag form now accepts
  `--title/--request` without `--target/--mode`, and `maestro config` prints the
  effective `[defaults]`.
- **OpenCode adapter** — first-class support for the OpenCode CLI: spawn-mode
  adapter running `opencode run --format json --auto` (model via `-m
  provider/model`, effort via `--variant`), usage/cost parsed from the terminal
  `step_finish` event, registered in discovery and the agent registry, plus a
  global-rules integration (`~/.config/opencode/AGENTS.md`).
- **Self-contained skill** — the packaged `maestro-driven-development` skill now
  carries the full CLI reference, config schema (`[defaults]`, `[modes]`,
  `[context]`, `[codex]`, `[verification]`, `[storage]`), the MCP tool contract,
  and the routing/answer flow, so a host agent never has to rediscover Maestro's
  interface at runtime.

### Changed

- **Skill updates are delete-then-reinstall** — `maestro skill install` (and
  `install.sh`) now remove the existing installed skill before writing the new
  one, so stale files from a previous Maestro version never survive an update.
- **Demo assets renamed** — `demo-v0.9.{sh,gif}` and `docs/demo-v0.9.md` are now
  `demo-v0.10.*`, matching this release.

- **One-command installation** — `install.sh` (usable via
  `curl -fsSL … | bash`) installs Maestro into `~/.local/share/maestro` with a
  private virtualenv, exposes `maestro` / `maestro-daemon` / `maestro-mcp` in
  `~/.local/bin`, discovers and registers every supported coding-agent CLI on
  the machine (preserving existing registrations), installs the global skill
  into each detected agent, and starts/verifies the background daemon — all
  without root. Rerunning updates in place; `--uninstall` (optionally
  `--purge-state`) removes it while keeping user state by default.
- **Background daemon lifecycle** — `maestro daemon start | stop | status
  [--json] | restart`. `start` detaches the existing daemon executable in its
  own session (survives shell exit), reuses the `~/.maestro/daemon.json`
  marker as the single source of truth, refuses to start a duplicate for the
  same state directory (advisory lock + liveness check), and reports PID/port/URL.
  `stop` is SIGTERM → grace period (`MAESTRO_DAEMON_STOP_GRACE_S`) → SIGKILL,
  idempotent, and cleans stale markers; `status` distinguishes "no marker",
  "marker but dead process", and "live and answering".
- **Global `maestro-driven-development` skill** — a packaged operational skill
  (`maestro/skills/maestro-driven-development/SKILL.md`) that makes Maestro the
  default development execution backend for supported agents: trigger
  classification, daemon health check and auto-start, handoff/delegation via
  MCP tools or CLI, review + follow-up loop, agent selection left to Maestro's
  registry, and a bounded fallback when Maestro is unavailable.
- **Agent integration layer** — `maestro/integrations.py` defines one
  `AgentIntegration` per supported agent (Claude Code global skill, Codex /
  Copilot CLI / Hermes / Pi / Cline managed instruction blocks, Cursor global
  rule, OpenHands `custom_instructions`) with idempotent install/uninstall and
  status, driven by the new `maestro skill list | status | install [--agent
  NAME | --all] | uninstall` commands.
- **`maestro agents register-discovered [--dry-run]`** — converts discovery
  results into registrations programmatically; existing registrations (custom
  names/models/tokens) are preserved untouched.
- **Recursion protection** — every agent process Maestro launches receives
  `MAESTRO_AGENT_CONTEXT=1`, `MAESTRO_TASK_ID=<task>`, and
  `MAESTRO_ROLE=implementation`; the global skill instructs such workers to
  implement directly and never delegate back to Maestro.

### Changed

- `maestro-daemon` remains the foreground executable (development/CI/service
  managers); the preferred user-facing command is now `maestro daemon start`.
- The "no daemon reachable" CLI error now points at `maestro daemon start`.

### Fixed

- **`claude_code` adapter** — emit `--verbose` with `--output-format
  stream-json`, as current Claude Code releases require it in print mode and
  reject the combination without it at argument-validation time (every attempt
  failed identically). If an older CLI rejects `--verbose`, a single bounded
  retry without it self-heals the version skew.
- **`codex` adapter** — the `codex exec --help` probe is now only a first
  guess: when the installed CLI rejects the probed autonomy flag at
  argument-parse time (`error: unexpected argument … found`, e.g. a CLI that
  accepts neither `--full-auto` nor `--approve-for-me`), the adapter retries
  once with the alternate flag set and caches the choice, instead of failing
  every daemon retry with the same bad flag.
- **`hermes` adapter** — honor the usage report's `failed`/`failure` fields.
  The CLI exits 0 on API-level failures (e.g. `HTTP 401: Access denied due to
  missing subscription key`), which previously produced a silent COMPLETED
  receipt with `ok: true`; such runs now fail properly with the reported
  failure reason in the receipt.

## [0.9.0] — 2026-09-19

Productization release: **durable execution for coding agents** becomes the
front door. Maestro is now understandable, installable, diagnosable, and
auditable end to end.

### Added

- **Execution receipts** — `maestro task receipt <id|n>` (and `--json`) renders
  a task's full story from its durable state: final state, per-attempt phase /
  agent / duration / cost, the deterministic verification result and command,
  work-mode gate verdicts and bounce count, and totals. The receipt is a
  projection of existing durable task state (no second database), works for
  running, completed, failed, and canceled tasks, and survives daemon restarts.
- **`/tasks/<id>/receipt` HTTP endpoint** — the daemon serves the same receipt
  over its API; the web console shows it inline on each task's detail pane
  (implementation → verification → review/fix chain with durations, costs, and
  the final result).
- **`maestro doctor`** (and `--json`) — fast, deterministic, strictly read-only
  environment diagnostics: Maestro version, Python, system, state directory
  (existence/writability/config paths/storage backend), daemon reachability
  (env URL or liveness-checked marker, auth status), git, every known agent CLI
  (found/version/status) plus registered agents, the target workspace (git
  root, project type, detected verification command), and budget caps with
  current spend. Missing optional agents and a missing daemon are reported but
  never fail doctor; it exits non-zero only on a genuinely blocking problem
  (unusable state directory or invalid configuration).
- **Packaging** — the project builds a wheel and an sdist (`python -m build`)
  containing the Python package, the prebuilt web console assets
  (`maestro/web_dist/`), and full metadata; `pip install .` works in a clean
  environment without editable mode. `MANIFEST.in` includes LICENSE, README,
  CHANGELOG, and the web assets in the sdist.
- **Apache-2.0 license** — `LICENSE` added; packaging declares it (PEP 639).
- **Examples** — `examples/` now contains ready-to-adapt handoff files:
  `basic.toml`, `fallback.toml`, `work-mode-production.toml`, `context.toml`,
  plus a README explaining each.
- **Smoke script** — `scripts/smoke-fake-agent.sh` proves the full loop
  (delegate → attempt → output → verification → receipt → durable reload) with
  a deterministic fake agent, with no dependency on any real coding-agent CLI.

### Improved

- **Cost capture** — adapter-reported usage under either `cost_usd` or
  `total_cost_usd` now counts toward budgets and receipts (previously only the
  `total_cost_usd` spelling was seen).
- **Robustness without git** — project-root resolution degrades gracefully when
  the `git` binary is unavailable instead of failing task setup.
- **README** — rewritten around the durable-execution positioning: why Maestro,
  a task-flow diagram, an execution-receipt example, and a quickstart that
  runs install → doctor → discover → daemon → delegate → watch → receipt.
- **Documentation** — CLI reference now documents `doctor` and `task receipt`;
  version references updated to 0.9.0; a new concepts page explains how Tasks,
  Attempts, Agents, and Execution Receipts relate.

### Documentation

- New: `docs/usage/explanation/concepts.md` (core object model),
  `examples/README.md`.
- Updated: README (positioning, quickstart, CLI table, license section),
  `docs/usage/reference/cli.md`, `docs/usage/tutorials/first-delegation.md`,
  `docs/usage/reference/configuration.md`.

### Release readiness

- **CI** — GitHub Actions matrix (Python 3.11/3.12/3.13) runs the full suite
  with the 100% coverage gate, builds the wheel + sdist, validates a clean
  install of both artifacts in fresh virtualenvs (`scripts/validate-package.sh`),
  runs the fake-agent smoke test, and rebuilds the web bundle to prove the
  checked-in `maestro/web_dist/` is in sync. A release workflow on `v*` tags
  re-runs all gates, enforces tag == pyproject == `maestro.VERSION`, and
  publishes artifacts (wheel + sdist) to GitHub Releases — never to PyPI (the
  name is taken by an unrelated project).
- **Clean-install verification** — `scripts/validate-package.sh` builds both
  artifacts and installs each into a throwaway venv, checking `maestro
  --version/--help/doctor`, the `maestro-mcp`/`maestro-daemon` entry points,
  and the packaged web assets. Packaging tests in `tests/test_packaging.py`
  pin version, license, metadata, manifest, and asset presence; a marker test
  in `tests/test_console_js.py` catches stale console bundles even without node.
- **Flagship demo** — `scripts/demo-v0.9.sh`: deterministic fake agents drive a
  full work-mode cycle (implement → failing verification gate → auto-fix bounce
  → re-verification) and prove the receipt survives daemon shutdown; the
  90-second storyboard is `docs/demo-v0.9.md`. `examples/overnight.toml`
  documents multi-task batches (separate workspaces, inspect via receipts — no
  scheduler). A 90-second terminal recording of a real demo run
  (`docs/assets/demo-v0.9.gif`, produced with VHS) is embedded in the README.
- **Contributor & security docs** — `CONTRIBUTING.md` (setup, the test bar,
  design principles) and `SECURITY.md` (reporting path + threat model: Maestro
  executes external processes with your privileges; loopback-only daemon by
  default, token required for any non-loopback bind).
- **Real-agent validation** — `docs/release/real-agent-validation.md`: the
  manual, labeled procedure for validating against real agent CLIs (deliberately
  outside CI); screenshot capture instructions in `docs/assets/README.md`.
- **Receipt readability** — the human receipt now shows a single scannable line
  per attempt (phase / agent / duration / cost / ✓-✗) and a `Runs` summary that
  disambiguates total executions from the attempts list
  (`6 (4 agent · 2 verification)`). The JSON shape is unchanged.
- **Coverage gate** — `fail_under = 100` now lives in `.coveragerc` itself, so
  a plain `coverage report` enforces the gate as well as CI's explicit flag.

### Fixed

- **Work-mode presets over the wire** — `explicit_target` (whether the target
  agent was named by the user or is just a default) was dropped when handoffs
  crossed the JSON-RPC boundary, so `maestro delegate --mode <preset>` without
  `--target` ran the default agent instead of the preset's implementer. The flag
  is now serialized with the document; legacy persisted records keep the old
  heuristic.

## [0.8.x] — prior milestones

Highlights from the 0.8 line (see git history for the full record):

- **Context injection** — typed, user-controlled context entries (text / file /
  Agent Skills) composed from config and handoffs, staged into agent turns and
  recorded on the task (`task audit` shows exactly what each turn received).
- **Work modes** — named presets pinning agents to the implement / verify /
  review / fix phases with bounded bounces; deterministic verification always
  runs first and an LLM verdict can only add failures.
- **Cross-machine daemons** — `--bind`, token auth, host-aware P2P discovery,
  remote agent registration over the A2A protocol (`a2a_remote`).
- **MCP server + web console** — `maestro-mcp` for Claude Code supervision;
  React console served by the daemon with live SSE output.
- **Budget caps** — per-agent and daily USD caps enforced at launch time.
- **Durable user-level state** — claim journal + registry under `~/.maestro`;
  tasks survive worktree changes and daemon restarts.
