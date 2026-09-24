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
import signal
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


def _terminal_width(stdout: Any, fallback: int = 80) -> int:
    """Best-effort terminal width for frame layout.

    Resolution order: ``$COLUMNS`` (explicit user intent), then a TIOCGWINSZ
    query on the stdout fd, then a sane default. The result is clamped — an
    over-wide guess makes every line wrap and destroys the layout."""
    cols = os.environ.get("COLUMNS")
    if cols:
        try:
            value = int(cols)
            if 20 <= value <= 500:
                return value
        except ValueError:
            pass
    try:
        size = os.get_terminal_size(stdout.fileno())
        if 20 <= size.columns <= 500:
            return size.columns
    except (OSError, ValueError, AttributeError):
        pass
    return fallback


def _install_winch_handler(stdout: Any, width_holder: dict[str, int], wake_fd: int):
    """Re-query the terminal width on SIGWINCH and wake the render loop.

    Returns a restore callable (a no-op when signals are unavailable, e.g.
    ``run`` called from a non-main thread)."""
    def _on_winch(signum, frame):
        width_holder["width"] = _terminal_width(stdout)
        try:
            os.write(wake_fd, b"w")  # wake the select() loop for a redraw
        except OSError:
            pass

    try:
        previous = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, _on_winch)
    except (ValueError, OSError):
        return lambda: None
    return lambda: signal.signal(signal.SIGWINCH, previous)


def normalize(record: dict[str, Any]) -> dict[str, Any]:
    """Map an A2A-shaped /tasks record onto the flat shape the TUI renders."""
    meta = record.get("metadata") or {}
    return {
        "task_id": record.get("id"),
        "title": meta.get("title"),
        "state": (record.get("status") or {}).get("state") or "unknown",
        "workspace": meta.get("workspace"),
        "run_dir": meta.get("run_dir"),
        "branch": meta.get("branch"),
        "origin_agent": meta.get("origin_agent"),
        "target_agent": meta.get("target_agent"),
        "usage": meta.get("usage") or None,
        "attempts": meta.get("attempts") or [],
        "error": meta.get("error") or None,
    }


def load_task(url: str, task_id: str, token: str | None = None) -> dict[str, Any] | None:
    """One task's record from the daemon (JSON-RPC ``tasks/get``), or None when
    the daemon does not know the task."""
    from .a2a_client import post_jsonrpc

    result = post_jsonrpc(url, "tasks/get", {"id": task_id}, timeout=10, token=token)
    task = (result or {}).get("task")
    return normalize(task) if isinstance(task, dict) else None


def _draw(stdout: Any, frame: str) -> None:
    """Write one frame with "\r\n" line breaks.

    The dashboard runs the terminal in raw mode, which turns off the
    terminal's own "\n" to "\r\n" translation. A bare "\n" would move the
    cursor down without returning it to column 0, so every row would start
    where the previous row ended."""
    stdout.write(frame.replace("\n", "\r\n"))


def load_tasks(url: str, token: str | None = None) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(f"{url}/tasks", headers=headers), timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [normalize(r) for r in body.get("tasks") or []]


def _state_color(state: str) -> str:
    return _STATE_COLORS.get(state, DIM)


def render_frame(tasks: list[dict[str, Any]], selected: int | None, live: bool, width: int = 100) -> str:
    """Render one full screen (ANSI). Pure function — trivially testable."""
    width = max(40, int(width))  # clamp: tiny widths would produce negative columns
    lines: list[str] = []
    counts: dict[str, int] = {}
    for task in tasks:
        state = task.get("state") or "unknown"
        counts[state] = counts.get(state, 0) + 1
    summary = " ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no tasks"
    if len(summary) > max(10, width - 30):
        summary = summary[: max(10, width - 31)] + "…"
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
        detail_title = str(sel.get("title") or sel.get("task_id"))[: max(10, width - 12)]
        lines.append(f"{BOLD}{detail_title}{RESET} {_state_color(state)}[{state}]{RESET}")
        for label, value in (
            ("route", f"{sel.get('origin_agent') or '?'} → {sel.get('target_agent') or '?'}"),
            ("workspace", sel.get("workspace")),
            # Shown only for a task that runs in its own worktree.
            ("run dir", sel.get("run_dir") if sel.get("run_dir") not in (None, sel.get("workspace")) else None),
            ("branch", sel.get("branch")),
            ("error", (sel.get("error") or "").splitlines()[0] if sel.get("error") else None),
        ):
            if value:
                # Plain-text truncation before the ANSI wrapping keeps the whole
                # line inside the terminal width (no mid-line wrapping).
                lines.append(f"  {DIM}{label}:{RESET} {str(value)[: max(10, width - len(label) - 6)]}")
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


_ARROW_UP = b"\x1b[A"
_ARROW_DOWN = b"\x1b[B"
# In application cursor mode a terminal sends the arrow keys as ESC O A and
# ESC O B instead. The reader turns those into the usual ESC [ forms.
_APP_ARROWS = {b"\x1bOA": _ARROW_UP, b"\x1bOB": _ARROW_DOWN}
# How long to wait for the rest of an escape sequence after an ESC byte. A
# terminal sends an arrow key's three bytes together; a lone ESC is the Esc key.
_ESCAPE_WAIT_S = 0.05


