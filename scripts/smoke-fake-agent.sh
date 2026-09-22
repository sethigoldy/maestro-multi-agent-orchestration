#!/bin/sh
# Maestro smoke test — proves the full durable-execution loop with a
# deterministic FAKE agent: no real coding-agent CLI, network, or credentials
# are needed. This is NOT a production dependency on the fake; it exists so a
# fresh checkout can verify delegate -> attempt -> output -> verification ->
# receipt -> daemon restart -> receipt still available.
#
# Usage: scripts/smoke-fake-agent.sh   (from any directory)
# Requires: sh, git, and this checkout's .venv (python3.11 -m venv .venv && pip install -e .)

set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    echo "smoke: missing Python environment: $PYTHON" >&2
    echo "Run: python3.11 -m venv .venv && .venv/bin/python -m pip install -e ." >&2
    exit 1
fi

# Resolve to a real path (e.g. /tmp is a symlink on macOS) so workspace
# scoping matches what the daemon records.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/maestro-smoke.XXXXXX")"
WORK="$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$WORK")"
cleanup() {
    [ -n "${DAEMON_PID:-}" ] && kill "$DAEMON_PID" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

# Isolated state + hermetic discovery (loopback only, no TTL).
export MAESTRO_HOME="$WORK/state"
export MAESTRO_DISCOVERY_TTL=0
export MAESTRO_DISCOVERY_IF=127.0.0.1
unset MAESTRO_DAEMON_URL MAESTRO_DAEMON_TOKEN 2>/dev/null || true

# --- workspace: a git repo with no test runner (verification degrades to
# --- git diff --check, which now also requires evidence of work).
WS="$WORK/ws"
mkdir -p "$WS"
git -C "$WS" init -q
git -C "$WS" config user.email "smoke@local"
git -C "$WS" config user.name "Smoke Test"
echo "# smoke repo" > "$WS/README.md"
git -C "$WS" add . && git -C "$WS" commit -qm "initial"

# --- the fake agent: consumes the prompt, streams one line, reports usage,
# --- and leaves a real working-tree change (the fallback verifier refuses to
# --- certify a turn with no changes). Guarded: the preflight probe runs the
# --- binary with --version from the daemon's cwd, not a real turn.
FAKE="$WORK/fake-agent.sh"
cat > "$FAKE" <<'EOF'
#!/bin/sh
case "${1:-}" in --version) exit 0 ;; esac
cat > /dev/null
echo "implementing (fake agent)"
echo '{"cost_usd": 0.41}'
echo "fake work" >> README.md
exit 0
EOF
chmod +x "$FAKE"

# --- register the fake as a generic agent and start the daemon.
"$PYTHON" -m maestro.cli agents add --name smoke-fake --kind generic \
    --command "$FAKE {prompt}" --input-mode arg --output-format jsonl

DAEMON_LOG="$WORK/daemon.log"
"$PYTHON" -m maestro.daemon_main --port 0 >"$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!

i=0
while [ ! -f "$MAESTRO_HOME/daemon.json" ]; do
    i=$((i + 1))
    if [ "$i" -gt 50 ] || ! kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "smoke: daemon did not start (see $DAEMON_LOG)" >&2
        exit 1
    fi
    sleep 0.2
done

# --- delegate and wait for completion (delegate blocks by default).
"$PYTHON" -m maestro.cli delegate \
    --title "Smoke: fake agent run" \
    --request "Do the smoke thing." \
    --target smoke-fake \
    --workspace "$WS" >"$WORK/delegate.log" 2>&1

# --- find the task id.
TASK_ID="$("$PYTHON" -m maestro.cli task list --workspace "$WS" | "$PYTHON" -c '
import json, sys
tasks = json.load(sys.stdin)
assert tasks, "no tasks found"
print(tasks[0]["task_id"])
')"

# --- live receipt (served by the daemon over HTTP).
LIVE="$("$PYTHON" -m maestro.cli task receipt "$TASK_ID" --json)"
echo "--- live receipt ---"
echo "$LIVE" | head -30

# --- stop the daemon; the durable receipt must still be available.
kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
DAEMON_PID=""

DURABLE="$("$PYTHON" -m maestro.cli task receipt "$TASK_ID" --json)"
echo "--- durable receipt (daemon stopped) ---"
echo "$DURABLE" | head -30

# --- assert the loop end to end.
"$PYTHON" - <<EOF
import json, sys

live = json.loads('''$LIVE''')
durable = json.loads('''$DURABLE''')

for label, r in (("live", live), ("durable", durable)):
    assert r["state"] == "completed", f"{label}: state {r['state']!r}"
    assert r["task"]["title"] == "Smoke: fake agent run"
    attempts = r["attempts"]
    assert len(attempts) == 1, f"{label}: {len(attempts)} attempts"
    a = attempts[0]
    assert a["agent"] == "smoke-fake" and a["ok"] is True
    assert a["cost_usd"] == 0.41, f"{label}: cost {a['cost_usd']!r}"
    v = r["verification"]
    assert v["ran"] is True and v["result"] == "PASSED", f"{label}: verification {v}"
    t = r["totals"]
    assert t["cost_reported"] is True and abs(t["cost_usd"] - 0.41) < 1e-9

assert live["task"]["id"] == durable["task"]["id"]
print("smoke: receipt stable across daemon restart")
EOF

# --- human-readable rendering sanity (contains the key sections).
HUMAN="$("$PYTHON" -m maestro.cli task receipt "$TASK_ID")"
case "$HUMAN" in
    *"Maestro Execution Receipt"*"COMPLETED"*"PASSED"*) ;;
    *) echo "smoke: unexpected human receipt:" >&2; echo "$HUMAN" >&2; exit 1 ;;
esac

echo "SMOKE OK"
