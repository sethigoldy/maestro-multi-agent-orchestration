# Contributing to Maestro

Thanks for helping make local-first multi-agent orchestration trustworthy. This
document covers the development setup, the test bar, and the design principles
that keep Maestro maintainable.

## Development setup

Python 3.11+ (the CI matrix is 3.11 / 3.12 / 3.13) and git. Node is only needed
to rebuild the web console bundle.

```sh
git clone https://github.com/sethigoldy/maestro-multi-agent-orchestration maestro
cd maestro
python3.11 -m venv .venv
.venv/bin/python -m pip install -e . pytest coverage build
```

Verify the environment before and after your change:

```sh
.venv/bin/python -m pytest -q                          # full suite (hermetic, no network)
.venv/bin/python -m coverage run --branch -m pytest -q && \
  .venv/bin/python -m coverage report --fail-under=100 # the coverage gate
.venv/bin/python -m build                             # wheel + sdist
scripts/smoke-fake-agent.sh                           # end-to-end fake-agent smoke test
scripts/validate-package.sh                           # clean-install verification (wheel + sdist)
```

### Rebuilding the web console

The checked-in bundle is `maestro/web_dist/` (`console.js` + `index.html`). It is
built by `web/build.mjs` with a pinned esbuild version, and the build is
byte-reproducible from the lockfile:

```sh
cd web && npm ci && node build.mjs
git diff -- maestro/web_dist/   # must be empty for a rebuild of unchanged sources
```

If you touch `web/src`, rebuild in the same commit. CI enforces this with a
rebuild-and-diff job, and `tests/test_console_js.py` carries a marker check that
catches stale bundles even where node is unavailable.

## The test bar

The suite is hermetic by design: no network, no real agent CLIs, no dependence on
host layout beyond `pip install -e .`. Tests that need "an agent" use small fake
binaries on a temp PATH (see `tests/test_receipt.py::_run_fake_task` and
`scripts/smoke-fake-agent.sh`). Every change ships with tests that fail if the
change regresses — new behavior, entry points, packaging metadata, and dependency
constraints each get at least one such test.

The coverage gate is **100% branch coverage** over `maestro/` (`.coveragerc`,
enforced by `coverage report --fail-under=100` in CI). Do not add uncovered
source lines; if a line is genuinely unreachable, say so in the PR and we will
discuss an explicit exclusion — exclusions are reviewed like code.

## Design principles (please preserve them)

1. **Tasks are the durable source of truth.** State lives under `~/.maestro`
   (`$MAESTRO_HOME`) as plain files: registry, per-task claims, runtime JSON.
   The daemon is a cache and an executor; killing it must never lose or corrupt
   task state.
2. **Receipts are projections, not a second source of truth.**
   `maestro/receipt.py` renders whatever the durable state says; it writes
   nothing new. If a fact is not in the receipt, it is not in the durable state —
   do not add receipt-only storage.
3. **Adapters are backends.** Each agent (Codex, Claude Code, Cursor, generic
   commands, …) is an adapter behind one interface (`maestro/adapters/`). Nothing
   outside `adapters/` may know about a specific CLI's flags or output format.
4. **No coupling to one agent.** Routing fields (`target_agent`, `verify_agent`,
   work-mode presets) name registered agents; built-in CLIs are just the default
   registry entries. A Maestro install with only generic agents must work.
5. **The deterministic check is final.** Work-mode gate verdicts (LLM verify /
   review passes) can add failures but never override the deterministic
   verification result (invariant I1). Keep it that way.
6. **No real agent CLIs in unit tests or CI.** Real-agent validation is a manual
   procedure (`docs/release/real-agent-validation.md`), deliberately outside the
   automated gate.

## Pull request requirements

- Branch off `main`; keep PRs scoped (one concern per PR where practical).
- All checks green: matrix tests + 100% coverage, build, clean-install
  validation, smoke test, web rebuild diff.
- Changelog entry under the unreleased version in `CHANGELOG.md`.
- Docs updated for any user-visible behavior change (README, `docs/usage/`, or
  the relevant design doc).
- No new dependencies without a stated reason and an owner; dependency
  constraints are tested (`tests/test_packaging.py`).

## Reporting bugs

Use GitHub issues. A good report includes: `maestro --version` output,
`maestro doctor` output (it is read-only and safe to share — it contains no
task content), the command you ran, and what you expected versus what happened.
