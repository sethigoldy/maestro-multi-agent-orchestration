# Changelog

All notable changes to Maestro are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semantic versioning.

## [Unreleased]

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
