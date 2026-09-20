# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a suspected security vulnerability.
Report it through the repository's **Security → Advisories → New draft report**
(GitHub Security Advisories) on
[sethigoldy/maestro-multi-agent-orchestration](https://github.com/sethigoldy/maestro-multi-agent-orchestration).

A useful report includes: the affected version (`maestro --version`), a minimal
reproduction, and the impact you observed. We will acknowledge receipt promptly
and coordinate on a fix before any public disclosure.

Maestro is a local-first developer tool; there is no hosted service, no cloud
account, and no telemetry to compromise remotely. The realistic threat surface is
the machine running Maestro, which this policy describes honestly.

## Threat model

Maestro is an **orchestration broker that executes external processes on your
machine with your privileges**. It is not a sandbox: it deliberately hands real
workspaces and real agent CLIs to real tasks. Consequences to understand:

- **It runs the agent commands you configure.** Built-in adapters shell out to
  installed CLIs (Codex, Claude Code, Cursor, …); `kind = "generic"` agents run
  whatever command you registered — with your environment and permissions. A
  malicious or compromised agent process can do anything your user account can.
- **It operates on the workspaces you delegate to.** Agents receive the task's
  workspace path and may read, write, and commit there (commit behavior is
  governed by `commit_policy`). Delegate only to workspaces an agent is allowed
  to touch.
- **Agents inherit your environment.** Spawned processes see the daemon's
  environment variables, including any credentials your shell carries (API keys,
  git credentials, cloud tokens). Keep secrets out of ambient environments where
  possible; Maestro does not scrub or filter them.
- **Agent CLIs may access their own credentials.** For example, a Codex or
  Claude Code CLI authenticates with its own account/keys and acts accordingly.
  Running Maestro means those CLIs can be invoked programmatically.
- **Handoff documents are prompts.** `title`, `request`, `design`, context files,
  and skill contents are passed to agents as instructions. Treat untrusted
  handoff text as untrusted input to a code-executing process (prompt injection
  is the agent's problem domain, not Maestro's — but know it exists).

### Daemon network exposure

- The daemon **binds to loopback (`127.0.0.1`) by default** and requires no
  token on loopback. Nothing listens on your LAN unless you ask for it.
- Binding a non-loopback address (e.g. `--host 0.0.0.0` or an explicit IP)
  **always requires a bearer token**: either `MAESTRO_DAEMON_TOKEN` (stable
  across restarts) or an auto-generated one written to the local
  `$MAESTRO_HOME/daemon.json` marker. The API is not designed for public
  exposure; put it behind your own authentication if you must.
- The web console served by the daemon respects the same token: loopback needs
  none, remote access does.

### State and receipts

Durable state under `~/.maestro` (`$MAESTRO_HOME`) contains task titles,
requests, agent output, and costs — potentially sensitive project content. It is
plain files with default umask; protect the directory like any other workspace
data (and note that `maestro doctor` output includes paths but no task content).

## Security-relevant behavior we commit to

- Loopback-only binding by default; token required for any non-loopback bind.
- Read-only diagnostics: `maestro doctor` never mutates state or runs agents'
  work (its agent probes run version checks only).
- No network calls from the core library beyond what you configure (agent CLIs,
  optional remote daemons via `MAESTRO_DAEMON_URL`, generic API-mode agents you
  register).
- Dependency constraints are tested (`tests/test_packaging.py`); new dependencies
  go through review in pull requests.

## Scope notes

Things that are **not** Maestro vulnerabilities: an agent doing what its own
account/permissions allow; a user registering a malicious generic command for
their own use; prompt-injection outcomes inside an agent's own tooling. If you
are unsure, report it anyway — we would rather triage a duplicate than miss one.
