"""Terminal dashboard for the Maestro broker: ``maestro dashboard``.

Strictly event-driven like the web console: one initial ``GET /tasks`` fetch,
then a single SSE stream over ``GET /events``. The reader thread pushes each
event through a pipe; the main loop blocks on ``select(stdin, pipe)`` — there
is no redraw timer and no polling of task state. Stdlib only (ANSI escapes).
"""

from __future__ import annotations

import http.client
import json
import os
import select
import sys
import termios
import threading
import tty
import urllib.error
import urllib.request
from typing import Any, Callable

TRANSCRIPT_TAIL = 12
MAX_TASKS_SHOWN = 50

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"

_STATE_COLORS = {
    "submitted": BLUE,
    "working": CYAN,
    "input-required": YELLOW,
    "completed": GREEN,
    "failed": RED,
    "canceled": RED,
}


def normalize(record: dict[str, Any]) -> dict[str, Any]:
    """Map an A2A-shaped /tasks record onto the flat shape the TUI renders."""
    meta = record.get("metadata") or {}
    return {
        "task_id": record.get("id"),
        "title": meta.get("title"),
        "state": (record.get("status") or {}).get("state") or "unknown",
        "workspace": meta.get("workspace"),
        "branch": meta.get("branch"),
        "origin_agent": meta.get("origin_agent"),
        "target_agent": meta.get("target_agent"),
        "usage": meta.get("usage") or None,
        "attempts": meta.get("attempts") or [],
        "error": meta.get("error") or None,
    }


def load_tasks(url: str, token: str | None = None) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(f"{url}/tasks", headers=headers), timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [normalize(r) for r in body.get("tasks") or []]


def _state_color(state: str) -> str:
    return _STATE_COLORS.get(state, DIM)


def render_frame(tasks: list[dict[str, Any]], selected: int | None, live: bool, width: int = 100) -> str:
    """Render one full screen (ANSI). Pure function — trivially testable."""
    lines: list[str] = []
    counts: dict[str, int] = {}
    for task in tasks:
        state = task.get("state") or "unknown"
        counts[state] = counts.get(state, 0) + 1
    summary = " ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no tasks"
    dot = f"{GREEN}●{RESET}" if live else f"{RED}○{RESET}"
    lines.append(f"{BOLD}MAESTRO{RESET} {DIM}dashboard{RESET}  {dot}  {summary}")
    lines.append("")

    shown = tasks[:MAX_TASKS_SHOWN]
    for index, task in enumerate(shown):
        marker = f"{BOLD}▸{RESET}" if index == selected else " "
        state = task.get("state") or "unknown"
        title = (task.get("title") or task.get("task_id") or "?")[: max(10, width - 46)]
        target = (task.get("target_agent") or "-")[:8]
        cost = ""
        usage = task.get("usage") or {}
        if isinstance(usage.get("cost_usd"), (int, float)):
            cost = f" ${usage['cost_usd']:.3f}"
        lines.append(
            f"{marker} {DIM}{str(task.get('task_id') or '')[-14:]}{RESET} "
            f"{title:<{max(10, width - 46)}} {target:<8} {_state_color(state)}{state}{RESET}{cost}"
        )
    if not tasks:
        lines.append(f"  {DIM}no tasks yet — delegate one from any agent or the CLI{RESET}")
    if len(tasks) > MAX_TASKS_SHOWN:
        lines.append(f"  {DIM}… and {len(tasks) - MAX_TASKS_SHOWN} more{RESET}")

    sel = tasks[selected] if selected is not None and 0 <= selected < len(tasks) else None
    if sel is not None:
        lines.append("")
        state = sel.get("state") or "unknown"
        lines.append(
            f"{BOLD}{sel.get('title') or sel.get('task_id')}{RESET} "
            f"{_state_color(state)}[{state}]{RESET}"
        )
        for label, value in (
            ("route", f"{sel.get('origin_agent') or '?'} → {sel.get('target_agent') or '?'}"),
            ("workspace", sel.get("workspace")),
            ("branch", sel.get("branch")),
            ("error", (sel.get("error") or "").splitlines()[0] if sel.get("error") else None),
        ):
            if value:
                lines.append(f"  {DIM}{label}:{RESET} {value}")
        attempts = sel.get("attempts") or []
        for attempt in attempts[-3:]:
            ok = not attempt.get("error")
            mark = f"{GREEN}ok{RESET}" if ok else f"{RED}fail{RESET}"
            lines.append(f"  {DIM}attempt:{RESET} {attempt.get('agent') or '?'} {mark}")
        transcript = sel.get("transcript") or []
        if transcript:
            lines.append(f"  {DIM}— output (last {min(len(transcript), TRANSCRIPT_TAIL)}) —{RESET}")
            for line in transcript[-TRANSCRIPT_TAIL:]:
                lines.append(f"  {line[: width - 4]}")

    lines.append("")
    lines.append(f"{DIM}q quit · ↑/↓ or j/k select{RESET}")
    return "\x1b[2J\x1b[H" + "\n".join(lines)


