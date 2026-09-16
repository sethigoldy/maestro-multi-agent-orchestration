# Maestro orchestration

This repository is designed for a Claude-supervised multi-agent workflow.

Claude Code is the supervisor: it researches, designs, delegates, reviews, and communicates with the user.
Codex is the implementation agent.
Memvara is the durable project memory/state layer.

For non-trivial implementation requests, Claude should use the `maestro` MCP tool automatically. The user should not have to run the CLI.

Lifecycle:

1. inspect existing code + Memvara
2. design/specify
3. delegate to Codex through Maestro
4. verify tests/diff
5. Claude reviews
6. delegate fixes through Maestro when necessary
7. close only after review

The CLI is available for operators and automation, but it is not the normal user interface.


## Maestro workspace rule

When delegating through Maestro, first run `git rev-parse --show-toplevel` and pass the returned absolute path in every Maestro MCP call as `workspace`. Never rely on Maestro MCP's process cwd; Claude sessions and git worktrees can change while the MCP process remains alive.

Maestro/Codex must not commit or install dependencies. Claude owns final review and commits.


## Codex selection

Maestro reads the default Codex model and reasoning effort from tracked `.maestro/config.toml`.
Use the defaults for normal tasks. Override `model` or `effort` in `delegate_to_codex` only when the task clearly warrants it.
Supported effort values: `low`, `medium`, `high`, `xhigh`, `max`.
