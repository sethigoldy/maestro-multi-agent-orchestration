# How to run Maestro across machines

This guide shows you how to expose a daemon to the network with token
authentication and delegate work to it from another machine — either through
the CLI, as a registered `a2a_remote` agent, or via peer discovery.

## Step 1 — Bind the daemon to the network

By default a daemon listens on loopback only (`127.0.0.1`) and needs no
token. To make it reachable from other machines:

```bash
maestro-daemon --bind 0.0.0.0        # all interfaces
```

On startup it prints JSON that includes the two values you need:

```json
{
  "pid": 51234,
  "port": 8790,
  "bind": "0.0.0.0",
  "advertised_host": "10.0.0.5",
  "state_dir": "/home/you/.maestro",
  "token": "9f2c…generated…"
}
```

- `advertised_host` is the LAN IP other machines should dial (for an explicit
  `--bind <ip>`, that IP is used as-is and also for local CLI calls).
- Any non-loopback bind **enables token authentication automatically**. A
  token is generated unless you set `MAESTRO_DAEMON_TOKEN` yourself — set it to
  keep a stable token across restarts.

The token is also written into the local `daemon.json` marker, so your own
machine's CLI keeps working without extra configuration. The marker is readable
by your user only (mode 0600).

## Step 2 — Verify reachability

From the other machine, confirm the endpoint answers:

```bash
curl -H "Authorization: Bearer <token>" http://10.0.0.5:8790/agents
```

A wrong or missing token gets `HTTP 401`. The static web console files are
public (the app must load), but every data endpoint — task list, event streams,
JSON-RPC mutations — requires the token.

## Step 3a — Delegate from the CLI on the other machine

Point the CLI at the remote daemon with environment variables:

```bash
export MAESTRO_DAEMON_URL=http://10.0.0.5:8790
export MAESTRO_DAEMON_TOKEN=<token>
maestro delegate --title "T" --request "R" --target codex --workspace /path/to/repo
```

Without `MAESTRO_DAEMON_URL`, the CLI uses the local `daemon.json` marker; a
stale marker (dead process) is rejected with a clear error instead of failing
mid-stream.

## Step 3b — Register it as an agent for repeated use

For ongoing delegation (and so a supervising agent can route to it), register
the remote daemon as an `a2a_remote` agent:

```bash
maestro agents add --name remote-b --kind a2a_remote \
  --command http://10.0.0.5:8790 \
  --token <token>
maestro agents status remote-b      # live check: fetches B's agent card
maestro delegate --title "T" --request "R" --target remote-b --workspace /path/to/repo
```

The full handoff document travels with the request, so routing survives the
hop. The one field left out is `[expectations] branch`: a task branch name
applies only to the workspace of the daemon that runs the task. The remote
daemon gets a new request on every attempt and every turn, so it puts its work
on its own default branch, `maestro/<remote-task-id>`, instead of refusing
every request after the first because the named branch already exists. Maestro checks the remote's agent card before delegating, streams its
output and usage live, and forwards cancellation. If the remote has no free
workspace slot it queues the task; a missing/wrong token fails fast with
`HTTP 401 — check this agent's token`.

## Step 4 (optional) — Peer discovery

Daemons on the same LAN find each other automatically: each daemon periodically
announces itself over UDP multicast (port 9786) and peers are recorded in
`~/.maestro/peers.json`.

```bash
maestro peers list          # live + stale peers, with URLs
maestro peers add --name lab --url http://10.0.0.5:8790   # manual registration
maestro peers remove lab
```

Use `peers add` instead of auto-discovery when the network blocks multicast
(many corporate networks, VPNs), when you want a stable name for a long-lived
daemon, or when one machine runs several daemons. Discovered peers go stale
after ~15 s of silence; manually added peers keep their URL forever.

Peers are an informational roster — to actually delegate to a discovered
daemon, register it as an agent with its token (Step 3b). Discovery
announcements never carry tokens. Tuning variables (`MAESTRO_DISCOVERY`,
`MAESTRO_DISCOVERY_PORT`, `MAESTRO_DISCOVERY_IF`, `MAESTRO_DISCOVERY_TTL`,
`MAESTRO_NODE_NAME`) are in the
[configuration reference](../reference/configuration.md#environment-variables).

## Step 5 — Open the console from another machine

```text
http://10.0.0.5:8790/?token=<t>
```

The token is captured into the browser session and stripped from the address
bar.

## Step 6 (optional) — Put the daemon behind a reverse proxy

The daemon refuses a browser POST whose `Origin` header does not exactly match
the address the request was sent to (its `Host` header). A reverse proxy that
forwards `https://maestro.example.com` to the daemon usually changes the `Host`
header to the daemon's own address, such as `127.0.0.1:8790`, while the browser
still sends `Origin: https://maestro.example.com`. The daemon then refuses the
console's POSTs with `HTTP 403 — cross-origin requests are not allowed`.

List the proxy's public origin so the daemon accepts it:

```bash
maestro-daemon --port 8790 --allow-origin https://maestro.example.com
```

Repeat `--allow-origin` for more than one origin. For a daemon started with
`maestro daemon start` or by the MCP server, which take no options, set the
environment variable instead, as a comma-separated list:

```bash
export MAESTRO_DAEMON_ALLOWED_ORIGINS=https://maestro.example.com,https://maestro.internal:8443
maestro daemon start
```

The `--allow-origin` option wins over the environment variable when both are
given. Write each origin the way a browser sends it: `scheme://host[:port]`,
with no path. An invalid entry stops the daemon from starting. Requests without
an `Origin` header, such as the CLI, the MCP server and `curl`, are not
affected.

A daemon on loopback also refuses any `Host` header other than `127.0.0.1`,
`localhost` or `::1`, so configure the proxy to send the daemon's own address
as `Host` (for nginx, `proxy_pass http://127.0.0.1:8790;` without
`proxy_set_header Host $host;`). A daemon bound beyond loopback accepts any
`Host` but needs its token on every data request.

## Security notes

- Treat the token as a credential: it gates every data endpoint of that
  daemon. Rotate by restarting with a new `MAESTRO_DAEMON_TOKEN`.
- Bind to the narrowest interface you can; `0.0.0.0` exposes every interface.
- The web console's static files are public by design so the app can load —
  do not rely on the console being unlisted for confidentiality; the data
  behind it is token-gated.
