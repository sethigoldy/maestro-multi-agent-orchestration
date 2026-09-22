# Design: Task Continuation & Context Reuse

Status: implemented (v1). This document records the architecture audit that
preceded the implementation, the design decisions, and the explicit
limitations of the first release.

## 1. Architecture audit

### Verified observations

- **V1 — `followup()` exists.** `MaestroDaemon.followup(task_id, instruction)`
  (`maestro/daemon.py`) plus an MCP tool `followup(workspace, task_id,
  instruction)` (`maestro/mcp_server.py`). There is **no CLI follow-up command**
  (the global skill says the same: "the CLI has no followup command").
- **V2 — `followup()` reuses the same task ID.** It mutates the existing record
  in place (`record["doc"]` is replaced by the follow-up handoff; the record,
  its number, and its registry entry are unchanged).
- **V3 — Same workspace and task branch.** The workspace comes from the record;
  `_prepare_branch()` re-checks out `maestro/<task-id>`, which is stable because
  the task ID is stable.
- **V4 — Routing/work-mode reuse.** The follow-up handoff carries over
  `mode`, `review_agent`, `verify_agent`, `fix_agent`, `max_bounces`, fallback,
  artifacts, verification mode, commit policy, budget hint, and sensitivity;
  the target is the pinned fixer (a follow-up is a fix) or the original target.
  `max_depth_remaining` is decremented per follow-up (nesting guard).
- **V5 — Durable task state.** Every state change persists a `task_runtime`
  claim (JSON snapshot of the record, minus the transcript) plus individual
  claims (`task_status`, `task_workspace`, `task_branch`, `task_request`,
  `task_title`, `task_verification`, `task_gates`, ...). The registry index is
  durable. `status_a2a()`, `wait()`, and `list_tasks()` all have durable
  fallbacks for tasks from earlier daemon runs.
- **V6 — Raw execution history is preserved.** The claim journal is
  append-only (every `_write_claim` appends; `history()` returns all entries).
  Per-turn artifacts live under `<state>/tasks/<task-id>/`: `result-<agent>-t<turn>-<attempt>.json`,
  agent logs (`<kind>-<task-id>.log`), and `verification.txt`. Nothing is
  deleted by follow-ups.

### Corrected assumptions

- **C1 — Follow-ups do NOT replay raw history today.** The prompt for a
  follow-up turn contains: the new instruction, the inherited design/context
  files/notes, and the parked-question Q&A transcript (input-required parking
  only). It does **not** contain prior agent output. So the "bad" pattern of
  re-sending 20K tokens of turn-1 output on every turn is not present in
  Maestro. The real inefficiency is the opposite: a follow-up agent receives
  almost no accumulated state (what was done, which files changed, what
  verification found) and must re-discover it by exploring the repository.
  Task Knowledge therefore *adds* a compact state snapshot; it does not remove
  a history-replay problem. The context-reduction measurement (§7 of the spec)
  is measured against the honest baseline: the full durable per-task artifact
  trail (result files + logs + verification report) versus the rendered
  continuation block.
- **C2 — `followup()` does not survive a daemon restart.** It requires the
  in-memory record (`self._tasks.get(task_id)` → `KeyError` otherwise). This
  feature adds durable reconstruction so continuation works after restart.
- **C3 — Follow-up turns currently lose composed context entries.** The
  follow-up handoff copies `context_files`/`context_notes` but not
  `context_entries`, so user-injected `[[context]]` entries are dropped from
  the prompt on follow-up turns. This feature carries them over (and uses the
  same channel for the knowledge entry).
- **C4 — Memvara is a storage backend, not semantic memory.** It implements the
  same claim interface as the filesystem JSONL journal
  (`remember`/`history`/`get_all`) behind `[storage] backend = "memvara"`.
  Task Knowledge therefore lives in the existing claim journal (one new
  predicate, `task_knowledge`) and works identically on both backends. No
  Memvara-specific code is added; optional long-term semantic retrieval is
  explicitly deferred.

### Current architecture (context path per turn)

1. `delegate()` composes standing `[context]` config entries with the
   handoff's `[[context]]` entries once (`compose_context`) and stores the
   merged list on the record's doc (invariant C2: the stored task record shows
   exactly what was injected).
2. Each turn, `_rendered_context()` renders the entries for the phase via
   `render_context()` → a user-prompt CONTEXT block (plus, for claude_code, a
   system-file channel and staged skills). Caps: 8 KiB per inlined file,
   32 KiB total rendered block; overflow degrades to visible drop notes.
3. `build_prompt()` assembles the implementation prompt: title, request,
   design, rendered context block, context notes/files, expectations, and the
   parked-question Q&A transcript. Gate turns (verifier/reviewer/fixer) use
   `_gate_context()` plus the verification report excerpt and a live git diff
   excerpt.
