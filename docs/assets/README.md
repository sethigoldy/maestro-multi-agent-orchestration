# Launch assets

This directory holds images for the README and release notes.

**Checked in:** `demo-v0.10.gif` — a 90-second terminal recording of a real
`scripts/demo-v0.10.sh` run (doctor → work-mode delegation → verify-fix bounce
→ durable receipt), embedded in the README. It was produced by driving the
real CLI through [VHS](https://github.com/charmbracelet/vhs) with the demo's
output paced to the storyboard in `docs/demo-v0.10.md`; every line shown is
genuine output from a real run (only the timing is presentation).

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
