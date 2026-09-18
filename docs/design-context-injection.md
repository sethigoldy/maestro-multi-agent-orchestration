# Design Spec: Context Injection — user-controlled context for agent turns

Status: **PROPOSAL — pending approval**
Scope: `maestro/` (new `context` module, handoff, core, daemon, claude_code adapter, CLI) + tests + docs. No migration of existing state.

## 1. Purpose

Give the user a first-class channel to inject their own context into agent turns —
standing instructions ("use this skill", "follow these conventions", "review against
this checklist") that a supervising agent may not think to add. Today the handoff has
`design`, `context_notes` (one unlabelled string) and `context_files` (a list of paths
the agent is merely *told* to read); nothing is structured, attributed, size-managed,
or available to gate turns.

Context injection adds a typed, layered `[context]` channel:

- **Three sources** — user config (`~/.maestro/config.toml`), project config
  (`.maestro/config.toml`), and the handoff itself — composed per label, so standing
  repo context is written once and reaches every task.
- **Typed entries** — `text` (inline instruction), `file` (path; inlined when small,
  artifact-referenced when large), and `skill` (a directory with a `SKILL.md`, the
  [Agent Skills open standard](https://agentskills.io/) — staged for Claude Code via
  real CLI flags, prompt-referenced everywhere else).
- **Phase scoping** — entries can target `implementer`, `verifier`, and/or `reviewer`
  turns (work-mode gates included), so a reviewer can carry its own checklist.
- **Bounded and auditable** — per-entry and total size caps with graceful degradation;
  the composed entry list is recorded on the handoff at delegate time, so every task's
  record shows exactly what context was injected.

Existing `design` / `context_notes` / `context_files` are untouched (backward
compat); docs steer new work to `[context]`.

## 2. Entry model — new module `maestro/context.py`

```python
PHASES = ("implementer", "verifier", "reviewer")   # fix bounces and follow-ups are implementer-phase turns
FILE_INLINE_LIMIT = 8192     # bytes; larger files become artifact references
TOTAL_CONTEXT_LIMIT = 32768  # bytes; rendered block cap

@dataclass(frozen=True)
class ContextEntry:
    label: str
    kind: str            # "text" | "file" | "skill"
    text: str | None     # kind == "text"
    path: str | None     # kind in ("file", "skill")
    phases: tuple[str, ...] = PHASES
    source: str = ""     # "user config" | "project config" | "handoff" (stamped at compose time)
```

`parse_entry(raw: dict, source: str) -> ContextEntry` validates one entry:

- must be a mapping with a non-empty string `label`
- `kind` ∈ {text, file, skill}; default: `"text"` when `text` is present, else `"file"`;
  explicit `kind = "skill"` requires `path`
- exactly one of `text` / `path` is set (text kind → `text`; file/skill → `path`)
- `phases` (when present) is a list of values from `PHASES`; default all three

Errors name the offending label, e.g. `context entry 'style': skill entries need a path`.

## 3. Standing context in config

Tables keyed by label in the existing three-file config chain
(user → project root → worktree; later wins per label — the current `_load_config`
dict merge handles this for free):

```toml
# .maestro/config.toml
[context.style]
text = "This API uses the error shapes in src/api/errors.py. Never add new exception types."

[context.pdf-skill]
kind   = "skill"
path   = "~/skills/pdf-processing"     # directory containing SKILL.md
phases = ["implementer"]               # optional; default: all phases
```

`core._load_config` gains `"context": parse_context_config(merged.get("context"))` →
`dict[label, ContextEntry]` with the source stamped from which file contributed it
(user-level entries are re-stamped when a project entry overrides them). An invalid
entry raises at config load, like `[modes]`.

## 4. Handoff section: `[[context]]`

A fifth top-level section — an array of tables (idiomatic TOML, multiline-friendly):

```toml
[[context]]
label = "spec"
path  = "docs/pagination-spec.md"          # relative → resolved against the workspace; ~ expands

[[context]]
label = "review-checklist"
text  = "Check: no new public API without a test. Check: page_size bounded."
phases = ["reviewer"]
```

- `HandoffDoc` gains `context_entries: list[dict] = []`; `to_dict` always emits
  `"context": [...]`; `from_dict` reads it (absent → `[]`, legacy-safe);
  `load_handoff_file`'s section-key check and the serialization tuple gain `"context"`.
- `validate_handoff` runs every entry through `parse_entry(..., source="handoff")`.

## 5. Composition at delegate time

`MaestroDaemon._apply_context(doc)` — called in `delegate()` after `_apply_work_mode`:

1. base = config entries (already user⊕project merged), in first-seen label order;
2. handoff entries override by label (same label replaces the config entry) and
   append new labels after;
3. every entry gets its final `source` stamped;
4. the composed list **replaces** `doc.context_entries` — the stored/serialized
   handoff is self-contained, so `task audit` shows exactly what was injected (C2).

Skill entries are validated here against the live filesystem: the directory must
exist and contain a `SKILL.md`, else `ValueError` naming the label (fail fast, same
spirit as work-mode agent checks — C4).

## 6. Rendering and staging (per turn)

```python
@dataclass(frozen=True)
class RenderedContext:
    block: str                  # user-prompt CONTEXT block ("" when nothing applies)
    system_file: Path | None    # standing-context file for the claude_code channel
    skill_dirs: list[Path]      # staged skill roots for --add-dir

def render_context(entries, phase: str, workspace: Path, task_dir: Path, adapter_kind: str) -> RenderedContext
```

- **Filter** by `phase` (entry applies when its `phases` include it).
- **Resolve per kind**:
  - `text` → inline verbatim.
  - `file` → expand `~`, resolve relative paths against the workspace; if ≤
    `FILE_INLINE_LIMIT` bytes, inline the content; else copy to
    `task_dir/context-<label>.txt` and emit a path reference line.
  - `skill` → copy the directory (hermetic copy, not symlink — D2) to
    `task_dir/context/skills/<label>/` and emit an availability line:
    `Skill "<label>" is available at <staged path> — read its SKILL.md and follow it.`
- **Cap**: entries render in composed order; once the accumulated block would exceed
  `TOTAL_CONTEXT_LIMIT`, remaining entries are dropped and a trailing note lists the
  dropped labels (C3 — degrade visibly, never error).
- **Channel split** (D1): for `adapter_kind == "claude_code"`, standing-context
  entries (source ≠ "handoff", kinds text/file) render into
  `task_dir/context-system.md` *instead of* the user block (the adapter passes it via
  `--append-system-prompt-file`); handoff entries and all skill availability lines stay
  in the user block. For every other kind, everything renders into the user block.

User-block shape:

```text
CONTEXT (user-provided; follow these along with the request):

[style] (project config)
This API uses the error shapes in src/api/errors.py. …

[spec] (handoff)
<path or inlined content>

Skill "pdf-skill" is available at /…/context/skills/pdf-skill — read its SKILL.md and follow it.
```

## 7. Adapter integration

- `ClaudeCodeAdapter.build_command` consumes a reserved settings key
  `maestro_context = {"system_file": str | None, "skill_dirs": [str]}` (same pattern as
  `model`/`effort` flowing through settings): appends `--append-system-prompt-file <f>`
  when the file exists and one `--add-dir <d>` per staged skill root. All other
  adapters ignore the key — their context arrives entirely in the prompt.
- The daemon injects the settings key only for claude_code turns; the system file is
  written once per task (in `_run_task`, before the first turn) and reused by gate and
  fix turns of that task.

## 8. Daemon flow

- `delegate()`: `validate_handoff` → `_apply_work_mode` → **`_apply_context`**.
- `_run_task`: renders for phase `implementer` with the target's adapter kind;
  `build_prompt(...)` gains a `context_block: str` parameter (rendered text inserted
  after the design block, before EXPECTATIONS); claude_code turns get the
  `maestro_context` settings.
- `_gate_turn`: verifier/reviewer prompts gain the same parameter, rendered for their
  phase — gate agents see only entries scoped to them.
- `_fix_turn`: renders phase `implementer` (a fix bounce is an implementer-phase turn).
- Follow-ups and answer-resumes re-render per turn from the stored composed entries —
  no extra state.

Prompt builders stay pure functions of their arguments; rendering happens in the
daemon, which owns `task_dir` and the adapter kind.

## 9. CLI surface

```text
maestro delegate … --context "text" [--label NAME]        # repeatable (kind text)
                  … --context-file PATH [--label NAME]    # repeatable (kind file)
                  … --skill PATH [--label NAME]           # repeatable (kind skill)
```

- Flags append to the handoff's `[[context]]` entries (file entries first, flags after;
  same label → later wins at composition). Auto-labels: `context-1`, `context-2`, …
- `maestro config` output gains `"context": {label: {kind, text|path, phases}}`.
- MCP path: unchanged signatures — a supervisor simply writes `[[context]]` into the
  handoff file; the `delegate` docstring notes it.

## 10. Invariants and validation

- **C1 — data, not execution.** Maestro stages and references context files; it never
  runs skill scripts or executes context content. Trust level equals config (user-
  managed); docs state this explicitly.
- **C2 — auditable.** The composed entry list is stored on the handoff at delegate
  time; result files and `task audit` show what each turn received.
- **C3 — bounded.** Per-entry inline cap and total block cap with a visible dropped-
  labels note; overflow degrades, never errors.
- **C4 — skills fail fast.** Missing directory or missing `SKILL.md` is a delegate-
  time `ValueError` naming the label.
- **C5 — strict parsing.** Unknown `kind`, bad `phases`, missing/ambiguous text-or-path
  are validation errors (handoff and config alike).
- **C6 — no context, no change.** A task with no entries anywhere produces a
  byte-identical prompt to today's (regression-tested).

## 11. Worked example

```toml
# .maestro/config.toml
[context.style]
text = "Follow docs/STYLE.md; error shapes live in src/api/errors.py."

[context.pdf-skill]
kind = "skill"
path = "~/skills/pdf-processing"
```

```toml
# handoff.toml
[routing]
mode = "economy"

[[context]]
label = "spec"
path  = "docs/pagination-spec.md"

[[context]]
label = "review-checklist"
text  = "No new public API without a test; page_size must be bounded."
phases = ["reviewer"]
```

Economy-mode run: `codex-mini` implements with the style note + spec inlined (or
referenced if large) and the staged pdf skill available; the deterministic gate runs;
`codex-mini` verifies (sees style + spec); `codex-pro` reviews seeing style + spec +
the review checklist. If the spec file is 40KB, it lands at
`~/.maestro/tasks/<id>/context-spec.txt` and the prompt carries the path. Per-phase
cost attribution is unchanged — context adds no attempts.

## 12. Documentation updates

All ship in the same change; reference files pinned by `tests/test_docs_usage.py`
must land together with the code.

| File | Change |
|---|---|
| `README.md` — new *Context injection* section (after *Work modes*) | Concept, entry-kind table, one worked snippet, pointer to the how-to. Keep landing-page brevity |
| `README.md` — *Configuration* | `[context.<label>]` tables: keys, defaults, merge semantics |
| `README.md` — *CLI reference* | `--context` / `--context-file` / `--skill`; `config` lists standing context |
| `docs/usage/reference/cli.md` | The three flags (repeatable, auto-labels), config output shape *(pinned)* |
| `docs/usage/reference/handoff-format.md` | `[[context]]` section: entry fields, kinds, phases, caps, validation rules *(pinned)* |
| `docs/usage/reference/configuration.md` | `[context.<label>]` table + three-file merge per label *(pinned)* |
| `docs/usage/reference/mcp-tools.md` | `delegate` note: handoff files may carry `[[context]]`; signatures unchanged *(pinned)* |
| `docs/usage/how-to/inject-context.md` — **new** | Goal-first guide: write standing context in project config → add per-task entries (flags or file) → stage a skill → verify what the agent received (`task audit`) → phase-scope a reviewer checklist |
| `docs/usage/how-to/delegate-a-task.md` | Short section + pointer to the new how-to |
| `docs/usage/explanation/how-delegation-works.md` | Why layered context (user channel bypassing supervisor judgment), caps, the claude_code system-prompt channel vs prompt fallback |
| `docs/usage/README.md` | Index row for the new how-to *(link resolution pinned)* |
| `docs/architecture-proposal.md` | Addendum: context injection as layered user-controlled context; links to this spec and the how-to |

Intentionally unchanged: tutorials (first-delegation stays minimal), agent-onboarding,
protocol research.

## 13. Test plan (100% branch-coverage gate)

- `tests/test_context.py` — **new**: `parse_entry` validation matrix (parametrized);
  config parsing + source stamping; composition override/ordering; rendering: text
  inline, file inline-vs-artifact at the cap boundary, total-cap drop note, phase
  filtering, skill staging (copy + SKILL.md check), claude_code vs other-kind channel
  split, empty-entries no-op.
- `tests/test_handoff.py` — `[[context]]` TOML/JSON round-trips; absent → `[]`;
  validation errors via `validate_handoff`.
- `tests/test_daemon.py` — e2e with fake agents: a generic fake dumps its stdin prompt
  to a workspace file, asserting the CONTEXT block, labels, sources, and phase scoping
  (reviewer sees the checklist, implementer doesn't); standing config context reaches
  the prompt; claude_code fake dumps argv → asserts `--append-system-prompt-file` +
  `--add-dir` flags and staged `SKILL.md` content; C4 delegate-time skill failures;
  C6 no-context regression (no CONTEXT block in the prompt).
- `tests/test_cli.py` — flag plumbing (repeatable, auto-labels, file+flag merge),
  `config` output shape.
- Docs pinning suite green; full gate:
  `coverage run --branch -m pytest -q && coverage report --fail-under=100`.

## 14. Open decisions (all recommended yes)

| # | Decision | Recommendation |
|---|---|---|
| D1 | claude_code standing-context channel via `--append-system-prompt-file` (system prompt, not user message); other adapters get everything in-prompt | Include — real CLI knob, keeps standing context out of per-task text; one adapter branch + fake-argv test |
| D2 | Skill staging by **copy** into the task dir (not symlink) | Copy — hermetic across worktree cleanup and machines; skills are small |
| D3 | Default `phases` = all three | Yes — standing context should reach gates unless scoped out |
| D4 | Relative paths resolve against the workspace; `~` expands | Yes — matches how tasks see the world; config entries in a worktree resolve inside the checkout |
| D5 | `kind = "plugin"` (Claude Code `--plugin-dir`) | **Out of scope** — same staging pattern + one flag; fast follow-up once skills prove out |