4. The prompt is handed to an adapter as a plain string; **every adapter
   spawns a fresh process per turn** (spawn mode), or a fresh RPC/API session.
   No adapter supports native persistent sessions or resume-by-session-id
   today (copilot/cursor report session IDs but they are not used for
   resumption). The prompt-string contract is the seam where continuation
   context is injected without leaking task-model concerns into adapters.

## 2. Design decisions

### Task Knowledge representation (`maestro/knowledge.py`)

A versioned, deterministic dataclass:

```python
TaskKnowledge(
    schema_version=1, task_id, goal, constraints[], decisions[], assumptions[],
    files_changed[], current_state, verification{status, command, failures[]},
    known_issues[], latest_summary, last_updated, source_turn)
```

- **Deterministic serialization**: fixed field order; the persisted claim is
  `json.dumps(..., sort_keys=True, ensure_ascii=False)` — same inputs give the
  same bytes.
- **Versioned + compatible**: `from_dict` tolerates missing optional fields
  (older knowledge) and ignores unknown fields (future versions); any
  `schema_version >= 1` is accepted.
- **Single source of truth**: persisted as one `task_knowledge` claim in the
  existing claim journal (filesystem or memvara). It is a *projection* —
  re-derived from durable state at every terminal transition and again when a
  continuation starts; it never replaces claims, receipts, or artifacts.

### Knowledge projection (deterministic, no LLM)

Extracted only from durable state:

| Field | Source |
|---|---|
| `goal` | original `task_request` claim + title (written once at registration) |
| `constraints` | original handoff expectations/constraints (verification mode, commit policy, sensitivity, depth) |
| `decisions`, `assumptions` | **reserved** — not deterministically extractable in v1; documented limitation, empty lists |
| `files_changed` | recomputed from live git state at projection time (`git diff --name-only HEAD` + untracked); empty when the workspace is absent or not a git repo |
| `current_state` | state + branch + turn count + last agent |
| `verification` | `task_verification` claim (PASSED/FAILED + command from the report) plus deterministic failure lines parsed from `verification.txt` |
| `known_issues` | gate verdict issues (`task_gates` claim) + failed-attempt errors (runtime attempts) |
| `latest_summary` | bounded tail of the last attempt's output log, or its error text |

### Continuation context builder

`render_continuation_block(knowledge, budget_chars)` renders sections in
value order: goal → current state → verification (+ failures) → known issues →
constraints → latest summary. The budget (config `[continuation] max_tokens`,
default 6000; converted deterministically at 4 chars/token — a labeled
estimate, never an exact tokenizer count) is enforced by dropping
lowest-value sections first and truncating the last kept section with a visible
marker. **The new instruction is never part of this block** (it is the
handoff's `request`, rendered separately), so it can never be truncated.

### Integration with `followup()`

- The knowledge block is injected as a typed text context entry labeled
  `task-knowledge` on the follow-up handoff — reusing the existing
  `render_context()` pipeline (phase filtering, caps, drop notes) and keeping
  invariant C2 (the stored record shows exactly what was injected).
- Existing composed `context_entries` are carried over to the follow-up doc
  (fixes corrected assumption C3).
- `context_mode="fresh"` skips the knowledge entry entirely: same task,
  workspace, branch, and routing — a new reasoning context only.
- **Restart-safe**: when the in-memory record is absent, `followup()`
  reconstructs it from durable claims (corrected assumption C2).
- **Instrumentation**: each reuse-mode continuation records honest sizes on
  the task record (`context_stats`): knowledge chars, rendered block chars,
  raw per-task artifact trail bytes, estimated tokens (chars/4, labeled), and
  `reduction_ratio = 1 - context/raw`. All values are measured, none derived
  from a model.

### CLI / MCP / wire

- CLI: `maestro task continue <task-id> --request "..." [--context
  reuse|fresh] [--no-wait]` — same conventions as `delegate` (daemon endpoint,
  SSE streaming until terminal state).
- Wire: new JSON-RPC method `tasks/followup` on the A2A dispatcher (additive;
  existing methods unchanged).
- MCP: the **existing** `followup` tool gains an optional `context_mode`
  parameter (default `reuse`) — no duplicate API.

### Receipts

`build_receipt()` gains additive fields only: `turns`, `knowledge`
(schema/source turn/updated), and `context` (the instrumentation block when a
continuation ran). Existing fields are untouched; old receipts render
unchanged.

## 3. Explicitly deferred (v1)

- Native adapter session reuse (documented above; the prompt-string contract
  is the seam for it later).
- LLM-assisted knowledge refinement (decisions/assumptions extraction).
- Memvara semantic retrieval / cross-task memory.
- Automatic task merging, vector search, telemetry.
