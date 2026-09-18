# Handoff format reference

The Maestro handoff document is the normalized work order carried by every
delegation. It has four sections plus an optional per-task settings table:

```text
[handoff]       what to do        (title, request, design, context pointers)
[routing]       who does it       (target_agent, fallback, origin_agent, parent_task)
[expectations]  what done looks like (artifacts, verification, commit_policy, budget_hint)
[constraints]   guardrails        (sensitive, max_depth_remaining)
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

Additional refusals happen at delegation time (see
[Delegate a task — rules](../how-to/delegate-a-task.md#rules-that-will-refuse-your-delegation)):
self-delegation, depth exhausted, missing workspace, non-git workspace with
`commit_policy = "branch"`, budget cap exceeded.

## File loading rules

`load_handoff_file(path)` (used by `maestro delegate --file` and the MCP
`delegate` tool) accepts:

1. **JSON object containing any of the four section keys** (`handoff`,
   `routing`, `expectations`, `constraints`) → parsed as the 4-section document.
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
