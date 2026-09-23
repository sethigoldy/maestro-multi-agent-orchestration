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
- A web page open in your browser can also reach `127.0.0.1`, so a loopback
  daemon refuses requests that a browser page could send:
  - A request whose `Host` header is not `127.0.0.1`, `localhost` or `::1` is
    refused with 403. This stops a DNS-rebinding page, whose own domain has
    been pointed at 127.0.0.1, from reading tasks or live agent output.
  - A POST must have `Content-Type: application/json`. A browser can only send
    that cross-site after asking the server first (a CORS preflight), and the
    daemon never agrees, so a page cannot submit work to it.
  - A POST that carries an `Origin` header is refused with 403 unless it
    comes from exactly this daemon's own address: the host and port in the
    `Origin` must be the same as the request's `Host` header. This is an
    exact-origin rule, not a same-site rule, so another port on the same
    machine counts as a different origin. A POST with no `Origin` header comes
    from a program rather than a web page (the CLI, the MCP server, `curl`)
    and is not checked.
  - Behind a reverse proxy that changes the `Host` header to the daemon's own
    address, the page's `Origin` (the proxy's public address) no longer
    matches, so browser POSTs through the proxy are refused. List the proxy's
    public origin with `maestro-daemon --allow-origin https://maestro.example.com`
    (repeat the option for more than one) or in the
    `MAESTRO_DAEMON_ALLOWED_ORIGINS` environment variable, as a comma-separated
    list. The environment variable also covers a daemon started by
    `maestro daemon start` or by the MCP server. Each entry must be written
    the way a browser sends it, `scheme://host[:port]`; an invalid entry stops
    the daemon from starting. A loopback daemon still checks the `Host`
    header, so the proxy must send `Host: 127.0.0.1:<port>` (or `localhost`),
    which is what a proxy that rewrites `Host` to its upstream address does.
- Binding a non-loopback address (e.g. `--host 0.0.0.0` or an explicit IP)
  **always requires a bearer token**: either `MAESTRO_DAEMON_TOKEN` (stable
  across restarts) or an auto-generated one written to the local
  `$MAESTRO_HOME/daemon.json` marker. The token is compared in constant time.
  The API is not designed for public exposure; put it behind your own
  authentication if you must.
- Files that can hold a token are readable by their owner only (mode 0600):
  the `daemon.json` marker and each agent registry entry under
  `$MAESTRO_HOME/agents/`. Maestro never rewrites these files in place. It
  writes a new file with mode 0600 and moves it over the old one, so a
  process that opened an older, world-readable copy cannot read the new
  token through that open handle. Registry entries written by older versions
  with a wider mode are set to 0600 whenever the registry is loaded; an entry
  that the current user cannot change (for example one owned by another
  user) is left as it is. `maestro agents list`, `maestro agents add` and the
  MCP `agents_list` tool show a stored token as `<redacted>`.
- The web console served by the daemon respects the same token: loopback needs
  none, remote access does.
- The agent card (`/.well-known/agent.json`) names the daemon's pid and state
  directory, so `maestro daemon status` and `stop` can confirm that a marker
  written by an older version belongs to this daemon. The card is served under
  the same rules as every other data endpoint: with the token beyond loopback,
  and only to loopback host names on a loopback daemon.
- The HTTP API limits what one client can make it hold. A POST whose
  `Content-Length` is negative or not a number is refused with 400, and one
  larger than 8 MiB is refused with 413; neither body is parsed or kept in
  memory. After the reply the daemon reads and discards the body, at most
  64 MiB of it and only while the client keeps sending (it gives up after
  5 seconds of silence), so the client receives the reply instead of a reset
  connection. A client of an
  event stream (SSE) that stops reading is disconnected once it is 2048 events
  behind or a write has waited 30 seconds, so it cannot make the daemon queue
  events forever. A JSON-RPC request that makes the daemon fail unexpectedly
  gets a `-32603` error reply instead of a dropped connection.
- P2P discovery (UDP multicast) is off for a daemon that listens on loopback
  only, unless `MAESTRO_DISCOVERY=1` is set. Announcements are untrusted input:
  the daemon reads only packets sent to the multicast group, ignores other
  hosts when `MAESTRO_DISCOVERY_IF` is loopback, drops an announcement whose
  advertised host is not an IP address, drops an announcement from another
  host that advertises a loopback address (such as `127.0.0.1`) or a
  link-local address other than the one it was sent from (such as the cloud
  metadata address `169.254.169.254`), strips control characters
  from names, and keeps at most 256 peers in `peers.json`. A daemon that
  listens on loopback only never announces `127.0.0.1` on a network
  interface. Discovered peers are only a
  list; nothing connects to them unless you register one as an agent.

### State and receipts

Durable state under `~/.maestro` (`$MAESTRO_HOME`) contains task titles,
requests, agent output, and costs — potentially sensitive project content. It is
plain files with default umask; protect the directory like any other workspace
data (and note that `maestro doctor` output includes paths but no task content).

## Security-relevant behavior we commit to

- Loopback-only binding by default; token required for any non-loopback bind.
- A loopback daemon refuses foreign `Host` headers, non-JSON POSTs and
  POSTs from any browser origin other than its own address or an origin you
  listed with `--allow-origin` or `MAESTRO_DAEMON_ALLOWED_ORIGINS`, so a web
  page cannot drive it.
- `maestro daemon stop` signals only a process it has confirmed to be the
  daemon, never a process that reused a crashed daemon's pid.
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
