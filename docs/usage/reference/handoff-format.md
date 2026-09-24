# Handoff format reference

The Maestro handoff document is the normalized work order carried by every
delegation. It has four sections plus an optional per-task settings table:

```text
[handoff]       what to do        (title, request, design, context pointers)
[routing]       who does it       (target_agent, fallback, origin_agent, parent_task,
                                   work modes: mode, review_agent, verify_agent, fix_agent, max_bounces)
[expectations]  what done looks like (artifacts, verification, commit_policy, branch, budget_hint)
[constraints]   guardrails        (sensitive, max_depth_remaining)
[[context]]     user-controlled context entries (label + text|path, kind, phases)
agent_settings  per-task overrides for the target agent (model, effort, …)
```

The document is carried as structured data in A2A message parts and stored
with the task.

## Fields

### `[handoff]`

| Field | Type | Default | Required | Notes |
|---|---|---|---|---|
| `title` | string | — | yes | Short, non-empty summary of the work |
| `request` | string | — | yes | The work order in prose. With `verification = "command"` and no `verification_command`, this field is instead interpreted as a shell command line (see below) |
| `design` | string | `""` | no | Authoritative design; multi-line text. Absent means the agent uses judgment within the request's scope |
| `context_files` | list of strings | `[]` | no | Paths the agent should read for context |
| `context_notes` | string | `""` | no | Free-form guidance about the context |

### `[routing]`

| Field | Type | Default | Required | Notes |
|---|---|---|---|---|
| `target_agent` | string | `"codex"` | yes (non-empty) | Registered agent name or built-in kind. Must differ from `origin_agent` |
| `fallback` | list of strings | `[]` | no | Agents tried in order if the target fails or is unavailable; entries must be non-empty |
| `origin_agent` | string | `"human"` | no | Who asked for the work — a person, or the name of a supervising agent delegating via MCP |
| `parent_task_id` | string or null | `null` | no | Set automatically by follow-ups; not normally set by authors |
| `mode` | string | `null` | no | Name of a `[modes.NAME]` work-mode preset. Expanded at delegate time: the preset's `implementer` becomes `target_agent` (unless one was set explicitly), and its optional slots fill in the fields below where they are absent |
| `review_agent` | string | `null` | no | Agent for the LLM review gate turn after verification. Must differ from `target_agent` (self-review is refused) |
| `verify_agent` | string | `null` | no | Agent for the optional LLM verification gate turn; runs in addition to, never instead of, the deterministic check |
| `fix_agent` | string | `null` | no | Agent for auto-fix bounces after a failed gate. Defaults to the implementer (`target_agent`) when omitted, also when a `mode` preset without a `fixer` is applied to a handoff that names its own target |
| `max_bounces` | integer | `2` (preset default) | no | Cap on auto-fix bounces; must be an integer ≥ 0 when set. `0` = park on the first issue |

Work-mode precedence: explicit fields on the handoff beat the preset for that
task — a handoff with both `mode = "economy"` and `review_agent = "other"` uses
the preset's implementer but reviews with `other`. A task with no `mode` and no
gate fields behaves exactly as before (deterministic verification only). See
[Configure work modes](../how-to/configure-work-modes.md).

### `[expectations]`

