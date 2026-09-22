# Maestro flagship demo — 90-second terminal recording (VHS tape).
#
# Drives the REAL CLI through VHS (headless Chrome + ttyd), paced to the
# storyboard in docs/demo-v0.10.md. Every visible line is genuine output of
# a real run; only the timing is presentation. The setup phase (fake agents,
# config, daemon) runs hidden via scripts/demo-gif-setup.sh so the screen
# shows only the product story: doctor -> delegate -> verify/fix bounce ->
# receipt -> daemon stop -> durable receipt.
#
# Render with:  vhs scripts/demo-gif.tape
#   (VHS needs ffmpeg + ttyd on PATH; VHS_NO_SANDBOX=1 helps in nested
#   sandbox environments. Note: stock VHS 0.12.0 has a bug where the
#   ffmpeg step runs with an already-cancelled context and silently
#   produces no GIF — patch video.go/screenshot.go to use exec.Command
#   instead of exec.CommandContext, or build from a fixed tree.)

Output demo.gif
Set Shell "bash"
Set FontFamily Menlo
Set LetterSpacing 0
Set FontSize 18
Set Width 760
Set Height 480
Set Framerate 25
Set TypingSpeed 35ms

Hide
Type @1ms 'cd /Applications/workstation/maestro && export PATH="$PWD/.venv/bin:$PATH" REPO_ROOT=$PWD' Enter
Sleep 300ms
Type @1ms '. scripts/demo-gif-setup.sh' Enter
Sleep 6s
Show

Type 'maestro doctor --workspace "$WS"' Enter
Sleep 11s

Type "maestro delegate --title" "'Add a health-check endpoint'" "--request" "'Implement the change described in the handoff.'" "--mode demo --workspace" '"$WS"' Enter
Sleep 18s

Hide
Type @35ms "TID=$(maestro task list --workspace" '"$WS"' "| python3 -c 'import json,sys;print(json.load(sys.stdin)[0]" '["task_id"]' ")'" ")" Enter
Sleep 500ms
Show

Type 'maestro task receipt "$TID"' Enter
Sleep 22s

Type "kill $(cat $MAESTRO_HOME/daemon.pid)" Enter
Sleep 2s
Type 'maestro task receipt "$TID"' Enter
Sleep 13s

Type "echo 'Durable execution for coding agents.'" Enter
Sleep 9s
