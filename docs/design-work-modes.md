# Design Spec: Work Modes — per-phase agent presets

Status: **PROPOSAL — pending approval**
Scope: `maestro/` (config, handoff, daemon, CLI, MCP docs) + tests. No migration of existing state.

## 1. Purpose

Let a named config **preset** pin specific agents/models to the model-running phases of a
task cycle, so cost/quality profiles ("economy", "tri-agent", …) are data, not prompt
discipline:

| Preset slot | Phase | Optional? | Default when omitted |
|---|---|---|---|
| `implementer` | IMPLEMENTING | **required** | — (becomes the handoff `target_agent`) |
| `verifier` | VERIFYING | yes | deterministic check only (today's behavior) |
| `reviewer` | REVIEWING | yes | supervisor reviews (today's behavior) |
| `fixer` | FIXING | yes | `implementer` |

Every omission falls back to exactly what is already built. A task with no preset and no new
routing fields behaves byte-for-byte as it does today.

Out of scope (future, same pattern): `designer` slot (daemon-authored design turn),
cross-machine mode propagation, dashboard UI.

## 2. Configuration: `[modes]` presets

Parsed from the existing config chain (`~/.maestro/config.toml`, project `.maestro/config.toml`,
merged with project winning — same semantics as today's `_load_config`).

```toml
[modes.economy]
implementer = "codex-mini"     # required — cheap model, low effort
verifier    = "codex-mini"     # optional LLM verification pass
reviewer    = "codex"          # expensive: verifies requested changes only
fixer       = "codex-mini"     # optional → defaults to implementer
max_bounces = 2                # optional → default 2; 0 = no auto-fix, park on first issue
```

New module `maestro/modes.py`:

- `load_modes(config_text) -> dict[str, ModePreset]` — parse + validate.
- `expand(preset, doc) -> HandoffDoc` — map preset → routing fields (§3).
- Validation at load: `implementer` present; `max_bounces` int ≥ 0; slot values non-empty.
  Agent-name existence is checked **at delegate time** against the live registry (fail fast
  with a hint listing registered agents), because presets are config and registries are state.

## 3. Handoff document: new `[routing]` fields

```toml
[routing]
target_agent = "codex-mini"    # existing (= implementer)
mode         = "economy"       # NEW, optional — preset name; expanded at delegate time
review_agent = "codex"         # NEW, optional
verify_agent = "codex-mini"    # NEW, optional
fix_agent    = "codex-mini"    # NEW, optional (default: target_agent)
max_bounces  = 2               # NEW, optional (default 2)
```

- `HandoffDoc` gains `mode`, `review_agent`, `verify_agent`, `fix_agent`, `max_bounces`
  (defaults `None/None/None/None/2`). Round-tripped through `to_dict` / `from_dict` /
  `to_toml` / `from_toml`; `from_legacy` untouched.
- Precedence: **explicit fields beat the preset** (a handoff may set `mode = "economy"` and
  still override `review_agent`). Documented in the module docstring.
- Expansion happens once, inside `MaestroDaemon.delegate()`, before validation of routing —
  so all existing guards (self-delegation, depth, budgets) see the final agents.

## 4. Daemon execution flow

Today `_post_complete` runs deterministic `_verify()` then marks COMPLETED. New flow after a
successful implementer turn:

1. **Deterministic verify** — unchanged; always runs when `doc.verification != "none"`.
2. **Verifier turn** (only if `verify_agent` set): LLM, read-only. Prompt carries request +
   design + diff + deterministic report. Framing: *"Does it work and does it meet the request?
   You may re-run targeted tests; do not modify any file."* Runs **even when the
   deterministic check failed** — then framed as triage ("is this failure caused by the
   change?"). Findings are appended to `verification.txt` and recorded as a claim.
3. **Reviewer turn** (only if `review_agent` set): LLM, read-only. Prompt carries request +
   design + diff + verification evidence (+ verifier findings). Framing: *"Should we accept
   this code? Quality, design conformance, scope."*
4. **Bounce loop** — any issues from steps 2–3, or a failed deterministic check, trigger:
   - **Fix turn** under `fix_agent` (default: target agent) on the same task branch;
     instruction = issue list + failing output + "build on the existing work".
   - Re-run: deterministic verify → reviewer turn. The verifier does **not** re-run on
     bounces (its findings are already in the issue list; bounds cost).
   - Each cycle consumes one of `max_bounces`. Exhaustion (or `max_bounces = 0`) → **park**:
     `STATE_INPUT_REQUIRED` with a question summarizing unresolved issues + report paths.
     The supervisor then answers, follows up, or cancels — no implicit expensive spend.
5. All green → `STATE_COMPLETED`, metadata extended with verifier/reviewer verdicts.

Mechanics that already work and are reused:

- State during turns stays `STATE_WORKING` (existing `verifying=True` marker; add a
  `reviewing=True` marker). No new A2A states.
- Every turn is an *attempt* recorded via `_record_attempt` with its agent name → per-phase
  cost attribution and budget caps work with zero new accounting.
- Result files reuse the existing `result-{agent}-t{turn}-{attempt}.json` naming.

### Verdict protocol (verifier + reviewer turns)

The prompt requires the agent to end its output with exactly one line:

```
VERDICT: PASS            ← or VERDICT: FAIL
ISSUES:                  ← only on FAIL, one bullet per line
- <issue>
```

Parsing is lenient (case/whitespace tolerant). **Unparseable or missing verdict → park
immediately** (`input-required`, raw output attached) — it does not consume a bounce, because
a formatting failure of the reviewer is not fixable by the implementer.

## 5. Invariants and validation

- **I1 — Deterministic verification can never be overridden by an LLM.** A failed test run
  stays failed; LLM turns can only *add* failures, never flip FAILED → PASSED.
- **I2 — `review_agent == target_agent` is rejected** at delegate time (self-review is
  meaningless; consistent with the existing origin==target guard).
- **I3 — `verify_agent == target_agent` is allowed** (a second pass of the same model on top
  of the independent deterministic gate is a legitimate "add LLM verification" request).
- **I4 — Safe defaults:** fixer → implementer; escalation → park. Nothing spends an
  expensive model's tokens implicitly.
- **I5 — Bounded loops:** `max_bounces` caps all auto-fix cycles per task.
- Delegate-time errors (all `ValueError`, actionable): unknown `mode`; unknown agent name in
  a preset or routing field; non-int / negative `max_bounces`; I2 violation.

## 6. Surface changes

- **CLI:** `maestro delegate --mode NAME` (optional flag; equivalent to `[routing] mode` in
  the file). `maestro config` lists defined modes with their slots.
- **MCP:** `delegate` signature unchanged (the mode lives in the handoff file); docstrings
  updated. No new tools.
- **`followup()`:** when the task's stored doc has `fix_agent`, a supervisor follow-up resumes
  under it instead of hardcoding the original target (`daemon.py:585`). One-line change + tests.
- **Status:** `status_a2a` metadata gains `verify_verdict` / `review_verdict` summaries;
  `maestro task status` and the console render them.

## 7. Worked example — economy mode

```bash
# one-time: register tiers (pure config)
maestro agents add --name codex-mini --kind codex --model gpt-5-mini --effort low
maestro agents add --name codex      --kind codex --model gpt-5        # expensive, default effort
```

```toml
# .maestro/config.toml
[modes.economy]
implementer = "codex-mini"
verifier    = "codex-mini"
reviewer    = "codex"
max_bounces = 2
```

A handoff with `[routing] mode = "economy"` then runs: cheap implements → deterministic gate
→ cheap LLM verifies (triage included) → **expensive reviews the diff of requested changes**
→ bounces go back to cheap, capped at 2 → then parked for the supervisor. Per-phase cost is
visible in `task audit` / budgets because every turn attributes usage to its agent.

## 8. Documentation updates

All of the following ship in the same change as the code. Several reference files are
**pinned by `tests/test_docs_usage.py`** (it asserts the documented commands/fields/config keys
match reality), so they must be updated together or the suite fails.

| File | Change |
|---|---|
| `README.md` — *Configuration → Project config* | Document the `[modes]` table: every key, defaults, and the economy example from §7. |
| `README.md` — new *Work modes* section (after *Deterministic verification*) | One-paragraph concept + the slot/defaults table (§1) + pointer to the how-to guide. Keep landing-page brevity. |
| `README.md` — *Deterministic verification* | Note that presets can add an LLM verifier/reviewer turn, and state invariant I1 (the deterministic gate can never be overridden). |
| `README.md` — *CLI reference* | `maestro delegate --mode NAME`; `maestro config` lists defined modes. |
| `docs/usage/reference/cli.md` | Add the `--mode` option to `delegate` and the mode listing to `config`. *(pinned: `test_cli_reference_pins_all_commands`)* |
| `docs/usage/reference/handoff-format.md` | New `[routing]` fields (`mode`, `review_agent`, `verify_agent`, `fix_agent`, `max_bounces`) with types/defaults, preset-expansion precedence (explicit fields beat preset), and the new validation rules (§5). *(pinned: `test_handoff_reference_pins_fields_and_legacy_mapping`)* |
| `docs/usage/reference/configuration.md` | `[modes]` table: keys, defaults, project-vs-user precedence. *(pinned: `test_configuration_reference_pins_env_vars_and_layout`)* |
| `docs/usage/reference/mcp-tools.md` | Update the `delegate` description to mention that a handoff file may carry `mode`. Signature unchanged. *(pinned: `test_mcp_reference_pins_all_tools_and_states`)* |
| `docs/usage/how-to/delegate-a-task.md` | New section: delegating with a work-mode preset (flag vs `[routing] mode`, overriding one slot per task). |
| `docs/usage/how-to/configure-work-modes.md` — **new** | Task-oriented guide, opens with its goal: register model tiers as agents → define a preset → delegate with it → read per-phase cost in audit/budgets → handle parked tasks (answer/follow-up/cancel). |
| `docs/usage/explanation/how-delegation-works.md` | Extend the lifecycle account: verifier/reviewer turns, verdict protocol, bounce loop and parking. Stays explanation-grade (what/why, no step-by-step). *(pinned: `test_explanation_is_about_not_how_to`)* |
| `docs/usage/README.md` | Index row for the new how-to in the How-to table. *(link resolution pinned by `test_index_links_resolve`)* |
| `docs/architecture-proposal.md` | Short addendum section: work modes as preset-driven per-phase routing; links to this design doc and the how-to. |

Intentionally **unchanged**: `docs/usage/tutorials/first-delegation.md` (stays a minimal
single-line lesson), `docs/agent-onboarding.md` (CLI onboarding is orthogonal — tiers are just
two registrations of the same CLI, covered by the new how-to), and
`docs/multi-agent-protocol-research.md` (research background).

Rule: anything a doc claims about new behavior (`--mode`, verdict lines, park states) must be
backed by a test that exercises that behavior — docs and tests land in the same commit.

## 9. Test plan (100% branch-coverage gate)

Follows existing patterns: fake shell binaries on PATH + registered `AgentSpec`s + the
`daemon` fixture (`tests/test_daemon.py`).

- **`test_modes.py` (new):** preset parse/validate/expand — missing `implementer`, unknown
  agent at expansion, bad `max_bounces`, explicit-field-over-preset precedence, project-vs-user
  config merge.
- **`test_handoff.py`:** new fields round-trip (dict/TOML both directions); legacy and
  field-less docs parse unchanged; validation messages.
- **`test_daemon.py`:**
  - economy end-to-end: fake cheap implementer + fake expensive reviewer emitting
    `VERDICT: PASS` → completed; usage attributed to both agents; review metadata present.
  - reviewer FAIL with issues → fix turn under `fix_agent` → re-verify → reviewer PASS →
    completed; attempts list shows all turns/agents in order.
  - bounce exhaustion → parked `input-required`; question contains the unresolved issues.
  - `max_bounces = 0` → parks on first issue with no fix turn.
  - verifier FAIL adds issues to the same bounce counter (combined with reviewer issues).
  - deterministic FAILED + LLM verdict PASS → final verification still FAILED (I1).
  - unparseable verdict → immediate park, raw output attached, no bounce consumed.
  - `review_agent == target_agent` rejected; unknown mode/agent rejected with hints.
  - default path regression: handoff with no new fields → identical behavior to today
    (no verifier/reviewer turns, COMPLETED with verification marker as before).
- **`test_cli.py`:** `--mode` flag expansion; `config` lists modes.
- **`test_mcp.py`:** delegate docstring/behavior with a mode-bearing handoff file.

- **`test_docs_usage.py`:** passes with the updated pinned references (§8); new how-to is
  indexed and its links resolve; explanation/how-to tone pins still hold.
- Gate: `coverage run --branch -m pytest -q && coverage report --fail-under=100`.

## 10. Open decisions (default = my recommendation)

1. Verifier runs on deterministic failure too (triage framing) — **yes** (recommended).
2. Unparseable verdict → immediate park, no bounce consumed — **yes** (recommended).
3. `followup()` honors `fix_agent` in the MVP — **yes** (one line + tests; deferring it makes
   mode semantics inconsistent between internal and supervisor bounces).
4. Preset name collision across config layers → project-level wins (existing merge rule).
