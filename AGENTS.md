# Agent collaboration protocol

The repository uses self-hosted Memvara as shared durable state.

Before implementing a delegated task:
1. Retrieve the task/design from Memvara using the task ID.
2. Inspect current git state.
3. Implement the approved design.
4. Record important deviations and evidence in Memvara.
5. Run tests and report exact results.

Do not treat a pasted prompt as the canonical design when Memvara contains the approved artifact.
