# Maestro usage documentation

This section is the in-depth documentation for *using* Maestro. The top-level
[README](../../README.md) remains the landing page (quickstart, concepts,
troubleshooting); this section goes deeper, organized by what you need at the
moment you open a document:

- **Learning it** → [Tutorials](tutorials/) — guided, step-by-step, with
  expected output at every step.
- **Doing a specific job** → [How-to guides](how-to/) — task-oriented
  directions for someone who already knows the basics.
- **Looking something up** → [Reference](reference/) — the complete, exact
  description of commands, tools, formats, and configuration.
- **Understanding the design** → [Explanation](explanation/) — how and why
  Maestro works the way it does.

## Tutorials

| Document | You will… |
|---|---|
| [Your first delegation](tutorials/first-delegation.md) | install Maestro, start the daemon, delegate a real task to an agent, watch it run, review the result on its branch |

## How-to guides

| Document | Goal it serves |
|---|---|
| [Register and configure agents](how-to/register-an-agent.md) | make any CLI, REST server, or remote daemon available as a delegation target |
| [Delegate a task](how-to/delegate-a-task.md) | author a handoff (flags or file), pick target/fallback/settings, launch it |
| [Manage in-flight tasks](how-to/manage-in-flight-tasks.md) | answer agent questions, send follow-ups, cancel work, block on completion |
| [Inspect tasks and artifacts](how-to/inspect-tasks-and-artifacts.md) | read status, audits, live streams, dashboards; find branches, results, verification reports; clean up old tasks |
| [Run Maestro across machines](how-to/run-cross-machine.md) | expose a daemon to the network with token auth and delegate to it from another machine |

## Reference

| Document | What it describes |
|---|---|
| [CLI reference](reference/cli.md) | every `maestro` / `maestro-daemon` subcommand, option, and exit code |
| [MCP tools reference](reference/mcp-tools.md) | the eight MCP tools: exact signatures, parameters, return shapes |
| [Handoff format reference](reference/handoff-format.md) | the 4-section handoff document, every field, validation rules, legacy format |
| [Configuration reference](reference/configuration.md) | config files and precedence, environment variables, state directory layout |

## Explanation

| Document | What it covers |
|---|---|
| [How delegation works](explanation/how-delegation-works.md) | the daemon architecture, task lifecycle states, role-agnostic routing, durable state, verification, retries and budgets |

## Related documents outside this section

- [docs/architecture-proposal.md](../architecture-proposal.md) — the design
  proposal and milestone history (research-grade background).
- [docs/agent-onboarding.md](../agent-onboarding.md) — recipes and a checklist
  for onboarding a new agent CLI.
- [docs/multi-agent-protocol-research.md](../multi-agent-protocol-research.md) —
  protocol research behind the A2A-based design.