| Field | Type | Default | Required | Notes |
|---|---|---|---|---|
| `artifacts` | list of strings | `["code"]` | no | What the work should produce (e.g. `"code"`, `"tests"`, `"docs"`) — guidance, not enforced |
| `verification` | string | `"auto"` | no | One of: `auto`, `command`, `none`. `auto`: deterministic auto-detected check after the agent finishes. `command`: `verification_command` (or, when that is not set, the `request` field) is shlex-split and run as the check command. `none`: skipped |
| `verification_command` | string or null | `null` | no | The check command for `verification = "command"`, for example `"pytest -q"`. Setting it lets `request` stay a prose work order. It must be a non-empty string and requires `verification = "command"`. Follow-ups keep running this command: when it is not set, the original `request` is carried forward as the command, so a follow-up's instruction is never run as a shell command |
| `commit_policy` | string | `"branch"` | no | One of: `no-commit`, `branch`, `pr`. `branch`/`pr` create the task branch (named by `branch` below) and require a git workspace; `no-commit` works in place |
| `branch` | string or null | `null` | no | The name of the task's git branch, for example `"feat/login-form"`. When it is not set, the branch is `maestro/<task-id>`. The branch must not exist yet, and its name must not clash with an existing branch as a folder. Git keeps a branch such as `feat/login` as a file inside a folder called `feat`, so when a branch `feat` exists, `feat/login` cannot be created, and when `feat/x` exists, `feat` cannot be created. Delegation is refused in both cases. It must be a valid git branch name, and it cannot be combined with `commit_policy = "no-commit"`. Every later turn of the task, including follow-ups, uses the same branch. To change the name after the task has run, or when its first turn could not create the branch, see `maestro task rename-branch` in the [CLI reference](cli.md#task). The name applies only to the workspace of the daemon that runs the task. When the task runs on a remote daemon (an `a2a_remote` agent), the handoff Maestro forwards to it has `branch` removed, because the remote gets a new request on every attempt and every turn and would otherwise refuse each one after the first as "already exists". The remote daemon puts its work on its own default branch, `maestro/<remote-task-id>` |
| `agent_may_commit` | bool | `false` | no | Whether the agent may commit its work. By default the agent's prompt tells it not to commit and not to create, switch or delete branches, so its changes stay uncommitted for the supervisor to review and commit; Maestro itself never commits. With `true`, the prompt allows commits to the branch that is checked out, but still forbids pushing and switching branches. It cannot be `true` together with `commit_policy = "no-commit"`. Some agents run in a sandbox that cannot write to `.git` (Codex in its workspace-write sandbox, for example); their commits then fail even when this is `true` |
| `budget_hint` | number or null | `null` | no | Must be positive when set. Recorded on the task for visibility; enforcement is via the `MAESTRO_BUDGET_*_USD` environment caps, not this field |

### `[constraints]`

| Field | Type | Default | Required | Notes |
|---|---|---|---|---|
| `sensitive` | boolean | `false` | no | When true, the task pauses in `input-required` with an approval question before any agent runs |
| `max_depth_remaining` | integer | `3` | no | Must be an integer ≥ 0 (booleans, decimals such as `0.5` and strings such as `"2"` are rejected). Refuses nesting at/below 0; each follow-up decrements by one |

### `[[context]]` (array of tables)

