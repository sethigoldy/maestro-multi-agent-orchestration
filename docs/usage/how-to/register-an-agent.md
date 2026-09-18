# How to register and configure agents

This guide shows you how to make an implementation agent available as a
delegation target: built-in CLI adapters, custom-named entries with per-agent
settings, arbitrary CLIs via the generic spec, REST task servers, and remote
Maestro daemons.

Registration is per-user and lives in `~/.maestro` (or `$MAESTRO_HOME`). See
the [CLI reference](../reference/cli.md#agents) for every option of
`maestro agents add`.

## Choose the right kind first

| You have… | Use |
|---|---|
| Codex, Claude Code, Copilot CLI, Cursor, Hermes, Pi, Cline, or OpenHands installed and authenticated | its built-in `kind` — no registration needed to delegate |
| One of those, but you want a custom name or per-agent model/effort/token | a named entry with that `kind` |
| Any other CLI that can run non-interactively | `kind = "generic"` with a launch command |
| A small HTTP service that accepts tasks | `kind = "generic"` whose command is an `http(s)` URL (API mode) |
| Another Maestro daemon on another machine | `kind = "a2a_remote"` with its URL and token |

## Use a built-in agent directly

Built-in kinds resolve to their default binary automatically, so you can
delegate without registering anything:

```bash
maestro delegate --title "T" --request "R" --target codex --workspace /path/to/repo
```

The binary must be on the daemon's `PATH` and authenticated with its normal
setup flow. Check readiness before delegating:

```bash
maestro agents status codex
```

This shows exactly what preflight will see (binary found, version). If it
reports the agent unavailable, fix the installation first — delegation to an
unavailable agent fails fast and moves on to your fallback chain.

## Register a named entry with settings

Register when you want a stable name or per-agent execution defaults:

```bash
maestro agents add --name my-codex --kind codex \
  --display-name "Codex (fast model)" \
  --skill refactoring --skill debugging
```

Per-agent `model` and `effort` are carried by the registry entry and apply to
every delegation that targets it; a single handoff can still override them per
task (see [Delegate a task](delegate-a-task.md#per-task-model-and-effort)).
Verify with:

```bash
maestro agents list
maestro agents status my-codex
```

Remove an entry with `maestro agents remove <name>`.

## Discover what is installed

```bash
maestro agents discover
```

This scans your `PATH` for the known agent CLIs and reports which are present.
It informs you; it does not register anything.

## Onboard any CLI (generic spec)

Anything that can run non-interactively from a shell works as
`kind = "generic"` — no code, just a registry entry:

```bash
maestro agents add --name mytool --kind generic \
  --command "mytool run --json {prompt}"
```

- `{prompt}` in the command is replaced with the work order (title, request,
  design, expectations). If your prompt is long or contains shell metacharacters,
  pipe it instead: `--input-mode stdin` and drop `{prompt}` from the command.
- `--output-format jsonl` makes Maestro parse `cost_usd` / usage hints from JSON
  lines automatically — this is how budget caps see costs for that agent.
  Other formats: `text` (default), `rpc`.
- `--workspace-policy flag` passes the workspace to the CLI as an argument
  instead of running it with the workspace as its current directory (`cwd`,
  the default).

Preflight checks the binary and version before any delegation, so a wrong
entry fails fast with a clear error rather than mid-task. Full recipes and a
checklist for new agents: [docs/agent-onboarding.md](../agent-onboarding.md).

## Point at a REST task server (API mode)

A generic agent whose command is an `http(s)` URL switches to API mode
automatically. Maestro submits the prompt, polls for status about once per
second, and reports output and costs against this contract:

```text
POST /tasks            {"task_id", "prompt", "workspace"}        → 202 {"id": …}
GET  /tasks/{id}       → {"state", "output", "usage", "error"}   (polled ~1/s)
POST /tasks/{id}/cancel
```

`state` stays `working` until it reaches a terminal state: `completed`,
`failed`, or `canceled`. The base URL can also come from per-agent settings
(`api_base_url`) instead of the command.

## Register a remote Maestro daemon

To delegate to another machine's daemon, that daemon must first be reachable —
bind it to the network and note its token (see [Run Maestro across
machines](run-cross-machine.md)). Then register it on this machine:

```bash
maestro agents add --name remote-b --kind a2a_remote \
  --command http://10.0.0.5:8790 \
  --token <token-printed-by-B>
maestro agents status remote-b    # live check: fetches B's agent card
```

Note the command is a URL, not a binary, and the token is the one printed by
the remote daemon at startup (or its `MAESTRO_DAEMON_TOKEN`). Maestro checks
the remote's agent card before delegating, streams its output and usage live,
and forwards cancellation. A missing or wrong token fails fast with
`HTTP 401 — check this agent's token`.

## Verify before you delegate

Whatever you registered, the same two commands give you confidence:

```bash
maestro agents list          # what the daemon will see
maestro agents status <name> # binary/version/card check for one agent
```

Then delegate with `--target <name>` and (if you want a safety net)
`--fallback <other-agent>` — see [Delegate a task](delegate-a-task.md).
