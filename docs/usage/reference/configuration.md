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
neither the handoff nor that agent's registry entry sets them. A value in the
agent's registry entry therefore beats `[defaults]`, and a value in the handoff
beats both. Fallback agents never receive `[defaults].model` or `effort`; they
run with their own registry settings. An explicit handoff target always wins over
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
| `fixer` | string | the task's implementer | Agent for auto-fix bounces after a failed gate. When omitted, fixes go to the agent that implements the task: the handoff's explicit target if it names one, otherwise this preset's `implementer` |
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
When a turn starts, each skill is copied into the task directory. If the skill
cannot be found or copied at that point, the turn still runs, and its context
block says that the skill is unavailable and why. When two labels reduce to the
same file name (for example `code review` and `code/review`), the later entry
is staged under a numbered name, so neither copy overwrites the other.
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
| 1 | The makefile defines an explicit `check` rule (see below) | `make check` |
| 2 | `package.json` with a `test` script (other than the `npm init` default) + `pnpm-lock.yaml` | `pnpm test` |
| 3 | same, with `yarn.lock` | `yarn test` |
| 4 | same, without pnpm/yarn lockfile | `npm test` |
| 5 | `go.mod` exists | `go test ./...` |
| 6 | `Cargo.toml` exists | `cargo test` |
| 7 | Python project (`pyproject.toml`, or `pytest.ini`/`tox.ini`/`setup.cfg`, or a `tests/` directory) and pytest imports in the selected interpreter | `<python> -m pytest` |
| 8 | Python project with a test suite now or when the turn started (see below), but pytest does not import in the selected interpreter | `<python> -m pytest`, which fails; see below |
| 9 | fallback (including a Python project with no test suite and no pytest) | `git diff --check` |

Details of each rule:

- **Makefile.** Maestro decides from the makefile text alone and never runs
  make, not even as a `make -n` dry run. A dry run can change the workspace:
  GNU make remakes included makefiles, runs recipe lines that use `$(MAKE)` or
  start with `+`, and runs `$(shell ...)`. The makefile is the first of
  `GNUmakefile`, `makefile` and `Makefile` that exists, which is the one make
  reads. A file it includes with `include`, `-include` or `sinclude` is read
  too, when the include names a literal path (no variables or wildcards) to a
  file inside the workspace; includes inside included files are followed the
  same way, up to 50 files in all. `make check` is chosen only when one of
  these files has an explicit rule whose targets include `check`, such as
  `check:`, `check::` or `lint check:`. A line that ends with a backslash is
  joined with the next line first. These lines do not count as a `check`
  rule:
  - lines inside a `define ... endef` block, including nested blocks;
  - recipe lines, which start with a tab;
  - comments and variable assignments, such as `check := yes`;
  - target-specific variables, such as `check: PYTEST_ARGS = -q`, and the
    same with `:=`, `+=`, `?=`, `!=`, or an `export`, `override` or `private`
    prefix.

  A target that make could only build from a built-in rule (for example
  `%: %.sh` with a `check.sh` file) or from a catch-all rule (`%:` or
  `.DEFAULT:`) does not count either, because `make check` would then run no
  tests. Conditionals such as `ifeq` are not evaluated, so a `check` rule
  inside one counts whichever branch make would take. A project without a
  `check` rule is skipped, and detection continues with the next rule.
- **Node.** The test script that `npm init` writes,
  `echo "Error: no test specified" && exit 1`, always fails. Maestro treats it
  as "this package has no tests" and continues with the next rule.
- **Python interpreter.** The selected Python interpreter is, in order:
  `$MAESTRO_PYTHON` (if it is a file), `<workspace>/.venv/bin/python` (if
  executable), `<workspace>/venv/bin/python` (if executable), else the daemon's
  own interpreter.
- **Python test suite.** A project has a Python test suite when it has any
  of these:
  - a `tests/` or `test/` directory anywhere in the project, outside hidden
    directories (names that start with `.`), virtual environments (a
    directory named `venv` or holding a `pyvenv.cfg` file), `site-packages`,
    `__pycache__` and `node_modules`;
  - a `conftest.py` file at the project root;
  - a file named `test_*.py` or `*_test.py` in the same places;
  - a `pytest.ini` file, a `[tool.pytest.ini_options]` table in
    `pyproject.toml`, a `[tool:pytest]` section in `setup.cfg`, or a
    `[pytest]` section in `tox.ini`.

  In a git repository Maestro takes the file list from `git ls-files` (tracked
  files and new files that are not ignored). Outside git it walks the
  directory tree and stops after 20,000 entries.
