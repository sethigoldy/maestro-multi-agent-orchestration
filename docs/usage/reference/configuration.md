# Configuration reference

Maestro configuration consists of TOML config files (merged by precedence),
environment variables, and the on-disk state layout.

## Config files

Three locations are read, in this order; later files override earlier ones per
top-level table (sub-tables merge key by key):

1. `$MAESTRO_HOME/config.toml` — user level (default `~/.maestro/config.toml`)
2. `<project-root>/.maestro/config.toml` — project level
3. `<workspace-root>/.maestro/config.toml` — active worktree level

The project root is the git common directory's parent when available, else the
workspace itself. Unreadable or unparsable files are skipped silently; an
invalid value that *is* read raises an error (e.g. unsupported effort).

### `[defaults]` — routing defaults

```toml
[defaults]
agent    = "codex"          # default implementation agent
fallback = ["claude_code"]  # optional fallback chain (list of agent names)
model    = "gpt-5.6-luna"   # optional; applied when the handoff sets none
effort   = "max"            # optional: low | medium | high | xhigh | max
```

Consulted at delegate time when a handoff names no target agent: `agent`
becomes the task's target (as if explicitly chosen), `fallback` fills an empty
fallback chain, and `model`/`effort` are applied to the chosen agent only when
the handoff does not set them. An explicit handoff target always wins over
`[defaults].agent`. If neither the handoff nor `[defaults]` names an agent,
the task parks in state `input-required` with a question listing every
available agent; answer it via MCP `answer_task_question` (a bare agent name,
`agent=… model=…` pairs, or JSON). Unknown keys and invalid values are errors
at daemon start. Shown by `maestro config`.

### `[codex]`

```toml
[codex]
model = "gpt-5.6-luna"
effort = "max"            # low | medium | high | xhigh | max
```

Default model/effort for Codex-targeted work, shown by `maestro config`.
When a file value is absent, the environment variables `MAESTRO_CODEX_MODEL`
and `MAESTRO_CODEX_EFFORT` are used as fallbacks (file values win over
environment). An effort outside the five supported values is an error.

### `[verification]`

```toml
[verification]
command = ["make", "check"]     # or a string, split on whitespace
```

Parsed and validated (string or list of strings). Note: this value is stored
but **not consumed** by the daemon's verification step — verification uses the
handoff's `verification` mode plus auto-detection (see below). It is kept for
forward compatibility.

### `[storage]`

```toml
[storage]
backend = "filesystem"    # filesystem (default) | memvara
```

The durable-state backend. When absent, `MAESTRO_STORAGE` is used as a
fallback. Switching backends for existing state: `maestro storage
migrate-memvara`.

### `[modes]` — work-mode presets

```toml
[modes.economy]
implementer = "codex-mini"     # required — cheap model, low effort
verifier    = "codex-mini"     # optional LLM verification pass
reviewer    = "codex"          # expensive: verifies requested changes only
fixer       = "codex-mini"     # optional → defaults to implementer
max_bounces = 2                # optional → default 2; 0 = no auto-fix, park on first issue
```

