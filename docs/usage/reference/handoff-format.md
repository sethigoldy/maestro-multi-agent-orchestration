# Handoff format reference

The Maestro handoff document is the normalized work order carried by every
delegation. It has four sections plus an optional per-task settings table:

```text
[handoff]       what to do        (title, request, design, context pointers)
[routing]       who does it       (target_agent, fallback, origin_agent, parent_task,
                                   work modes: mode, review_agent, verify_agent, fix_agent, max_bounces)
[expectations]  what done looks like (artifacts, verification, commit_policy, budget_hint)
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
| `request` | string | — | yes | The work order in prose. With `verification = "command"`, this field is instead interpreted as a shell command line (see below) |
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
| `verification` | string | `"auto"` | no | One of: `auto`, `command`, `none`. `auto`: deterministic auto-detected check after the agent finishes. `command`: the `request` field is shlex-split and run as the check command. `none`: skipped |
| `commit_policy` | string | `"branch"` | no | One of: `no-commit`, `branch`, `pr`. `branch`/`pr` create branch `maestro/<task-id>` and require a git workspace; `no-commit` works in place |
| `budget_hint` | number or null | `null` | no | Must be positive when set. Recorded on the task for visibility; enforcement is via the `MAESTRO_BUDGET_*_USD` environment caps, not this field |

### `[constraints]`

| Field | Type | Default | Required | Notes |
|---|---|---|---|---|
| `sensitive` | boolean | `false` | no | When true, the task pauses in `input-required` with an approval question before any agent runs |
| `max_depth_remaining` | integer | `3` | no | Must be ≥ 0. Refuses nesting at/below 0; each follow-up decrements by one |

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
registry entry, which overrides adapter defaults.

## Validation rules

A handoff is rejected with a `ValueError` when any of these holds:

- `title`, `request`, or `target_agent` is empty/whitespace
- `commit_policy` is not one of `no-commit`, `branch`, `pr`
- `verification` is not one of `auto`, `command`, `none`
- `max_depth_remaining` < 0
- `budget_hint` is set and ≤ 0
- any `fallback` entry is empty
- `max_bounces` is set but not an integer ≥ 0 (booleans are rejected)
- a `[[context]]` entry is not a table, has no non-empty `label`, sets both or neither of `text`/`path`, names an unknown `kind`, or carries a `phases` list with unknown/empty values
- a field has the wrong type. A list field (`context_files`, `fallback`, `artifacts`) must be a list of strings; a single string such as `fallback = "claude"` is rejected rather than read as a list of letters. Text fields (`title`, `request`, `design`, `target_agent`, `mode`, the gate agent fields and so on) must be strings, `max_depth_remaining` must be an integer, `budget_hint` must be a number, `[[context]]` must be a list of tables and `agent_settings` must be a table. A null `title` or `request` counts as missing, and a null optional field takes its default.

Additional refusals happen at delegation time (see
[Delegate a task — rules](../how-to/delegate-a-task.md#rules-that-will-refuse-your-delegation)):
self-delegation, depth exhausted, missing workspace, non-git workspace with
`commit_policy = "branch"`, budget cap exceeded. With work modes: an unknown
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

`commit_policy` is forced to `"branch"` for legacy documents.

## Serialization

Documents serialize back to the same 4-section shape (JSON via `to_dict`;
TOML via the registry's minimal writer, which escapes multi-line strings).
Round-tripping a document through either format preserves all fields.
