# Launch assets

This directory holds images for the README and release notes.

**Checked in:** `demo-v0.10.gif` — a ~85-second terminal recording of the
flagship demo (doctor → work-mode delegation → verify-fix bounce → durable
receipt), embedded in the README. It is produced by driving the real CLI
through [VHS](https://github.com/charmbracelet/vhs) with the phases paced to
the storyboard in `docs/demo-v0.10.md`; every line shown is genuine output
from a real run (only the timing is presentation). Re-record it with:

```sh
scripts/demo-gif-build-vhs.sh          # builds ./vhs-demo (patched VHS, see below)
VHS_NO_SANDBOX=1 ./vhs-demo scripts/demo-gif.tape -o docs/assets/demo-v0.10.gif
```

The tape's hidden setup phase runs `scripts/demo-gif-setup.sh` (same fake
agents and work-mode preset as `scripts/demo-v0.10.sh`). VHS needs `ffmpeg`,
`ttyd`, and Go on PATH; in nested-sandbox environments add `VHS_NO_SANDBOX=1`.
The v0.11 recording uses Menlo with zero letter spacing — the earlier take
rendered with wide character gaps, which is why the tape pins
`Set FontFamily Menlo` / `Set LetterSpacing 0`.

Upstream VHS v0.12.0 cannot record this tape reliably, so
`scripts/demo-gif-build-vhs.sh` builds a patched binary (details in the
script header): it detaches the ffmpeg encode from the already-cancelled
render context (upstream exits without writing the GIF) and keeps xterm.js's
canvas renderer from pausing under headless Chrome — while paused, output
lands in the terminal buffer but is never drawn to the canvases VHS captures,
so lines vanish from the recording nondeterministically. Recordings made
with stock VHS may be missing lines (notably near the end of the tape);
verify a re-recording by checking that its final frame ends with the
`Durable execution for coding agents.` tagline.

**Not captured yet:** the PNG screenshots below have not been taken in this
release cycle, so the README does not reference them. Capture them with the
steps below and commit them here; then link them from the README (task detail +
receipt panel as `console-task.png`, the doctor screen as `doctor.png`).

## What to capture

| File | Content | Where it comes from |
| --- | --- | --- |
| `console-task.png` | Web console: task list with one completed task, detail pane open showing the execution receipt (attempts, verification, gates, final result) | Console while/after `scripts/demo-v0.10.sh` runs |
| `execution-receipt.png` | Terminal output of `maestro task receipt <id>` for the demo task | Same run, terminal |
| `doctor.png` | Terminal output of `maestro doctor` in a healthy environment | Any machine with Maestro + one agent CLI installed |

## How to capture (deterministic setup)

1. Set up a clean environment:

   ```sh
   python3.11 -m venv .venv && .venv/bin/python -m pip install -e .
   export MAESTRO_HOME="$HOME/.maestro-demo"      # isolated state, optional
   ```

2. Run the demo in a terminal you will keep open:

   ```sh
   scripts/demo-v0.10.sh
   ```

   The script prints the doctor screen, the delegation, and both receipts —
   capture `execution-receipt.png` (and `doctor.png`) from that terminal output.
   Note: the script removes its temp state on exit; for the console shot you want
   the daemon still running, so instead of letting it finish, run the steps by
   hand (see `docs/demo-v0.10.md`, "Replaying pieces by hand") or re-run the demo
   and keep a second copy of the state dir.

3. For `console-task.png`: with the daemon from step 2 still running, open
   `http://127.0.0.1:<port>` (the port is printed on daemon start and in
   `$MAESTRO_HOME/daemon.json`). Click the completed task; the detail pane shows
   the receipt. Screenshot the window at a comfortable width (≈1280 px).

4. For `doctor.png` on a "real" machine: install Maestro, authenticate one agent
   CLI (e.g. Codex), start the daemon, and run `maestro doctor`. The Agents
   section should show at least one `✓ … — available`.

## Style notes

- Use the same terminal font/size across shots; monospace ≥ 13 px.
- Redact anything that looks like a real path to a private project, an API key,
  or an account name (the demo uses temp paths, so this is mostly a non-issue).
- Keep each image under ~500 KB (resize if needed); they live in git.
