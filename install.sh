#!/usr/bin/env bash
# Maestro one-command installer (macOS + Linux, no root required).
#
#   curl -fsSL https://raw.githubusercontent.com/sethigoldy/maestro-multi-agent-orchestration/main/install.sh | bash
#   ./install.sh                  # install or update in place
#   ./install.sh --update         # explicit alias for the default behavior
#   ./install.sh --uninstall      # remove Maestro (keeps ~/.maestro state)
#   ./install.sh --uninstall --purge-state   # also delete the state directory
#
# What it does:
#   1. Verifies prerequisites (Python >= 3.11, git).
#   2. Clones/updates the repository into ~/.local/share/maestro/src
#      (HTTPS; a fresh download goes through a temporary directory).
#   3. Creates/refreshes a virtualenv at ~/.local/share/maestro/venv and
#      installs Maestro into it — the system Python is never touched.
#   4. Writes launcher scripts for maestro / maestro-daemon / maestro-mcp
#      into ~/.local/bin (on PATH for most shells).
#   5. Discovers locally installed coding-agent CLIs and registers them with
#      Maestro (existing registrations are preserved, never overwritten).
#   6. Installs the global maestro-driven-development skill into every
#      detected supported agent.
#   7. Starts (or restarts) the background daemon and verifies it is healthy.
#
# The script is idempotent: rerunning it updates Maestro in place, preserves
# ~/.maestro state and custom agent registrations, and never duplicates skill
# configuration.

set -euo pipefail

REPO_URL="${MAESTRO_REPO_URL:-https://github.com/sethigoldy/maestro-multi-agent-orchestration.git}"
INSTALL_ROOT="${MAESTRO_INSTALL_ROOT:-$HOME/.local/share/maestro}"
BIN_DIR="${MAESTRO_BIN_DIR:-$HOME/.local/bin}"
STATE_DIR="${MAESTRO_HOME:-$HOME/.maestro}"
PYTHON="${MAESTRO_PYTHON:-}"

MODE="install"
PURGE_STATE=0

for arg in "$@"; do
    case "$arg" in
        --update) MODE="update" ;;
        --uninstall) MODE="uninstall" ;;
        --purge-state) PURGE_STATE=1 ;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "install.sh: unknown option: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

say()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v sed >/dev/null 2>&1 || die "sed is required"

# ---------------------------------------------------------------- prerequisites
require_python() {
    if [ -z "$PYTHON" ]; then
        local candidate
        for candidate in python3.12 python3.11 python3 python; do
            if command -v "$candidate" >/dev/null 2>&1; then
                PYTHON="$candidate"
                break
            fi
        done
    fi
    [ -n "$PYTHON" ] || die "no Python interpreter found on PATH (need Python >= 3.11)"
    if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        die "Python >= 3.11 is required ($($PYTHON --version 2>&1 || echo '$PYTHON') found); install it and rerun this script"
    fi
}

require_git() {
    command -v git >/dev/null 2>&1 || die "git is required (install it with your system package manager, e.g. 'brew install git' or 'apt-get install git')"
}

# ------------------------------------------------------------------- source mgmt
verify_repo() {
    # Sanity-check that the checkout is really this project (HTTPS clone of the
    # expected repository; we verify identity by content, not by trust).
    local dir="$1"
    [ -f "$dir/pyproject.toml" ] || die "downloaded source does not look like the Maestro repository (missing pyproject.toml)"
    grep -q '^name = "maestro"' "$dir/pyproject.toml" || die "downloaded source is not the Maestro project (unexpected pyproject.toml)"
    [ -f "$dir/maestro/cli.py" ] || die "downloaded source is incomplete (missing maestro/cli.py)"
}

fetch_source() {
    local src="$INSTALL_ROOT/src"
    mkdir -p "$INSTALL_ROOT"
    if [ -d "$src/.git" ]; then
        say "Updating existing Maestro source at $src"
        git -C "$src" remote set-url origin "$REPO_URL"
        git -C "$src" fetch --depth 1 origin || die "failed to update the Maestro checkout from $REPO_URL"
        git -C "$src" reset --hard FETCH_HEAD
        verify_repo "$src"
    else
        if [ -e "$src" ]; then
            warn "$src exists but is not a git checkout; replacing it"
            rm -rf "$src"
        fi
        local tmp
        tmp="$(mktemp -d "${TMPDIR:-/tmp}/maestro-install.XXXXXX")"
        trap 'rm -rf "$tmp"' RETURN
        say "Downloading Maestro from $REPO_URL"
        git clone --depth 1 "$REPO_URL" "$tmp/repo" || die "failed to clone $REPO_URL (check network access and that the repository exists)"
        verify_repo "$tmp/repo"
        mv "$tmp/repo" "$src"
    fi
}

# ------------------------------------------------------------------------ venv
install_venv() {
    local src="$INSTALL_ROOT/src"
    local venv="$INSTALL_ROOT/venv"
    if [ ! -x "$venv/bin/python" ]; then
        say "Creating virtualenv at $venv"
        "$PYTHON" -m venv "$venv" || die "failed to create a virtualenv with $PYTHON (on Debian/Ubuntu you may need 'python3-venv')"
    fi
    say "Installing Maestro into the virtualenv"
    "$venv/bin/pip" install --quiet --upgrade pip 2>/dev/null || warn "could not upgrade pip (continuing with the bundled version)"
    "$venv/bin/pip" install --quiet --upgrade "$src" \
        || die "pip install failed; if you are offline, pre-provision $venv and rerun"
}