- **pytest missing.** If a project with a Python test suite cannot run its
  tests because pytest is not installed in the selected interpreter,
  verification fails. The project counts as having a test suite when it has
  one now, or when it had one at the start of the turn (see "No tests
  collected" below for how that is recorded). So an agent that deletes every
  test still gets the failing pytest command, not the `git diff --check`
  fallback. When nothing was recorded, for example for a task started by an
  older Maestro or when `maestro doctor` shows the command, only the
  workspace as it is now decides. The report
  names the interpreter and says how to fix it: set `MAESTRO_PYTHON` to the
  interpreter of the project's environment (for example a poetry or conda
  environment, or the main checkout's `.venv` when you work in a git
  worktree), or use `verification = "command"` with an explicit test command.
- **No tests collected.** When pytest finds no tests, it exits with code 5.
  At the start of every turn, before the agent runs, Maestro records whether
  the project has a Python test suite. It keeps this in the task record, so a
  restarted daemon still has it. What exit code 5 means depends on that
  record:
  - If the project had no test suite when the turn started, exit code 5 is
    not counted as a test failure. It is not evidence of work either, so it
    is handled like the `git diff --check` fallback: the task passes only if
    the turn left working-tree changes or new commits in the workspace.
  - If the project had a test suite when the turn started, exit code 5 is a
    failure. The report says that pytest collected no tests although the
    project had a test suite, because the tests may have been deleted,
    renamed or hidden during the turn.
  - If nothing was recorded (a task started by an older Maestro), exit code 5
    is treated as a failure too.
- **Fallback.** `git diff --check` alone passes on an untouched workspace, so
  when it is the only check, verification fails unless the turn left
  working-tree changes or new commits.

In every case `git diff --check` also runs; verification passes only if both
it and the selected command succeed. The full report (command, notes, stdout,
stderr) is written to `~/.maestro/tasks/<task-id>/verification.txt`.

With `verification = "command"`, the handoff's `request` field is shlex-split
and run as the check command instead of auto-detection. Any non-zero exit
code from that command is a failure, including exit code 5 from pytest. With
`verification = "none"`, no check runs and the completion metadata records
`skipped`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MAESTRO_HOME` | `~/.maestro` | State directory (registry, claim journal, daemon marker, peers, tasks) |
| `MAESTRO_WORKSPACE` | cwd | Default workspace for CLI task commands; an empty value scopes to the home directory |
| `MAESTRO_DAEMON_URL` | from `daemon.json` | Daemon endpoint for CLI daemon commands (e.g. another machine's) |
| `MAESTRO_DAEMON_TOKEN` | — | Bearer token for that endpoint; also the stable token a daemon uses when it generates one on a non-loopback bind |
| `MAESTRO_DAEMON_ALLOWED_ORIGINS` | — | Comma-separated browser origins (`scheme://host[:port]`) that may POST to the daemon besides its own address, such as a reverse proxy's public address. `maestro-daemon --allow-origin` replaces it when given. An invalid entry stops the daemon from starting |
| `MAESTRO_MAX_RETRIES` | `2` | Retry attempts per agent before moving to the next fallback (total attempts = 1 + N) |
| `MAESTRO_BACKOFF_S` | `1.0` | Base seconds between retries; delay is linear: base × (attempt + 1) |
| `MAESTRO_DELEGATE_TIMEOUT` | `3600` | Max seconds the MCP `delegate`/`followup` tools block for a task |
| `MAESTRO_BUDGET_PER_AGENT_USD` | off | Cumulative USD cap per agent name (see Budget caps) |
| `MAESTRO_BUDGET_DAILY_USD` | off | Daily USD cap across all agents, reset at UTC midnight |
| `MAESTRO_DISCOVERY` | on beyond loopback, off on loopback | Set `0` to disable P2P discovery entirely. A daemon that listens on loopback only runs discovery only when this is set to `1` (or `true`, `yes`, `on`) |
| `MAESTRO_DISCOVERY_PORT` | `9786` | UDP port for the presence channel |
| `MAESTRO_DISCOVERY_IF` | default interface | Interface used for announcements (e.g. `127.0.0.1` for loopback only; then announcements from other hosts are ignored). A daemon that listens on loopback only announces itself only when this is a loopback address; on another interface it listens for peers but never announces `127.0.0.1` |
| `MAESTRO_DISCOVERY_TTL` | `1` | Hop distance: `0` = this machine only, `1` = LAN |
| `MAESTRO_NODE_NAME` | `maestro-node` | Name this daemon announces under discovery |
| `MAESTRO_STORAGE` | — | Storage backend fallback when no config file sets it |
| `MAESTRO_CODEX_MODEL` / `MAESTRO_CODEX_EFFORT` | — | Codex model/effort fallbacks when no config file sets them |
| `MAESTRO_LOGIN_ENV` | `1` | Set `0` to stop passing a login-shell environment snapshot (`$SHELL -lc 'env -0'`) to spawned agents. Values that span several lines, such as a PEM key, are kept whole, and anything the profile prints before the variables is ignored |
| `MAESTRO_LOGIN_ENV_TIMEOUT_S` | `10` | Max seconds to wait for the login-shell snapshot before falling back to the daemon's own environment |
| `MAESTRO_CONTINUATION_MAX_TOKENS` | from `[continuation]` | Positive-integer override of the continuation context budget (see `[continuation]`) |
| `MAESTRO_PYTHON` | — | Python interpreter that verification uses to run pytest (must be a file). Set it when the project's environment is not in `.venv/` or `venv/` inside the workspace |

