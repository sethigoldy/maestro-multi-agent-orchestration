#!/bin/sh
# Maestro v0.10 flagship demo — deterministic, no network, no real agent CLIs.
#
# Reuses the fake-agent infrastructure from smoke-fake-agent.sh to demonstrate
# the core product story end to end:
#
#   doctor  ->  delegate (work mode)  ->  IMPLEMENT / VERIFY / FIX attempts
#           ->  deterministic verification  ->  execution receipt
#           ->  daemon restart  ->  durable receipt still available
#
# The verifier fake FAILs on its first pass (with one concrete issue), which
# drives one auto-fix bounce; the deterministic check then passes and the task
# completes. Every number shown is produced by this run — nothing is faked.
#
# Usage: scripts/demo-v0.10.sh        (from any directory)
# Requires: sh, git, and this checkout's .venv (python3.11 -m venv .venv && pip install -e .)

set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    echo "demo: missing Python environment: $PYTHON" >&2
    echo "Run: python3.11 -m venv .venv && .venv/bin/python -m pip install -e ." >&2
    exit 1
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/maestro-demo.XXXXXX")"
WORK="$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$WORK")"
cleanup() {
    [ -n "${DAEMON_PID:-}" ] && kill "$DAEMON_PID" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

export MAESTRO_HOME="$WORK/state"
export MAESTRO_DISCOVERY_TTL=0
export MAESTRO_DISCOVERY_IF=127.0.0.1
unset MAESTRO_DAEMON_URL MAESTRO_DAEMON_TOKEN 2>/dev/null || true

banner() {
    echo ""
    echo "==================================================================="
    echo "$1"
    echo "==================================================================="
}

# --- workspace: a plain git repo (no test runner -> verification degrades to
# --- the deterministic `git diff --check` whitespace gate).
WS="$WORK/ws"
mkdir -p "$WS"
git -C "$WS" init -q
git -C "$WS" config user.email "demo@local"
git -C "$WS" config user.name "Demo"
echo "# demo repo" > "$WS/README.md"
git -C "$WS" add . && git -C "$WS" commit -qm "initial"

# --- fake agents -------------------------------------------------------------
# Implementer (also the fixer): streams one line, reports a small usage cost.
# Like a real CLI it answers --version without side effects (Maestro's preflight
# probes run `<binary> --version` before every turn and before doctor's check).
IMPL="$WORK/demo-impl.sh"
cat > "$IMPL" <<'EOF'
#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "demo-impl 0.1 (deterministic fake)"
    exit 0
fi
cat > /dev/null
echo "implementing the change..."
echo '{"cost_usd": 0.15}'
exit 0
EOF
chmod +x "$IMPL"

# Verifier: FAILs on its first real pass (one concrete issue), PASSes after.
# The counter file makes the sequence deterministic; --version stays side-effect
# free so preflight probes never consume a tick.
VERIFY="$WORK/demo-verify.sh"
cat > "$VERIFY" <<EOF
#!/bin/sh
if [ "\$1" = "--version" ]; then
    echo "demo-verify 0.1 (deterministic gate)"
    exit 0
fi
COUNT_FILE="$WORK/verify-count"
n=0
[ -f "\$COUNT_FILE" ] && n=\$(cat "\$COUNT_FILE")
n=\$((n + 1))
echo "\$n" > "\$COUNT_FILE"
if [ "\$n" -eq 1 ]; then
    echo "reviewing the diff and test evidence..."
    echo "VERDICT: FAIL"
    echo "- flaky assertion in tests/api/test_health.py"
else
    echo "re-reviewing after the fix..."
    echo "VERDICT: PASS"
fi
exit 0
EOF
chmod +x "$VERIFY"

# --- register agents + work-mode preset --------------------------------------
banner "[1/6] Registering deterministic fake agents (kind=generic)"
"$PYTHON" -m maestro.cli agents add --name demo-impl --kind generic \
    --command "$IMPL {prompt}" --input-mode arg --output-format jsonl >/dev/null
"$PYTHON" -m maestro.cli agents add --name demo-verify --kind generic \
    --command "$VERIFY {prompt}" --input-mode arg --output-format text >/dev/null

cat > "$MAESTRO_HOME/config.toml" <<'EOF'
[modes.demo]
implementer = "demo-impl"
verifier    = "demo-verify"
fixer       = "demo-impl"
max_bounces = 1
EOF
echo "agents: demo-impl (implement+fix), demo-verify (gate) — preset [modes.demo]"

# --- daemon ------------------------------------------------------------------
banner "[2/6] Starting the local daemon"
DAEMON_LOG="$WORK/daemon.log"
"$PYTHON" -m maestro.daemon_main --port 0 >"$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!
i=0
while [ ! -f "$MAESTRO_HOME/daemon.json" ]; do
    i=$((i + 1))
    if [ "$i" -gt 50 ] || ! kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "demo: daemon did not start (see $DAEMON_LOG)" >&2
        exit 1
    fi
    sleep 0.2
done
echo "daemon up (loopback, ephemeral port)"

banner "[3/6] maestro doctor --workspace <ws>"
"$PYTHON" -m maestro.cli doctor --workspace "$WS" | sed 's/^/  /'

# --- delegate ----------------------------------------------------------------
# Defensive: no prior run may have left counter state behind (probes answer
# --version without touching the counter, but this keeps the demo idempotent).
rm -f "$WORK/verify-count"

banner "[4/6] Delegating a work-mode task (delegate blocks until done)"
echo "\$ maestro delegate --title 'Add a health-check endpoint' \\"
echo "    --request 'Implement the change described in the handoff.' \\"
echo "    --mode demo --workspace $WS"
"$PYTHON" -m maestro.cli delegate \
    --title "Add a health-check endpoint" \
    --request "Implement the change described in the handoff." \
    --mode demo \
    --workspace "$WS" >"$WORK/delegate.log" 2>&1

TASK_ID="$("$PYTHON" -m maestro.cli task list --workspace "$WS" | "$PYTHON" -c '
import json, sys
tasks = json.load(sys.stdin)
assert tasks, "no tasks found"
print(tasks[0]["task_id"])
')"
echo "task: $TASK_ID (state: completed)"

banner "[5/6] Execution receipt — maestro task receipt $TASK_ID"
"$PYTHON" -m maestro.cli task receipt "$TASK_ID"
echo ""
echo "(note: the ✓ on the VERIFY attempt means the gate RUN succeeded;"
echo " its verdict — FAIL with one issue — is what drove the FIX bounce below)"

# --- durable receipt across daemon restart -----------------------------------
banner "[6/6] Stopping the daemon — the receipt must survive (durable state)"
kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
DAEMON_PID=""
# (script via heredoc on stdin, so the JSON goes through a file, not a pipe)
"$PYTHON" -m maestro.cli task receipt "$TASK_ID" --json > "$WORK/receipt.json"
"$PYTHON" - "$WORK/receipt.json" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1]))
t, v, g = r["totals"], r["verification"], r["gates"]
print("durable receipt after daemon stop:")
print(f"  state={r['state']}  runs={t['attempts']}  cost=${t['cost_usd']:.2f}")
print(f"  verification={v['result']}  bounces={g['bounces']}")
PYEOF

echo ""
echo "==================================================================="
echo "Durable execution for coding agents."
echo "==================================================================="
