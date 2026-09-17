# Agent collaboration protocol

Codex owns implementation, tests, debugging, refactoring, and follow-up fixes. Claude owns requirements, architecture, final review, and user communication.

When a task has an approved Maestro handoff, use that handoff as the canonical design and inspect only the active target repository. Do not inspect Maestro internals unless Maestro itself is being changed or is failing.

Before implementation:
1. Read the approved handoff from the active Maestro task.
2. Inspect current git state.
3. Implement the approved design.
4. Run tests and record exact evidence.
5. Record important deviations in task artifacts.

## Testing (mandatory)

Every code change ships with tests that confirm it works — no exceptions.

- New behavior, entry points, packaging metadata, and dependency constraints each get at least one test that would fail if the change regressed.
- Prefer behavioral tests (run the real thing: spawn the console script, send a protocol request) over string-matching assertions when the environment allows it.
- Keep the suite hermetic: no network, no dependence on host layout beyond what CI provides (`pip install -e .`). Skip gracefully only when an artifact is genuinely absent from the interpreter under test.
- The coverage gate is 100% (`.coveragerc`); do not add uncovered `maestro/` source lines. Run `coverage run --branch -m pytest -q && coverage report --fail-under=100` before considering a change done.