Misconfigured budget values (non-numeric, negative) are ignored rather than
blocking delegation.

## Budget caps

Caps are per-daemon: set them in the environment where the daemon starts.

- `MAESTRO_BUDGET_PER_AGENT_USD` — cumulative USD cap per agent name, across
  all tasks.
- `MAESTRO_BUDGET_DAILY_USD` — cumulative USD cap across all agents for
  attempts that finished today (UTC). Each attempt is dated by its own finish
  time, so a follow-up turn today on a task started earlier counts toward
  today's spend. An attempt recorded without a finish time is dated by the
  task's start time.

Spend is computed from attempt usage: each attempt records the usage its agent
produced (`total_cost_usd`), so attribution is per agent and failed attempts
count (failed work still costs money). Enforcement happens only at launch: an
exhausted cap refuses new delegations with a clear error; running tasks always
finish. Agents that report no cost contribute $0. `maestro budgets` shows caps
and current spend.

## Where state lives

```text
~/.maestro/                        (or $MAESTRO_HOME)
├── agents/<name>.toml             # registered agents (mode 0600: may hold a remote token)
├── registry.json                  # task registry: task numbers, titles, workspaces
├── task-counter                   # highest task number ever given out (never reused)
├── state.jsonl                    # durable claim journal (append-only; subjects maestro:task:<id>)
├── state.lock / registry.lock     # lock files; writers and gc take them
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
and deletion.

Each task records the exact workspace it ran in and its project root.
Project-level files are configuration only (`.maestro/config.toml`).

### When `registry.json` is missing or damaged

When `registry.json` is missing, Maestro rebuilds the task registry from the
task claims in `state.jsonl` and saves it, so the rebuild runs once and not on
every `maestro task list`. The rebuild runs under the registry lock.

When `registry.json` cannot be parsed, Maestro takes the registry lock and
reads the file again, because another process may have repaired it in the
meantime. If it is still damaged, Maestro moves it aside as
`registry.corrupt-<time>.json`, where it is kept for inspection, and rebuilds
the registry as above.

The rebuild keeps every task that has at least one claim:

- A task keeps the number in its `task_number` claim. A task without a usable
  `task_number` claim gets the next number after the highest ever given out
  (`task-counter`), and that number is saved as a claim, so a later rebuild
  gives the same number.
- The title comes from the `task_title` claim, or is the task id when there
  is none. The workspace comes from the `task_workspace` claim, or is empty.
- The claim journal stores no times, so `created_at` is taken from the date
  and time in the task id (`task-YYYYMMDD-HHMMSS-…`), or else from the
  modification time of the task's `tasks/<task-id>/` folder. It is empty when
  neither is available.
- A task imported from a legacy project journal keeps its
  `legacy_task_number`. The number is saved as a claim during the import, so
  this applies to tasks imported by this version or later.

When `registry.json` exists but cannot be read at that moment (for example
the process has too many open files, or no permission to read the file),
Maestro stops with an error and changes nothing. It does not rebuild the
registry and does not write over the file, because the file itself may be
fine.

### File locking

Writers take `registry.lock` to change the task registry and `state.lock` to
append to or rewrite `state.jsonl`. These are advisory `flock` locks. On
Windows, where Python has no `fcntl` module, and on file systems that refuse
`flock` (some NFS and SMB mounts), Maestro cannot take them. It prints a
warning once that names the lock file and continues without the lock. In that
case, run only one Maestro process that writes state at a time: stop the
daemon before `maestro gc`.
