# Quickstart

1. Install Python 3.11+.
2. Install/authenticate `claude` and `codex`.
3. `pip install -e .`
4. Configure Claude Code to use the Maestro MCP server.
5. Keep Codex defaults in `.maestro/config.toml`; filesystem state is stored under `~/.maestro/`.
6. Claude delegates implementation through Maestro/Codex; use `maestro task list` to inspect tasks.
