#!/usr/bin/env bash
# Build a VHS binary that records scripts/demo-gif.tape reliably.
#
# Upstream VHS v0.12.0 has two problems that corrupt or crash demo GIF
# recordings in headless Chrome:
#
# 1. ffmpeg is launched with exec.CommandContext using the render context,
#    which is already cancelled by the time encoding starts — the encode step
#    dies and the run exits without producing a GIF. (video.go / screenshot.go)
#
# 2. xterm.js pauses its canvas renderer when an IntersectionObserver reports
#    the terminal element as not intersecting, which headless Chrome does
#    intermittently. While paused, output lands in the terminal buffer but is
#    never drawn to the canvases VHS captures, so lines vanish from the GIF
#    (nondeterministically). The fix forces the renderer's _isPaused flag to
#    false and re-renders all rows before every frame capture.
#
# Usage:
#   scripts/demo-gif-build-vhs.sh [output-binary]     # default: ./vhs-demo
#
# Requires: go, git, python3. Then record with:
#   VHS_NO_SANDBOX=1 ./vhs-demo scripts/demo-gif.tape -o docs/assets/demo-v0.10.gif
set -euo pipefail

OUT="${1:-./vhs-demo}"
VHS_REF="v0.12.0"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --quiet --depth 1 --branch "$VHS_REF" https://github.com/charmbracelet/vhs.git "$WORK/vhs"

cd "$WORK/vhs"

# --- Patch 1: detach ffmpeg from the (already cancelled) render context.
python3 - video.go screenshot.go <<'PYEOF'
import re, sys

for path in sys.argv[1:]:
    src = open(path).read()
    fixed, n = re.subn(
        r'exec\.CommandContext\(\s*\n(\s*)ctx,\s*\n(\s*)"ffmpeg",',
        r'exec.Command(\n\2"ffmpeg",',
        src,
    )
    if n == 0:
        print(f"note: {path} has no multi-line CommandContext(ffmpeg) call; skipping", file=sys.stderr)
        continue
    open(path, "w").write(fixed)
    print(f"patched {path} ({n} call(s))")
PYEOF

# --- Patch 2: never pause the xterm.js renderer; refresh before each capture.
python3 - vhs.go <<'PYEOF'
import sys

path = sys.argv[1]
src = open(path).read()

anchor = 'vhs.TextCanvas, _ = vhs.Page.Element("canvas.xterm-text-layer")\n\tvhs.CursorCanvas, _ = vhs.Page.Element("canvas.xterm-cursor-layer")'
assert anchor in src, "canvas element lookup not found (VHS layout changed?)"
hook = (
    anchor
    + "\n"
    + "\t// Demo-recording fix: headless Chrome intermittently reports the terminal\n"
    + "\t// element as non-intersecting, which makes xterm.js pause its canvas\n"
    + '\t// renderer and drop lines from the captured frames. Force it to stay on.\n'
    + '\tvhs.Page.MustEval(`() => { try { const rs = term._renderService; Object.defineProperty(rs, "_isPaused", { get: () => false, set: () => {} }); } catch (e) { console.error("vhs-demo: no-pause hook failed:", e.message); } }`)'
)
src = src.replace(anchor, hook)

anchor2 = (
    "\t\t\t\tif vhs.Page == nil {\n"
    "\t\t\t\t\tcontinue\n"
    "\t\t\t\t}\n"
    "\n"
    '\t\t\t\tcursor, cursorErr := vhs.CursorCanvas.CanvasToImage("image/png", quality)'
)
assert anchor2 in src, "record loop not found (VHS layout changed?)"
fix2 = (
    "\t\t\t\tif vhs.Page == nil {\n"
    "\t\t\t\t\tcontinue\n"
    "\t\t\t\t}\n"
    "\n"
    "\t\t\t\t// Demo-recording fix: force a full re-render and wait one animation\n"
    "\t\t\t\t// frame so the canvases are up to date before we capture them.\n"
    '\t\t\t\t_, _ = vhs.Page.Eval(`() => new Promise((resolve) => { try { term.refresh(0, term.rows - 1); } catch (e) {} requestAnimationFrame(() => resolve(true)); })`)\n'
    "\n"
    '\t\t\t\tcursor, cursorErr := vhs.CursorCanvas.CanvasToImage("image/png", quality)'
)
src = src.replace(anchor2, fix2)
open(path, "w").write(src)
print("patched vhs.go")
PYEOF

go build -o "$OUT" .
echo "built $OUT"
