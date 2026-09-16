# Quickstart

1. Install Python 3.11+ and `memvara`.
2. Install/authenticate `claude` and `codex`.
3. `pip install -e .`
4. `aoa list-adapters`
5. `maestro handoff --title "Feature X" --request "Implement Feature X" --design-file .maestro/design.md`
6. `aoa run <TASK_ID>`
7. `aoa status <TASK_ID>`

For a shared agent workflow, configure Claude Code to use Maestro MCP and both agents to use the same self-hosted Memvara project store. Configure Codex model/effort in `.maestro/config.toml`; Claude normally leaves those fields unspecified and Maestro applies the defaults.