# -------------------------------------------------------------------- launchers
write_launchers() {
    mkdir -p "$BIN_DIR"
    local name
    for name in maestro maestro-daemon maestro-mcp; do
        cat > "$BIN_DIR/$name" <<EOF
#!/bin/sh
exec "$INSTALL_ROOT/venv/bin/$name" "\$@"
EOF
        chmod +x "$BIN_DIR/$name"
    done
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "$BIN_DIR is not on your PATH; add it to your shell profile:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
    esac
}

# ------------------------------------------------------------------ integration
run_maestro() {
    # Always drive the freshly installed CLI (never a stale one from PATH).
    "$INSTALL_ROOT/venv/bin/maestro" "$@"
}

setup_agents_and_skill() {
    say "Discovering and registering coding agents"
    run_maestro agents register-discovered || die "agent registration failed"
    say "Installing the global maestro-driven-development skill"
    run_maestro skill install || warn "skill installation reported problems (continuing)"
}

setup_daemon() {
    local running
    running="$(run_maestro daemon status --json 2>/dev/null | "$INSTALL_ROOT/venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); print("true" if d.get("running") else "false")' || echo false)"
    if [ "$running" = "true" ]; then
        say "Daemon already running — restarting it to pick up the new Maestro version"
        run_maestro daemon restart || die "daemon restart failed"
    else
        say "Starting the Maestro daemon in the background"
        run_maestro daemon start || die "daemon start failed"
    fi
    run_maestro daemon status --json | "$INSTALL_ROOT/venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("running") else 1)' \
        || die "daemon is not healthy after start (check $STATE_DIR/daemon.log)"
}

print_summary() {
    run_maestro daemon status --json > "$TMP_SUMMARY_DAEMON"
    run_maestro agents list > "$TMP_SUMMARY_AGENTS"
    run_maestro skill status > "$TMP_SUMMARY_SKILL"
    "$INSTALL_ROOT/venv/bin/python" - "$TMP_SUMMARY_DAEMON" "$TMP_SUMMARY_AGENTS" "$TMP_SUMMARY_SKILL" <<'PYEOF'
import json, sys

daemon = json.load(open(sys.argv[1]))
agents = json.load(open(sys.argv[2]))
skill = json.load(open(sys.argv[3]))

print("Maestro installed successfully.")
print()
print("Daemon:")
if daemon.get("running"):
    print("  ✓ running")
    if daemon.get("pid") is not None:
        print(f"  PID: {daemon['pid']}")
    if daemon.get("url"):
        print(f"  URL: {daemon['url']}")
else:
    print("  ✗ not running (see 'maestro daemon status')")
print()
print("Agents:")
if agents:
    for agent in agents:
        print(f"  ✓ {agent.get('display_name') or agent.get('name')}")
else:
    print("  - none registered yet ('maestro agents discover' shows candidates)")
print()
print("Global skill:")
installed = [s for s in skill if s.get("installed")]
if installed:
    print(f"  ✓ maestro-driven-development ({len(installed)} agent(s): {', '.join(s['display_name'] for s in installed)})")
else:
    print("  - not installed for any detected agent")
print()
print("You can now open any supported coding agent and start developing normally.")
print("Development tasks will automatically use Maestro.")
PYEOF
}

do_uninstall() {
    local venv_maestro="$INSTALL_ROOT/venv/bin/maestro"
    if [ -x "$venv_maestro" ]; then
        say "Stopping the Maestro daemon (if running)"
        "$venv_maestro" daemon stop >/dev/null 2>&1 || true
        say "Removing the global skill from all agents"
        "$venv_maestro" skill uninstall || warn "skill removal reported problems (continuing)"
    fi
    local name
    for name in maestro maestro-daemon maestro-mcp; do
        rm -f "$BIN_DIR/$name"
    done
    if [ -e "$INSTALL_ROOT" ]; then
        say "Removing $INSTALL_ROOT"
        rm -rf "$INSTALL_ROOT"
    fi
    if [ "$PURGE_STATE" = "1" ] && [ -d "$STATE_DIR" ]; then
        say "Removing state directory $STATE_DIR"
        rm -rf "$STATE_DIR"
    else
        say "Kept your Maestro state at $STATE_DIR (delete it manually if you no longer need it)"
    fi
    say "Maestro uninstalled."
}

# ------------------------------------------------------------------------ main
TMP_SUMMARY_DAEMON="$(mktemp "${TMPDIR:-/tmp}/maestro-daemon-status.XXXXXX")"
TMP_SUMMARY_AGENTS="$(mktemp "${TMPDIR:-/tmp}/maestro-agents.XXXXXX")"
TMP_SUMMARY_SKILL="$(mktemp "${TMPDIR:-/tmp}/maestro-skill.XXXXXX")"
trap 'rm -f "$TMP_SUMMARY_DAEMON" "$TMP_SUMMARY_AGENTS" "$TMP_SUMMARY_SKILL"' EXIT

case "$MODE" in
    uninstall)
        require_python
        do_uninstall
        ;;
    install|update)
        say "Maestro installer (mode: $MODE)"
        require_python
        require_git
        fetch_source
        install_venv
        write_launchers
        setup_agents_and_skill
        setup_daemon
        print_summary
        ;;
esac