Each `[modes.<name>]` table is one named preset pinning agents to the phases of
a task cycle (see [Work modes in the README](../../../README.md#work-modes) and
[Configure work modes](../how-to/configure-work-modes.md)). Keys:

| Key | Type | Default | Notes |
|---|---|---|---|
| `implementer` | string | — (required) | The agent that implements; becomes the task's target when the handoff sets no explicit target |
| `verifier` | string | omitted | Optional LLM verification gate turn; deterministic verification always runs first and can never be overridden by it |
| `reviewer` | string | omitted | LLM review gate turn over the diff of requested changes |
| `fixer` | string | `implementer` | Agent for auto-fix bounces after a failed gate |
| `max_bounces` | integer ≥ 0 | `2` | Cap on auto-fix bounces; `0` parks on the first issue |

Validation at load: `[modes]` must be a table of preset tables, each with an
`implementer`; unknown keys, non-string slot values, and non-integer/negative
`max_bounces` are errors. Agent names are checked against the live registry at
delegate time (presets are config, registries are state). Precedence follows
the config chain: a project's `[modes.<name>]` table wins over the user-level
one for that preset name. `maestro config` prints every defined preset.

### `[context]` — standing context entries

```toml
[context.style]
text = "Follow docs/STYLE.md; error shapes live in src/api/errors.py."

[context.pdf-skill]
kind   = "skill"                 # text (default) | file | skill
path   = "~/skills/pdf-processing"  # directory containing SKILL.md
phases = ["implementer"]         # optional — default: all phases
```

Each `[context.<label>]` table is one standing context entry, keyed by label
(see [Context injection in the README](../../../README.md#context-injection)).
The table key is the label; an explicit conflicting `label` field inside the
table is an error. Entry keys:

| Key | Type | Default | Notes |
|---|---|---|---|
| `text` | string | — | Required for `kind = "text"`; exactly one of `text`/`path` |
| `path` | string | — | Required for `file`/`skill`; relative paths resolve against the workspace, `~` expands |
| `kind` | string | inferred | `text` when only `text` is set, else `file`; explicit `"skill"` requires a directory with a `SKILL.md` |
| `phases` | list of strings | all three | Subset of `implementer`, `verifier`, `reviewer` |

Standing entries are composed into every task at delegate time; per-task
`[[context]]` handoff entries override them by label. Skill paths are checked
at delegate time (missing directory or `SKILL.md` fails the delegation).
Precedence follows the config chain: a project's `[context.<label>]` table
wins over the user-level one for that label. Validation at load is strict —
invalid entries raise, like `[modes]`. `maestro config` lists every defined
entry with its source.

### `[continuation]` — task continuation context

```toml
[continuation]
enabled    = true    # default: reuse the compact task-knowledge snapshot on follow-ups
max_tokens = 6000    # default: budget for the injected snapshot (chars ≈ 4 × tokens)
```

Controls how `followup` (CLI `task continue`, MCP `followup`, A2A
`tasks/followup`) builds the next turn's context. In the default `reuse` mode,
the daemon projects a compact **task-knowledge** snapshot — goal, current
state, files changed, latest verification result and failures, known issues,
and a bounded tail of the last turn's output — from durable state and injects
it as one labeled context entry; raw history stays in the task record and is
never replayed. `enabled = false` turns follow-ups into clean-context turns
(equivalent to always passing `--context fresh`). `max_tokens` caps the
snapshot size: sections are dropped or truncated deterministically until it
fits (the budget is a character budget of `4 × max_tokens`, so values are
comparable across models). `MAESTRO_CONTINUATION_MAX_TOKENS` overrides
`max_tokens` per daemon process when set to a positive integer.

## Verification auto-detection

With handoff `verification = "auto"` (the default), the check command is
selected in this order — first match wins:

| # | Condition | Command |
|---|---|---|
| 1 | `Makefile` exists | `make check` |
| 2 | `package.json` with a `test` script + `pnpm-lock.yaml` | `pnpm test` |
| 3 | same, with `yarn.lock` | `yarn test` |
| 4 | same, without pnpm/yarn lockfile | `npm test` |
| 5 | `go.mod` exists | `go test ./...` |
| 6 | `Cargo.toml` exists | `cargo test` |
| 7 | Python project (`pyproject.toml`, or `pytest.ini`/`tox.ini`/`setup.cfg`, or a `tests/` directory) and pytest imports in the selected interpreter | `<python> -m pytest` |
| 8 | fallback (including case 7 without pytest installed) | `git diff --check` |

The selected Python interpreter is, in order: `$MAESTRO_PYTHON` (if it is a
file), `<workspace>/.venv/bin/python` (if executable), else the daemon's own
interpreter. In every case `git diff --check` also runs; verification passes
only if both it and the selected command exit 0. The full report (command,
stdout, stderr) is written to `~/.maestro/tasks/<task-id>/verification.txt`.

With `verification = "command"`, the handoff's `request` field is shlex-split
and run as the check command instead of auto-detection. With `verification =
"none"`, no check runs and the completion metadata records `skipped`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MAESTRO_HOME` | `~/.maestro` | State directory (registry, claim journal, daemon marker, peers, tasks) |
| `MAESTRO_WORKSPACE` | cwd | Default workspace for CLI task commands; an empty value scopes to the home directory |
| `MAESTRO_DAEMON_URL` | from `daemon.json` | Daemon endpoint for CLI daemon commands (e.g. another machine's) |
| `MAESTRO_DAEMON_TOKEN` | — | Bearer token for that endpoint; also the stable token a daemon uses when it generates one on a non-loopback bind |
| `MAESTRO_MAX_RETRIES` | `2` | Retry attempts per agent before moving to the next fallback (total attempts = 1 + N) |
| `MAESTRO_BACKOFF_S` | `1.0` | Base seconds between retries; delay is linear: base × (attempt + 1) |
| `MAESTRO_DELEGATE_TIMEOUT` | `3600` | Max seconds the MCP `delegate`/`followup` tools block for a task |
| `MAESTRO_BUDGET_PER_AGENT_USD` | off | Cumulative USD cap per agent name (see Budget caps) |
| `MAESTRO_BUDGET_DAILY_USD` | off | Daily USD cap across all agents, reset at UTC midnight |
| `MAESTRO_DISCOVERY` | `1` | Set `0` to disable P2P discovery entirely |
| `MAESTRO_DISCOVERY_PORT` | `9786` | UDP port for the presence channel |
| `MAESTRO_DISCOVERY_IF` | default interface | Interface used for announcements (e.g. `127.0.0.1` for loopback only) |
| `MAESTRO_DISCOVERY_TTL` | `1` | Hop distance: `0` = this machine only, `1` = LAN |
| `MAESTRO_NODE_NAME` | `maestro-node` | Name this daemon announces under discovery |
| `MAESTRO_STORAGE` | — | Storage backend fallback when no config file sets it |
| `MAESTRO_CODEX_MODEL` / `MAESTRO_CODEX_EFFORT` | — | Codex model/effort fallbacks when no config file sets them |
| `MAESTRO_LOGIN_ENV` | `1` | Set `0` to stop passing a login-shell environment snapshot (`$SHELL -lc 'env -0'`) to spawned agents. Values that span several lines, such as a PEM key, are kept whole, and anything the profile prints before the variables is ignored |
| `MAESTRO_LOGIN_ENV_TIMEOUT_S` | `10` | Max seconds to wait for the login-shell snapshot before falling back to the daemon's own environment |
| `MAESTRO_CONTINUATION_MAX_TOKENS` | from `[continuation]` | Positive-integer override of the continuation context budget (see `[continuation]`) |
| `MAESTRO_PYTHON` | — | Interpreter for verification's pytest probe (must be a file) |

Misconfigured budget values (non-numeric, negative) are ignored rather than
blocking delegation.

## Budget caps

Caps are per-daemon: set them in the environment where the daemon starts.

- `MAESTRO_BUDGET_PER_AGENT_USD` — cumulative USD cap per agent name, across
  all tasks.
- `MAESTRO_BUDGET_DAILY_USD` — cumulative USD cap across all agents for tasks
  started today (UTC).

Spend is computed from attempt usage: each attempt records the usage its agent
produced (`total_cost_usd`), so attribution is per agent and failed attempts
count (failed work still costs money). Enforcement happens only at launch: an
exhausted cap refuses new delegations with a clear error; running tasks always
finish. Agents that report no cost contribute $0. `maestro budgets` shows caps
and current spend.

## Where state lives

```text
~/.maestro/                        (or $MAESTRO_HOME)
├── registry.json                  # registered agents
├── state.jsonl                    # durable claim journal (append-only; subjects maestro:task:<id>)
├── daemon.json                    # last-started broker marker: pid, host, port, token (when auth is on)
├── peers.json                     # discovered/registered peers
├── config.toml                    # user-level config
├── migrations/                    # migration markers per project/backend
└── tasks/<task-id>/               # per-task artifacts
    ├── result-<agent>-t<turn>-<attempt>.json
    ├── verification.txt
    └── … adapter logs
```

State is user-level, not per-project: it survives worktree creation, switching,
and deletion. Each task records the exact workspace it ran in and its project
root. Project-level files are configuration only (`.maestro/config.toml`).
