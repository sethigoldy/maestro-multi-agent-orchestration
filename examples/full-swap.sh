#!/usr/bin/env bash
# Maestro flagship demo — the "full role swap".
#
# Normally a human supervises agents. This script shows the other direction:
#   Leg A: Claude Code acts as a HOST agent — it writes a handoff and delegates
#          the work to Codex through Maestro's MCP tools, blocking until done.
#          (The MCP server runs its own embedded broker; both brokers share one
#          state directory, so everything stays visible afterwards.)
#   Leg B: the CLI (standing in for any host) delegates a follow-on task to
#          Hermes via a standalone broker, building on the file Codex just wrote.
#   Then:  the durable audit record (attempts + usage/cost) is printed for both.
#
# Requirements: python3 with this checkout importable, plus the `claude` and
# `codex` CLIs installed and authenticated (Leg A), and `hermes` (Leg B).
# If claude or codex is missing, Leg A falls back to a CLI delegation to codex.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/maestro-swap-ws.XXXXXX")"
STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/maestro-swap-state.XXXXXX")"
export MAESTRO_HOME="$STATE_DIR"
export MAESTRO_WORKSPACE="$WORKSPACE"

DAEMON_PID=""
cleanup() {
  if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill "$DAEMON_PID" 2>/dev/null || true
  fi
  echo "demo state: $STATE_DIR (kept for inspection)"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

command -v "$PYTHON" >/dev/null || fail "python3 not found"
( cd "$REPO_ROOT" && "$PYTHON" -c "import maestro" ) 2>/dev/null || fail "maestro package not importable from $REPO_ROOT"

start_daemon() {
  ( cd "$REPO_ROOT" && "$PYTHON" -m maestro.daemon_main --state-dir "$STATE_DIR" ) >"$STATE_DIR/daemon.out" 2>&1 &
  DAEMON_PID=$!
  for _ in $(seq 1 50); do
    [[ -f "$STATE_DIR/daemon.json" ]] && break
    kill -0 "$DAEMON_PID" 2>/dev/null || fail "daemon exited early: $(cat "$STATE_DIR/daemon.out")"
    sleep 0.2
  done
  [[ -f "$STATE_DIR/daemon.json" ]] || fail "daemon did not start"
}

# ---------------------------------------------------------------- Leg A
LEG_A_VIA_MCP=1
command -v claude >/dev/null && command -v codex >/dev/null || LEG_A_VIA_MCP=0

HANDOFF_A="$STATE_DIR/handoff-legA.toml"
cat > "$HANDOFF_A" <<EOF
[handoff]
title = "Full-swap demo, leg A"
request = """Create a file named hello_swap.py in the workspace root. It must print exactly: swap complete"""

[routing]
target_agent = "codex"
fallback = ["hermes"]

[expectations]
verification = "command"
commit_policy = "branch"

[constraints]
budget_hint = "one small file, no dependencies"
EOF

if [[ "$LEG_A_VIA_MCP" == 1 ]]; then
  echo "== leg A: Claude Code delegates to Codex via Maestro MCP (embedded broker) =="
  MCP_CONFIG="$STATE_DIR/mcp-config.json"
  cat > "$MCP_CONFIG" <<EOF
{"mcpServers":{"maestro":{"command":"$PYTHON","args":["-m","maestro.mcp_server"],"cwd":"$REPO_ROOT","env":{"MAESTRO_HOME":"$STATE_DIR"}}}}
EOF
  ( cd "$WORKSPACE" && timeout 900 claude -p \
      --mcp-config "$MCP_CONFIG" \
      "You are operating as a host agent for the Maestro broker. Use the 'maestro' MCP tools: call delegate with workspace='$WORKSPACE' and handoff_file='$HANDOFF_A'. It blocks until the work is done. Then report the final task state in one line." ) \
    || fail "leg A (claude -> codex) failed"
else
  echo "== leg A: CLI delegates to Codex via standalone broker =="
  start_daemon
  ( cd "$REPO_ROOT" && timeout 900 "$PYTHON" -m maestro.cli delegate --file "$HANDOFF_A" --workspace "$WORKSPACE" ) \
    || fail "leg A (cli -> codex) failed"
fi

[[ -f "$WORKSPACE/hello_swap.py" ]] || fail "hello_swap.py was not created in $WORKSPACE"
echo "   leg A produced: $(head -n 1 "$WORKSPACE/hello_swap.py")"

# ---------------------------------------------------------------- Leg B
if command -v hermes >/dev/null; then
  [[ -n "$DAEMON_PID" ]] || start_daemon   # the embedded broker died with claude
  echo "== leg B: CLI delegates to Hermes, building on leg A's file =="
  HANDOFF_B="$STATE_DIR/handoff-legB.toml"
  cat > "$HANDOFF_B" <<EOF
[handoff]
title = "Full-swap demo, leg B"
request = """Run 'python3 hello_swap.py' in the workspace and write its output into a file named swap_result.txt. Do not modify hello_swap.py."""

[routing]
target_agent = "hermes"

[expectations]
verification = "command"
commit_policy = "branch"
EOF
  ( cd "$REPO_ROOT" && timeout 900 "$PYTHON" -m maestro.cli delegate --file "$HANDOFF_B" --workspace "$WORKSPACE" ) \
    || fail "leg B (cli -> hermes) failed"
  [[ -s "$WORKSPACE/swap_result.txt" ]] || fail "swap_result.txt missing or empty"
  echo "   leg B produced: $(cat "$WORKSPACE/swap_result.txt")"
else
  echo "== leg B skipped (hermes not installed) =="
fi

# ---------------------------------------------------------------- audit trail
echo "== audit trail =="
for TASK_ID in $( "$PYTHON" - <<'PYEOF'
import json, os
state = os.environ["MAESTRO_HOME"]
try:
    index = json.load(open(os.path.join(state, "registry.json")))
except (OSError, ValueError):
    raise SystemExit(0)
for item in sorted(index, key=lambda x: int(x.get("number", 0))):
    print(item["task_id"])
PYEOF
); do
  ( cd "$REPO_ROOT" && "$PYTHON" -m maestro.cli task audit "$TASK_ID" ) || true
done

echo "FULL-SWAP DEMO PASSED — workspace: $WORKSPACE"