User-controlled context entries (see [Context injection in the README](../../../README.md#context-injection)). Each entry is one labeled unit of context injected into the agent's prompt at delegate time:

| Field | Type | Default | Required | Notes |
|---|---|---|---|---|
| `label` | string | — | yes | Non-empty; identifies the entry and is the merge key against standing `[context.<label>]` config entries (handoff wins per label) |
| `kind` | string | inferred | no | One of `text`, `file`, `skill`. Inferred: `text` when only `text` is set, else `file` |
| `text` | string | — | for `text` kind | Inline instruction; exactly one of `text`/`path` must be set |
| `path` | string | — | for `file`/`skill` kind | For `file`: a path inlined when ≤8KB, otherwise copied to the task dir and referenced. For `skill`: a directory containing a `SKILL.md`, staged into the task (Claude Code discovers it via `--add-dir`; other agents get a prompt reference) |
| `phases` | list of strings | all three | no | Subset of `implementer`, `verifier`, `reviewer`. Fix bounces and follow-ups count as `implementer`. Lets an entry target only gate turns (e.g. a reviewer checklist) |

The composed list (standing config entries + these, handoff overriding by
label) is stored on the task record, so `task audit` shows exactly what each
turn received. Skill entries are validated at delegate time: a missing
directory or `SKILL.md` fails delegation before any agent runs.

### `agent_settings` (top-level table)

| Field | Type | Notes |
|---|---|---|
| `model` | string | Per-task model override for the target agent |
| `effort` | string | One of `low`, `medium`, `high`, `xhigh`, `max` |
| *(any other key)* | — | Passed through to the adapter as a run setting; reserved key `maestro_handoff` (the full document) is added automatically for remote hops |

Precedence for settings: `agent_settings` (this task) overrides the agent's
registry entry, which overrides adapter defaults. `model` and `effort` apply to
the target agent only. Fallback agents, gate agents and a fixer that is a
different agent run with their own registry `model` and `effort`. Every other
key reaches all of them.

## Validation rules

A handoff is rejected with a `ValueError` when any of these holds:

- `title`, `request`, or `target_agent` is empty/whitespace
- `commit_policy` is not one of `no-commit`, `branch`, `pr`
- `branch` is set but is not a valid git branch name (for example it contains a space, `..`, `~`, `^`, `:`, `?`, `*`, `[` or `\`, starts with `-`, or ends with `/`, `.` or `.lock`), or it is set together with `commit_policy = "no-commit"`
- `verification` is not one of `auto`, `command`, `none`
- `verification_command` is set but empty, or set without `verification = "command"`
- `max_depth_remaining` < 0
- `budget_hint` is set and ≤ 0
- any `fallback` entry is empty
- `max_bounces` is set but not an integer ≥ 0 (booleans are rejected)
- a `[[context]]` entry is not a table, has no non-empty `label`, sets both or neither of `text`/`path`, names an unknown `kind`, or carries a `phases` list with unknown/empty values
- a field has the wrong type. A list field (`context_files`, `fallback`, `artifacts`) must be a list of strings; a single string such as `fallback = "claude"` is rejected rather than read as a list of letters. Text fields (`title`, `request`, `design`, `target_agent`, `mode`, the gate agent fields and so on) must be strings, `max_depth_remaining` must be an integer, `budget_hint` must be a number, `[[context]]` must be a list of tables and `agent_settings` must be a table. A null `title` or `request` counts as missing, and a null optional field takes its default.

Additional refusals happen at delegation time (see
[Delegate a task — rules](../how-to/delegate-a-task.md#rules-that-will-refuse-your-delegation)):
self-delegation, depth exhausted, missing workspace, non-git workspace with
`commit_policy = "branch"`, a `branch` name that already exists in the
workspace or clashes with an existing branch as a folder, budget cap exceeded. With work modes: an unknown
`mode` name, an unknown agent in any of the preset's slots or the explicit gate
fields (the error lists the registered agents), and `review_agent == target_agent`
(self-review is not a gate).

## File loading rules

`load_handoff_file(path)` (used by `maestro delegate --file` and the MCP
`delegate` tool) accepts:

1. **JSON object containing any of the section keys** (`handoff`, `routing`,
   `expectations`, `constraints`, `context`) → parsed as the 4-section document.
2. **JSON object with `title` and `request` but no section keys** → parsed as a
   legacy 0.8.x handoff (below).
3. **Anything else that is not valid JSON** → parsed as TOML (4-section shape).

Errors: missing file, non-object JSON, TOML decode failure, validation failure
— each raised with a message naming the problem.

## Legacy 0.8.x format

Old staged handoff files are still accepted and converted automatically:

| Legacy field | Mapped to | Notes |
|---|---|---|
| `title` | `handoff.title` | required |
| `request` | `handoff.request` | required |
| `design_file` | `handoff.design` | the file's text content is read in |
| `implementer` | `routing.target_agent` | default `"codex"` when absent |
| `supervisor` | `routing.origin_agent` | default `"human"` when absent (old files rarely named their supervisor) |
| `model` | `agent_settings.model` | copied when present |
| `effort` | `agent_settings.effort` | copied when present |
| `branch` | `expectations.branch` | copied when present |

`commit_policy` is forced to `"branch"` for legacy documents.

## Serialization

Documents serialize back to the same 4-section shape (JSON via `to_dict`;
TOML via the registry's minimal writer, which escapes multi-line strings).
Round-tripping a document through either format preserves all fields.
