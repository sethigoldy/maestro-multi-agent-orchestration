#!/bin/sh
# Maestro package validation — proves a real clean installation works.
#
# Builds the wheel and sdist, then installs EACH into a fresh throwaway virtual
# environment (never the repository's .venv) and checks:
#   * maestro --version / --help / doctor (+ --json parse)
#   * maestro-mcp and maestro-daemon console scripts resolve and start
#   * the web console assets (maestro/web_dist/) are inside the installed package
#   * the installed version matches pyproject.toml
#
# Usage: scripts/validate-package.sh
# Env:   PYTHON   — interpreter to build with (default: python3, then python).
#         Requires Python 3.11+ and the `build` module (pip install build).
#        DIST_DIR — directory already containing the wheel + sdist (e.g. dist/
#         after a CI `python -m build`); skips the in-script build step.

set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Prefer `python` (what setup-python/venvs provide), then python3.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if command -v python >/dev/null 2>&1; then PYTHON=python
    elif command -v python3 >/dev/null 2>&1; then PYTHON=python3
    else echo "validate-package: no Python interpreter found on PATH" >&2; exit 1
    fi
fi
# Make a relative PYTHON absolute now — the script cd's away before using it.
case "$PYTHON" in
    */*) case "$PYTHON" in /*) ;; *) PYTHON="$(pwd)/$PYTHON" ;; esac ;;
esac
echo "validate-package: using interpreter: $PYTHON ($($PYTHON --version 2>&1))"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/maestro-validate.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

EXPECTED_VERSION="$("$PYTHON" -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
echo "validate-package: expected version from pyproject.toml: $EXPECTED_VERSION"

# --- 1. Build both artifacts (or reuse a pre-built DIST_DIR) -----------------
if [ -n "${DIST_DIR:-}" ]; then
    mkdir -p "$WORK/dist"
    cp "$DIST_DIR"/*.whl "$DIST_DIR"/*.tar.gz "$WORK/dist/"
    echo "validate-package: reusing pre-built artifacts from $DIST_DIR"
else
    "$PYTHON" -m build --outdir "$WORK/dist"
fi
ls "$WORK/dist"
WHEEL="$(ls "$WORK/dist"/*.whl)"
SDIST="$(ls "$WORK/dist"/*.tar.gz)"
echo "validate-package: validating $WHEEL and $SDIST"

# Run every check from a NEUTRAL directory: with the repo root as cwd,
# `import maestro` would resolve to the repository copy (sys.path[0]) instead
# of the freshly installed package, silently validating nothing.
cd "$WORK"

# --- 2. Fresh venv for the WHEEL --------------------------------------------
"$PYTHON" -m venv "$WORK/wheel-venv"
WV="$WORK/wheel-venv/bin"
"$WV/pip" install --quiet --no-cache-dir "$WHEEL"

check_core() {
    # $1 = label, $2 = bin dir of the installed venv
    label="$1"; bin="$2"
    echo "--- [$label] core CLI checks ---"

    version="$("$bin/maestro" --version)"
    if [ "$version" != "$EXPECTED_VERSION" ]; then
        echo "FAIL [$label]: maestro --version is $version, expected $EXPECTED_VERSION" >&2
        exit 1
    fi
    echo "PASS [$label] maestro --version -> $version"

    "$bin/maestro" --help >/dev/null
    echo "PASS [$label] maestro --help exits 0"

    # doctor: fresh isolated state dir; must be usable (exit 0) and JSON-parseable.
    doctordir="$WORK/${label}-home"
    rc=0
    out="$(MAESTRO_HOME="$doctordir" "$bin/maestro" doctor --json)" || rc=$?
    if [ $rc -ne 0 ]; then
        echo "FAIL [$label]: maestro doctor exited $rc" >&2; echo "$out" >&2; exit 1
    fi
    ok="$(printf '%s' "$out" | "$bin/python" -c 'import json,sys; d=json.load(sys.stdin); print("true" if d.get("ok") else "false")')"
    if [ "$ok" != "true" ]; then
        echo "FAIL [$label]: doctor reported ok=false" >&2; echo "$out" >&2; exit 1
    fi
    echo "PASS [$label] maestro doctor --json -> ok=true (exit 0)"

    # Entry points resolve.
    if [ ! -x "$bin/maestro-daemon" ] || [ ! -x "$bin/maestro-mcp" ]; then
        echo "FAIL [$label]: maestro-daemon / maestro-mcp console scripts missing" >&2; exit 1
    fi
    echo "PASS [$label] maestro-daemon and maestro-mcp console scripts present"

    "$bin/maestro-daemon" --help >/dev/null
    echo "PASS [$label] maestro-daemon --help exits 0 (no server started)"

    # MCP stdio server: EOF on stdin must terminate it cleanly.
    "$bin/maestro-mcp" </dev/null >/dev/null 2>&1
    echo "PASS [$label] maestro-mcp terminates cleanly on stdin EOF"

    # Web console assets are packaged inside the installed distribution.
    webdir="$("$bin/python" -c 'import maestro, os; print(os.path.join(os.path.dirname(maestro.__file__), "web_dist"))')"
    for asset in console.js index.html; do
        if [ ! -s "$webdir/$asset" ]; then
            echo "FAIL [$label]: web asset $webdir/$asset missing or empty" >&2; exit 1
        fi
    done
    echo "PASS [$label] web assets packaged: $webdir/{console.js,index.html}"
}

check_core wheel "$WV"

# --- 3. Fresh venv for the SDIST --------------------------------------------
"$PYTHON" -m venv "$WORK/sdist-venv"
SV="$WORK/sdist-venv/bin"
# Standard pip install of an sdist (build isolation may fetch build deps).
"$SV/pip" install --quiet --no-cache-dir "$SDIST"

check_core sdist "$SV"

echo "validate-package: ALL CHECKS PASSED (wheel + sdist, version $EXPECTED_VERSION)"