class _KeyReader:
    """Read key presses from ``fd``, one key per ``read`` call.

    Most keys are one byte. The arrow keys arrive as ESC [ A and ESC [ B (or
    ESC O A and ESC O B). If only the ESC byte were read, the dashboard would
    take it as the Esc key and quit. So after an ESC byte the reader waits a
    short time for each further byte of the sequence, and never waits longer
    than that: a sequence that stops early is returned as it is, and the loop
    ignores it.

    When the byte after ESC does not start a sequence (Esc pressed twice, or
    Alt plus a key), the reader returns a lone ESC and keeps that byte for the
    next ``read`` call, so the byte is not lost.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._kept = b""

    @property
    def pending(self) -> bool:
        """True when a kept byte is waiting; stdin will not report it as ready."""
        return bool(self._kept)

    def _next_byte(self) -> bytes:
        if self._kept:
            byte, self._kept = self._kept, b""
            return byte
        return os.read(self.fd, 1)

    def _next_byte_soon(self) -> bytes:
        """Return the next byte if it arrives within the escape wait, else b"".

        Only called after ``_next_byte`` in the same ``read``, which has
        already used up any kept byte, so this reads from ``fd`` directly.
        """
        ready, _, _ = select.select([self.fd], [], [], _ESCAPE_WAIT_S)
        return os.read(self.fd, 1) if ready else b""

    def read(self) -> bytes:
        """Return the next key. Returns b"" at end of input."""
        key = self._next_byte()
        if key != b"\x1b":
            return key
        second = self._next_byte_soon()
        if second not in (b"[", b"O"):
            self._kept = second  # b"" when nothing followed: the Esc key on its own
            return key
        sequence = key + second + self._next_byte_soon()
        return _APP_ARROWS.get(sequence, sequence)


class _State:
    """Mutable task table shared between the reader thread and the render loop."""

    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.selected: int | None = 0
        self.live = False
        self.lost = False
        # Fetches one task's full record by id. Set by run() so that a task
        # first seen through an event gets its title, agents and workspace.
        self.fetch: Callable[[str], dict[str, Any] | None] | None = None

    def set_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """Replace the task table and rebuild the index that events use to find rows.

        Events are matched to rows through ``by_id``. If the index were not
        rebuilt here, an event for a task loaded from ``GET /tasks`` would add
        a second, untitled row, and the loaded row would never change again.
        """
        self.tasks = tasks
        self.by_id = {str(task["task_id"]): task for task in tasks if task.get("task_id")}

    def apply_event(self, task_id: str, type_: str, data: dict[str, Any]) -> None:
        if not task_id:
            return
        task = self.by_id.get(task_id)
        if task is None:
            # A task delegated after the dashboard opened. Its events carry no
            # title or agents, so ask the daemon for its record. If that fails,
            # the row still appears, showing the task id.
            record = None
            if self.fetch is not None:
                try:
                    record = self.fetch(task_id)
                except (ValueError, OSError):
                    record = None
            task = record or {"task_id": task_id, "state": "submitted"}
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
        elif type_ == "branch":
            task["branch"] = data.get("branch") or task.get("branch")

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
    state.fetch = lambda task_id: load_task(url, task_id, token=token)
    try:
        state.set_tasks(load_tasks(url, token=token))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"cannot reach the daemon at {url}: {exc}", file=sys.stderr)
        return 1

    read_fd, write_fd = os.pipe()
    done = threading.Event()
    width_holder = {"width": _terminal_width(stdout)}
    restore_winch = lambda: None  # noqa: E731 — replaced once the handler is installed
    try:
        restore_winch = _install_winch_handler(stdout, width_holder, write_fd)
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
        keys = _KeyReader(stdin_fd)
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
        # Draw the tasks loaded above straight away. Later frames are drawn on
        # daemon events and key presses, and an idle daemon sends no events.
        _draw(stdout, render_frame(state.tasks, state.selected, state.live, width=width_holder["width"]))
        stdout.flush()
        exit_code = 0
        try:
            while True:
                if keys.pending:
                    # A byte kept back by the key reader is already read from
                    # stdin, so select() would not report it. Handle it first.
                    ready = [stdin_fd]
                else:
                    try:
                        ready, _, _ = select.select([stdin_fd, read_fd], [], [])
                    except KeyboardInterrupt:
                        break  # Ctrl-C (raw mode passes ^C through as a byte too)
                if stdin_fd in ready:
                    key = keys.read()
                    if not key:
                        exit_code = 1  # stdin EOF (terminal closed)
                        break
                    if key in (b"q", b"\x1b", b"\x03"):
                        exit_code = 0
                        break
                    if key in (b"\x7f", b"k", _ARROW_UP):
                        state.selected = max(0, (state.selected or 0) - 1)
                    elif key in (b"j", b"\n", _ARROW_DOWN):
                        state.selected = min(len(state.tasks) - 1, (state.selected or 0) + 1)
                    else:
                        continue
                    _draw(stdout, render_frame(state.tasks, state.selected, state.live, width=width_holder["width"]))
                    stdout.flush()
                if read_fd in ready:
                    os.read(read_fd, 64)  # drain; one redraw per wakeup
                    state.clamp_selection()
                    _draw(stdout, render_frame(state.tasks, state.selected, state.live, width=width_holder["width"]))
                    stdout.flush()
                    if done.is_set():
                        exit_code = 1  # the stream ended (daemon stopped)
                        break
        finally:
            done.set()
            stdout.write("\x1b[?25h\x1b[?1049l")  # show cursor, leave alt screen
            stdout.flush()
            if old_termios is not None:
                try:
                    termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_termios)
                except termios.error:
                    pass
        return exit_code
    finally:
        restore_winch()
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
