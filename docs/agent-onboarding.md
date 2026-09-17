# Onboarding agents without a first-class adapter (generic spec)

Maestro's `generic` adapter kind onboards any CLI from its registry entry — no
Python code. This page carries the verified recipes for the agents that are not
first-class in v1, plus the honest status of each integration path.

Registry entries live under `~/.maestro/agents/<name>.toml`:

```toml
name = "openclaw"
kind = "generic"
display_name = "OpenClaw"
skills = ["autonomous", "messaging"]
# generic-kind fields:
command = "openclaw agent exec --json --message-file -"
input_mode = "stdin"        # prompt piped on stdin (alternative: "{prompt}" in command)
output_format = "jsonl"     # text | jsonl (parse cost_usd/question hints) | rpc
workspace_policy = "cwd"    # cwd | flag
```

Register from the CLI with `maestro agents add <name> --kind generic ...` or by
dropping the TOML file in place. Preflight checks the binary + `--version`
before any delegation, so a wrong entry fails fast instead of silently.

## OpenClaw — verified

Entry point (docs.openclaw.ai/cli/agent): `openclaw agent exec` is the
recommended headless entry point for CI and coding automation. It owns setup,
cleanup, output projection, and process status; runs against the user's normal
OpenClaw config (providers, credentials, runtime harness).

- Prompt via stdin: `cat task.md | openclaw agent exec --message-file - --json`
  (`--message-file -` reads stdin — no ARG_MAX concerns).
- `--cwd <dir>` sets the workspace; Maestro already runs adapters with
  `cwd=<workspace>`, so it can be omitted.
- Exit codes: `0` completed, `1` error/timeout/cancellation (after any result
  is written) — exactly the spawn contract Maestro expects.

Recipe:

```toml
name = "openclaw"
kind = "generic"
display_name = "OpenClaw"
command = "openclaw agent exec --json --message-file -"
input_mode = "stdin"
output_format = "jsonl"
workspace_policy = "cwd"
```

## Kilo Code — IDE-resident (MCP host path)

Kilo Code (Anaconda) is an IDE-resident agent: its automation surfaces are the
Agent Manager control panel, PR code reviews, and MCP. There is no documented
headless one-shot CLI, so it cannot be a *delegation target* via spawn in v1 —
same class as CodeGPT.

The productive direction is reversed: Kilo is an **MCP client**, so add Maestro's
MCP server to Kilo's MCP configuration and Kilo becomes a *host* agent that can
delegate to any registered target (Codex, Claude, Hermes, ...) through the same
`delegate`/`task_wait`/`followup` tools every host gets:

```json
{ "mcpServers": { "maestro": { "command": "python", "args": ["-m", "maestro.mcp_server"] } } }
```

If Kilo ships a headless CLI later, onboard it here as a generic spec (template
below) and promote to first-class if usage justifies it.

## omp — entry point unverified

omp ("a coding agent with the IDE wired in", omp.sh) renders its docs
client-side; no machine-readable reference could be verified at build time.
Before onboarding, run `omp --help` (or the equivalent) on a machine with it
installed and record the headless flags here. Template:

```toml
name = "omp"
kind = "generic"
display_name = "omp"
command = "omp <headless-flags> {prompt}"   # or input_mode = "stdin"
input_mode = "arg"
output_format = "text"
workspace_policy = "cwd"
```

## Verification checklist for any new generic agent

1. `maestro agents status` shows the binary + version (preflight).
2. One trivial delegated task completes end-to-end (`maestro delegate ...`).
3. A failing prompt exits non-zero (so Maestro records failure, not success).
4. Cost/usage: if the CLI emits `cost_usd` or `total_cost_usd` on JSONL lines,
   set `output_format = "jsonl"` and usage is tracked automatically.
