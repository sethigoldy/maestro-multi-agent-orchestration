# Changelog

All notable changes to Maestro are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semantic versioning.

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
