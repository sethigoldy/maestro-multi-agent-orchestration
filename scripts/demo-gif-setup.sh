#!/bin/sh
# Setup half of the flagship demo GIF (scripts/demo-gif.tape).
#
# Mirrors the setup section of scripts/demo-v0.10.sh: creates a throwaway
# git workspace, registers two deterministic fake agents (implementer +
# stateful verifier gate), writes the [modes.demo] preset, and starts an
# isolated local daemon. Exports WS/MAESTRO_HOME in the caller's shell when
# sourced, and prints the workspace path on stdout either way.
#
# Run from the repo root (or set REPO_ROOT). The daemon keeps running after
# this script exits — kill it with:  kill $(cat "$MAESTRO_HOME/daemon.pid")

set -eu

REPO_ROOT="${REPO_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    echo "demo-gif-setup: missing Python environment: $PYTHON" >&2
    exit 1
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/maestro-demo.XXXXXX")"
WORK="$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$WORK")"
export MAESTRO_HOME="$WORK/state"
export MAESTRO_DISCOVERY_TTL=0
export MAESTRO_DISCOVERY_IF=127.0.0.1
unset MAESTRO_DAEMON_URL MAESTRO_DAEMON_TOKEN 2>/dev/null || true

# --- workspace: a plain git repo (no test runner -> verification degrades to
# --- the deterministic `git diff --check` whitespace gate).
WS="$WORK/ws"
mkdir -p "$WS"
git -C "$WS" init -q
git -C "$WS" config user.email demo@local
git -C "$WS" config user.name Demo
echo "# demo repo" > "$WS/README.md"
git -C "$WS" add . && git -C "$WS" commit -qm "initial"

# --- fake agents (same bodies as scripts/demo-v0.10.sh) ---------------------
IMPL="$WORK/demo-impl.sh"
cat > "$IMPL" <<'FAKE'
#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "demo-impl 0.1 (deterministic fake)"
    exit 0
fi
cat > /dev/null
echo "implementing the change..."
echo '{"cost_usd": 0.15}'
echo "- health-check endpoint" >> README.md
exit 0
FAKE
chmod +x "$IMPL"

VERIFY="$WORK/demo-verify.sh"
cat > "$VERIFY" <<FAKE
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
FAKE
chmod +x "$VERIFY"

# --- register agents + work-mode preset --------------------------------------
"$PYTHON" -m maestro.cli agents add --name demo-impl --kind generic \
    --command "$IMPL {prompt}" --input-mode arg --output-format jsonl >/dev/null
"$PYTHON" -m maestro.cli agents add --name demo-verify --kind generic \
    --command "$VERIFY {prompt}" --input-mode arg --output-format text >/dev/null

cat > "$MAESTRO_HOME/config.toml" <<'TOML'
[modes.demo]
implementer = "demo-impl"
verifier    = "demo-verify"
fixer       = "demo-impl"
max_bounces = 1
TOML

# --- daemon (background; pid recorded for the teardown phase) -----------------
"$PYTHON" -m maestro.daemon_main --port 0 >"$WORK/daemon.log" 2>&1 &
echo $! > "$MAESTRO_HOME/daemon.pid"
i=0
while [ ! -f "$MAESTRO_HOME/daemon.json" ]; do
    i=$((i + 1))
    if [ "$i" -gt 50 ] || ! kill -0 "$(cat "$MAESTRO_HOME/daemon.pid")" 2>/dev/null; then
        echo "demo-gif-setup: daemon did not start (see $WORK/daemon.log)" >&2
        exit 1
    fi
    sleep 0.2
done

rm -f "$WORK/verify-count"
export WS
echo "$WS"