class _State:
    """Mutable task table shared between the reader thread and the render loop."""

    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.selected: int | None = 0
        self.live = False
        self.lost = False

    def apply_event(self, task_id: str, type_: str, data: dict[str, Any]) -> None:
        if not task_id:
            return
        task = self.by_id.get(task_id)
        if task is None:
            task = {"task_id": task_id, "state": "submitted"}
            self.by_id[task_id] = task
            self.tasks.insert(0, task)  # newest first
        if type_ == "state":
            task["state"] = data.get("state") or task["state"]
            if data.get("error"):
                task["error"] = data["error"]
        elif type_ == "output":
            line = data.get("line")
            if isinstance(line, str):
                task.setdefault("transcript", []).append(line)
                del task["transcript"][:-2000]
        elif type_ == "usage":
            merged = dict(task.get("usage") or {})
            merged.update(data)
            task["usage"] = merged

    def clamp_selection(self) -> None:
        if self.selected is not None and self.selected >= len(self.tasks):
            self.selected = max(0, len(self.tasks) - 1)


def _read_sse_frames(response, state: _State, signal_write: int, done: threading.Event) -> None:
    """Parse SSE frames from the response; push one pipe byte per event."""
    current_event = "message"
    try:
        buffer = b""
        while not done.is_set():
            # read1 returns as soon as any bytes are available (read(n) would
            # block until n bytes or EOF — wrong for a streaming SSE body).
            # No socket timeout: a dead daemon closes the connection (EOF/RST),
            # and CPython's timed-out-socket state makes retry-after-timeout
            # unreliable.
            try:
                chunk = response.read1(4096)
            except OSError:
                break  # RST/reset or closed socket — treat as stream end
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("event:"):
                    current_event = text[len("event:"):].strip()
                elif text.startswith("data:"):
                    payload = text[len("data:"):].strip()
                    try:
                        envelope = json.loads(payload)
                    except (ValueError, TypeError):
                        continue
                    data = envelope.get("data") or {}
                    state.apply_event(envelope.get("task_id"), current_event, data)
                    try:
                        os.write(signal_write, b"x")
                    except OSError:
                        return  # pipe closed; the main loop is exiting
                elif text == "":
                    pass  # frame separator
    finally:
        state.live = False
        state.lost = True
        try:
            os.write(signal_write, b"q")
        except OSError:
            pass
        done.set()


def run(
    url: str,
    *,
    stdin: Any | None = None,
    stdout: Any | None = None,
    is_tty: Callable[[], bool] | None = None,
    token: str | None = None,
) -> int:
    """Run the dashboard until the user quits. Returns a process exit code."""
    if is_tty is None:
        is_tty = lambda: sys.stdin.isatty() and (sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else True)  # noqa: E731
    if not is_tty():
        print("maestro dashboard needs an interactive terminal (tty)", file=sys.stderr)
        return 2

    stdout = stdout or sys.stdout
    stdin = stdin or sys.stdin.buffer
    state = _State()
    try:
        state.tasks = load_tasks(url, token=token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"cannot reach the daemon at {url}: {exc}", file=sys.stderr)
        return 1

    read_fd, write_fd = os.pipe()
    done = threading.Event()
    try:
        conn = http.client.HTTPConnection(url.split("//", 1)[1], timeout=None)
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            conn.request("GET", "/events", headers=headers)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            print(f"cannot reach the daemon at {url}: {exc}", file=sys.stderr)
            return 1
        if response.status != 200:
            print(f"daemon answered {response.status} on /events", file=sys.stderr)
            return 1
        state.live = True
        reader = threading.Thread(target=_read_sse_frames, args=(response, state, write_fd, done), daemon=True)
        reader.start()

        stdin_fd = stdin.fileno()
        # Put the terminal in raw mode: canonical (line-buffered) input would
        # swallow single keystrokes, and ECHO would paint them over the frame.
        old_termios = None
        if os.isatty(stdin_fd):
            try:
                old_termios = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)
            except termios.error:
                old_termios = None
        stdout.write("\x1b[?1049h\x1b[?25l")  # alt screen, hide cursor
        exit_code = 0
        try:
            while True:
                try:
                    ready, _, _ = select.select([stdin_fd, read_fd], [], [])
                except KeyboardInterrupt:
                    break  # Ctrl-C (raw mode passes ^C through as a byte too)
                if stdin_fd in ready:
                    key = os.read(stdin_fd, 1)
                    if not key:
                        exit_code = 1  # stdin EOF (terminal closed)
                        break
                    if key in (b"q", b"\x1b", b"\x03"):
                        exit_code = 0
                        break
                    if key == b"\x7f" or key == b"k":
                        state.selected = max(0, (state.selected or 0) - 1)
                    elif key in (b"j", b"\n"):
                        state.selected = min(len(state.tasks) - 1, (state.selected or 0) + 1)
                    else:
                        continue
                    stdout.write(render_frame(state.tasks, state.selected, state.live))
                    stdout.flush()
                if read_fd in ready:
                    os.read(read_fd, 64)  # drain; one redraw per wakeup
                    state.clamp_selection()
                    stdout.write(render_frame(state.tasks, state.selected, state.live))
                    stdout.flush()
                    if done.is_set():
                        exit_code = 1  # the stream ended (daemon stopped)
                        break
        finally:
            done.set()
            stdout.write("\x1b[?25l\x1b[?1049l")  # show cursor, leave alt screen
            stdout.flush()
            if old_termios is not None:
                try:
                    termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_termios)
                except termios.error:
                    pass
        return exit_code
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass
        try:
            os.close(read_fd)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    from .cli import _daemon_token, _daemon_url

    try:
        url = _daemon_url()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return run(url, token=_daemon_token())
