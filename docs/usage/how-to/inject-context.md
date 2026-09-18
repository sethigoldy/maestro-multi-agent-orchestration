# How to inject context into tasks

This guide shows you how to use context injection — the first-class channel for
putting your own context (instructions, files, skills) into agent turns, whether
it should stand behind every task in a project or apply to one specific handoff.
By the end you will have standing repo conventions and a reusable skill reaching
every implementation turn, plus a reviewer-only checklist that only the review
gate sees.

For the design rationale (why context is data Maestro stages but never executes,
and why Claude Code gets its standing context in the system prompt), see
[How delegation works](../explanation/how-delegation-works.md#context-injection).
Field-by-field reference: [handoff format](../reference/handoff-format.md) and
[configuration](../reference/configuration.md#context--standing-context-entries).

## Prerequisites

A running daemon (`maestro-daemon`) and at least one registered agent. For the
skill example, an [Agent Skills](https://agentskills.io/) directory — a folder
containing a `SKILL.md` (YAML frontmatter with `name` and `description`, then
the instructions).

## Step 1: Write standing context in your project config

Standing entries live in `[context.<label>]` tables in the existing config chain
— `~/.maestro/config.toml` for you, or `<project>/.maestro/config.toml` for a
project (project wins per label). Each entry is one labeled unit:

```toml
# .maestro/config.toml
[context.style]
text = "Follow docs/STYLE.md; error shapes live in src/api/errors.py. Never add new exception types."

[context.pdf-skill]
kind   = "skill"                 # text (default) | file | skill
path   = "~/skills/pdf-processing"  # directory containing SKILL.md
phases = ["implementer"]         # optional — default: all phases
```

- `text` entries inline the instruction verbatim.
- `file` entries (a `path`, no `kind`) inline the file when it is ≤8KB,
  otherwise copy it to the task directory and hand the agent a path reference.
- `skill` entries stage a hermetic copy of the skill directory into the task;
  Claude Code targets discover it via `--add-dir`, every other agent receives a
  "read its SKILL.md and follow it" line in the prompt.

Verify what Maestro sees:

```bash
maestro config --project /path/to/repo
# → { …, "context": {"style": {…}, "pdf-skill": {…}}}
```

## Step 2: Add per-task context to a handoff

Per-task entries go in the handoff's `[[context]]` array of tables (TOML) or
`"context"` list (JSON), or on the CLI for flag-form delegations:

```toml
# handoff.toml
[[context]]
label = "spec"
path  = "docs/pagination-spec.md"     # relative paths resolve against the workspace

[[context]]
label = "review-checklist"
text  = "No new public API without a test; page_size must be bounded."
phases = ["reviewer"]                 # only the review gate sees this
```

```bash
maestro delegate --title "…" --request "…" --target codex \
  --context "Prefer small commits" \
  --context-file docs/pagination-spec.md \
  --skill ~/skills/pdf-processing
```

CLI labels are derived automatically: `context-1`, … for `--context` text, the
file stem for `--context-file`, and the directory name for `--skill`. A handoff
entry with the same label as a standing entry overrides it for that task.

## Step 3: Verify what the agent received

The composed entry list (standing + per-task, with sources stamped) is stored on
the task record — no hidden prompt assembly:

```bash
maestro task audit <task-id>
# → "doc": { …, "context": [ {"label": "style", "source": "project config", …}, … ] }
```

For Claude Code targets you can also inspect the staged artifacts under
`~/.maestro/tasks/<task-id>/`: `context-system.md` (standing text/file entries,
passed via `--append-system-prompt-file`) and `context/skills/.claude/skills/…`
(staged skills).

## Notes

- **Caps:** 8KB per inlined file, 32KB total rendered block. Overflow degrades
  to artifact references or a trailing note listing dropped labels — it never
  fails the task.
- **Phases:** `implementer`, `verifier`, `reviewer` (default: all). Fix bounces
  and follow-ups are implementer-phase turns, so implementer-scoped entries reach
  them too.
- **Trust:** context is data — Maestro stages and references files but never
  executes context content. Treat a skill like installing software: only point
  entries at directories you manage.
- The legacy `design`, `context_notes`, and `context_files` handoff fields are
  unchanged; use `[[context]]` for new work.
