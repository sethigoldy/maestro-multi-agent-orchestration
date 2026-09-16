# Agent collaboration protocol

The repository uses the local `.maestro/` filesystem as shared durable state.

Before implementing a delegated task:
1. Retrieve the task/design from the active worktree `.maestro/` state using the task ID.
2. Inspect current git state.
3. Implement the approved design.
4. Record important deviations and evidence in the task artifacts and `.maestro/state.jsonl`.
5. Run tests and report exact results.

Do not treat a pasted prompt as the canonical design when the active `.maestro/` state contains the approved artifact.
