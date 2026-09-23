"""Daemon lifecycle management for ``maestro daemon start|stop|status|restart``.

The user-facing lifecycle manager. The low-level foreground executable stays
``maestro-daemon`` (see :mod:`maestro.daemon_main`); this module detaches it as a
background process and tracks it through the existing ``daemon.json`` marker in
the Maestro state directory (``~/.maestro`` by default).

Design notes:
- The marker written by the daemon itself (pid, port, host, token, started_at)
  is the single source of truth; this module never keeps a second copy.
- Liveness = the recorded pid answers signal 0, the process is confirmed to be
  the daemon that wrote the marker, **and** the HTTP endpoint answers. A stale
  marker (a dead process, or a live process that is not the daemon) is reported
  as stopped and is cleaned up by ``stop`` (or ignored, read-only, by ``status``).
- Identity: a running daemon holds an exclusive lock on
  ``<state_dir>/daemon.owner.lock`` for its whole life and writes its pid into
  that file. The operating system releases the lock when the process exits, so
  "the lock is held and the file names the marker's pid" proves that the pid is
  still the daemon, even after a crash left the marker behind and the pid was
  reused. A marker written by an older version (no ``owner_lock`` field) is
  confirmed through its HTTP endpoint's agent card. A card that names a pid and
  state directory (this version adds them) must name the marker's pid and this
  state directory. A card without them comes from a released version (0.12.0
  and earlier); then the marker's pid must be a process whose command line is a
  Maestro daemon or MCP server. ``stop`` never signals a process whose identity
  it cannot confirm, it confirms the process again before it escalates to
  SIGKILL, and it never removes the marker while a daemon holds the owner lock.
- Start is guarded by an exclusive advisory lock on ``<state_dir>/daemon.lock``
  so two concurrent starts for the same state directory cannot fork duplicates.
- The child runs in its own session (``start_new_session=True``) so it survives
  the shell that launched it; output goes to ``<state_dir>/daemon.log``.
"""

from __future__ import annotations

import contextlib
import fcntl
import http.client
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_STOP_GRACE_S = 10.0
READY_TIMEOUT_S = 20.0
LOCK_WAIT_S = 30.0
OWNER_LOCK_NAME = "daemon.owner.lock"  # held by the daemon that serves HTTP and owns daemon.json
USERS_LOCK_NAME = "daemon.users.lock"  # held (shared) by every daemon process using the state directory
OWNER_LOCK_WAIT_S = 2.0  # covers the moment a status check holds the lock to test it


class DaemonAlreadyRunning(RuntimeError):
    """Raised when a daemon starts on a state directory that a live daemon already owns."""


def _state_dir() -> Path:
    from .core import maestro_user_dir

    return maestro_user_dir()


@dataclass
class DaemonInfo:
    """Resolved state of the daemon for one Maestro state directory."""

    running: bool
    pid: int | None = None
    port: int | None = None
    host: str | None = None
    url: str | None = None
    token: str | None = None
    state_dir: Path | None = None
    started_at: str | None = None
    uptime_s: float | None = None
    stale_marker: bool = False
    detail: str = ""
    already_running: bool = False  # set by start() when it found a live daemon

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "running": self.running,
            "already_running": self.already_running,
            "pid": self.pid,
            "port": self.port,
            "host": self.host,
            "url": self.url,
            "state_dir": str(self.state_dir) if self.state_dir is not None else None,
            "started_at": self.started_at,
            "uptime_s": round(self.uptime_s, 1) if self.uptime_s is not None else None,
        }
        if self.detail:
            data["detail"] = self.detail
        return data


def _read_marker(state_dir: Path) -> dict[str, Any] | None:
    """Parse the daemon.json marker; ``None`` when absent or unreadable."""
    path = state_dir / "daemon.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    except OSError:
        return False
    return True


def open_lock(path: Path) -> int:
    """Open (creating if needed) a lock file readable by the owner only."""
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)


def try_exclusive(fd: int) -> bool:
    """Take an exclusive lock on ``fd`` without waiting; False when someone else holds it."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def hold_shared(fd: int) -> None:
    """Hold a shared lock on ``fd``, waiting while another process holds it exclusively."""
    fcntl.flock(fd, fcntl.LOCK_SH)


def acquire_owner_lock(state_dir: Path, wait_s: float = OWNER_LOCK_WAIT_S) -> int | None:
    """Take the owner lock for ``state_dir`` and record this process's pid in it.

    Returns the open file descriptor, which must stay open for as long as the
    daemon runs, or None when another process still holds the lock after
    ``wait_s`` seconds.
    """
    fd = open_lock(state_dir / OWNER_LOCK_NAME)
    deadline = time.monotonic() + wait_s
    while not try_exclusive(fd):
        if time.monotonic() >= deadline:
            os.close(fd)
            return None
        time.sleep(0.05)
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
    return fd


def release_owner_lock(fd: int) -> None:
    """Release the owner lock; closing the descriptor drops the lock."""
    os.close(fd)


def owner_lock_holder(state_dir: Path) -> int | None:
    """The pid recorded by the process holding the owner lock.

    Returns None when no process holds the lock, and 0 when a process holds it
    but the recorded pid cannot be read.
    """
    try:
        fd = os.open(str(state_dir / OWNER_LOCK_NAME), os.O_RDONLY)
    except OSError:
        return None
    try:
        if try_exclusive(fd):
            return None  # nobody held it; closing the descriptor releases it again
        try:
            return int(os.pread(fd, 32, 0).decode("ascii").strip())
        except ValueError:
            return 0
    finally:
        os.close(fd)


# Anything that can go wrong while talking to a port that may not be a Maestro
# daemon at all: refused or reset connections, timeouts, a reply that is not
# HTTP (http.client.BadStatusLine and the other HTTPException types), and a
# body that is not JSON. Each one means "this is not a daemon that answers".
_PORT_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError)


# Words in the command line of a process that runs a Maestro daemon: the
# daemon itself, or an MCP server with the daemon inside it (released versions).
_MAESTRO_COMMANDS = ("maestro-daemon", "maestro.daemon_main", "maestro-mcp", "maestro.mcp_server")


def _answers_as_maestro(url: str, token: str | None, pid: int, state_dir: Path, timeout: float = 3.0) -> bool:
    """True when ``url`` serves the agent card of the Maestro daemon ``pid`` for ``state_dir``.

    Used for markers from older versions, which carry no owner lock. Any
    Maestro daemon answers with an agent card, so more is needed to tie the
    card to the marker's pid. A card with a ``maestro`` block (this version)
    must name the marker's pid and this state directory; otherwise the port
    belongs to another daemon. A card without one comes from a released
    version, which does not report its pid; then the pid must be a process
    whose command line runs a Maestro daemon, so that after an upgrade the old
    daemon is still found and stopped instead of left running beside a new one.
    """
    request = urllib.request.Request(url + "/.well-known/agent.json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            card = json.loads(resp.read())
    except _PORT_ERRORS:
        return False
    if not (isinstance(card, dict) and "capabilities" in card and "url" in card):
        return False
    identity = card.get("maestro")
    if identity is None:
        command = process_command(pid) or ""
        return any(word in command for word in _MAESTRO_COMMANDS)
    if not isinstance(identity, dict) or not isinstance(identity.get("state_dir"), str):
        return False
    return identity.get("pid") == pid and os.path.realpath(identity["state_dir"]) == os.path.realpath(state_dir)


def _identity_confirmed(base: Path, marker: dict[str, Any], pid: int, url: str | None) -> bool:
    """True when the live process ``pid`` is the daemon that wrote this marker.

    A current daemon proves it by holding the owner lock with its pid recorded.
    A marker from an older version carries no ``owner_lock`` field, so the
    evidence is its HTTP endpoint answering with an agent card that names the
    same pid and state directory.
    """
    if marker.get("owner_lock"):
        return owner_lock_holder(base) == pid
    return url is not None and _answers_as_maestro(url, marker.get("token"), pid, base)


_PROC = Path("/proc")  # Linux process information; absent on macOS


def _proc_available() -> bool:
    return (_PROC / "self" / "stat").exists()


def _ps(field: str, pid: int) -> str | None:
    """One ``ps`` field for ``pid``, or None when ps cannot report it.

    The environment pins the time zone and the locale, so the output is the
    same whatever TZ or LANG the caller runs with.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, env={**os.environ, "TZ": "UTC0", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def process_start_token(pid: int) -> str | None:
    """A value that names process ``pid`` for its whole life; None when it cannot be read.

    A pid alone can be reused by a new process after the old one exits. The
    pair (pid, start time) names one process, so it is recorded with a task's
    runner and re-checked before ``stop`` sends SIGKILL. On Linux the start
    time is field 22 of ``/proc/<pid>/stat`` (clock ticks since boot), prefixed
    with the boot id so a reboot never repeats a value. Elsewhere it is the
    start time ``ps`` reports, read in UTC.
    """
    if not _proc_available():
        return _ps("lstart", pid)
    try:
        stat_line = (_PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        # The command name (field 2) is in parentheses and may contain spaces
        # or parentheses itself, so the fields are counted after the last ")".
        fields = stat_line.rsplit(")", 1)[1].split()
        start_ticks = fields[19]  # field 22; fields[0] is field 3
    except (OSError, IndexError):
        return None
    try:
        boot_id = (_PROC / "sys" / "kernel" / "random" / "boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return start_ticks
    return f"{boot_id}:{start_ticks}"


def process_command(pid: int) -> str | None:
    """The command line of process ``pid``, or None when it cannot be read."""
    if not _proc_available():
        return _ps("command", pid)
    try:
        raw = (_PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def runner_state(runner: dict[str, Any]) -> str:
    """Whether the process recorded in a task's ``runner`` field still runs.

    ``runner`` holds ``pid`` and ``started`` (see :func:`process_start_token`).
    Returns "gone" when the pid is dead, or alive with a different start time
    (another process reused the pid). Returns "alive" when the start time
    matches, or when none was recorded and the pid is alive. Returns
    "unknown" when a start time was recorded but cannot be read now (ps
    missing, failing or timing out): the runner may still be alive.
    """
    pid = int(runner["pid"])
    if not _pid_alive(pid):
        return "gone"
    started = runner.get("started")
    if started is None:
        return "alive"
    now = process_start_token(pid)
    if now is None:
        return "unknown"
    return "alive" if now == started else "gone"


def probe(url: str, timeout: float = 3.0) -> bool:
    """True when the daemon's HTTP endpoint answers (public static route)."""
    try:
        with urllib.request.urlopen(url + "/", timeout=timeout):
            return True
    except _PORT_ERRORS:
        return False


def _uptime_s(started_at: str | None) -> float | None:
    if not started_at:
        return None
    try:
        created = datetime.fromisoformat(str(started_at))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
    except ValueError:
        return None


def status(state_dir: Path | None = None) -> DaemonInfo:
    """Resolve the daemon state for one state directory (read-only).

    Distinguishes: no marker (stopped), marker with a dead pid (stale), a live
    pid that is not the daemon (stale: the pid was reused), and a live,
    answering daemon. A confirmed daemon that does not answer HTTP is reported
    as not running with an explanatory detail so callers never trust a hung
    process.
    """
    base = state_dir or _state_dir()
    if not (base / "daemon.json").is_file():
        return DaemonInfo(running=False, state_dir=base, detail="no daemon marker")
    marker = _read_marker(base)
    if marker is None:
        return DaemonInfo(running=False, state_dir=base, stale_marker=True, detail="malformed daemon marker")
    try:
        pid = int(marker["pid"])
        port = int(marker.get("port") or 0)
    except (KeyError, TypeError, ValueError):
        return DaemonInfo(running=False, state_dir=base, stale_marker=True, detail="malformed daemon marker")
    host = str(marker.get("host") or "127.0.0.1")
    url = f"http://{host}:{port}" if port else None
    started_at = marker.get("started_at")
    if not _pid_alive(pid):
        return DaemonInfo(
            running=False, pid=pid, port=port or None, host=host, url=url,
            state_dir=base, started_at=str(started_at) if started_at else None,
            stale_marker=True, detail="daemon marker exists but the process is dead",
        )
    if not _identity_confirmed(base, marker, pid, url):
        return DaemonInfo(
            running=False, pid=pid, port=port or None, host=host, url=url,
            state_dir=base, started_at=str(started_at) if started_at else None,
            stale_marker=True,
            detail=(
                f"pid {pid} is alive but is not the Maestro daemon that wrote the marker "
                "(the pid was probably reused after a crash); the marker is stale"
            ),
        )
    if url is None or not probe(url):
        return DaemonInfo(
            running=False, pid=pid, port=port or None, host=host, url=url,
            state_dir=base, started_at=str(started_at) if started_at else None,
            detail="daemon process is alive but its HTTP endpoint does not answer",
        )
    return DaemonInfo(
        running=True, pid=pid, port=port or None, host=host, url=url,
        token=marker.get("token"), state_dir=base,
        started_at=str(started_at) if started_at else None,
        uptime_s=_uptime_s(str(started_at) if started_at else None),
    )


def live_owner(state_dir: Path) -> DaemonInfo | None:
    """The live daemon that owns ``state_dir``, or None when no daemon owns it.

    A confirmed daemon that has stopped answering HTTP still owns the directory:
    starting a second daemon beside it would make two brokers share one state.
    """
    info = status(state_dir)
    if info.running or (not info.stale_marker and info.pid is not None):
        return info
    return None


@contextlib.contextmanager
def _start_lock(state_dir: Path, wait_s: float = LOCK_WAIT_S) -> Iterator[None]:
    """Exclusive advisory lock serializing concurrent starts for one state dir."""
    path = state_dir / "daemon.lock"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not acquire daemon start lock within {wait_s}s") from None
                time.sleep(0.1)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _spawn_command(state_dir: Path) -> list[str]:
    """Command that runs the foreground daemon entry point in a fresh interpreter."""
    return [sys.executable, "-m", "maestro.daemon_main", "--state-dir", str(state_dir)]


def start(
    state_dir: Path | None = None,
    *,
    ready_timeout_s: float = READY_TIMEOUT_S,
) -> DaemonInfo:
    """Start the daemon detached, or return the existing one when it is already up.

    Never starts a second daemon for the same state directory: a live daemon is
    detected first (under the start lock), and the lock keeps concurrent callers
    from racing past that check.
    """
    base = state_dir or _state_dir()
    base.mkdir(parents=True, exist_ok=True)
    with _start_lock(base):
        existing = status(base)
        if existing.running:
            existing.already_running = True
            return existing
        if not existing.stale_marker and existing.pid is not None:
            # Live pid that does not answer HTTP: do not stack a second daemon.
            raise RuntimeError(
                f"a daemon process (pid {existing.pid}) exists but is not answering; "
                f"check {base / 'daemon.log'} or run 'maestro daemon stop' first"
            )
        log_path = base / "daemon.log"
        env = dict(os.environ)
        env["MAESTRO_HOME"] = str(base)  # the child pins its state to this directory
        log_file = log_path.open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                _spawn_command(base),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(base),
                env=env,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"failed to launch the maestro daemon: {exc}") from exc
        finally:
            log_file.close()
        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            info = status(base)
            if info.running:
                return info
            if process.poll() is not None:
                # Our child died while starting (e.g. port in use): surface the log tail.
                break
            time.sleep(0.2)
        else:
            _terminate(process, grace_s=DEFAULT_STOP_GRACE_S)
            raise RuntimeError(f"daemon did not become ready within {ready_timeout_s}s; see {log_path}")
        tail = _tail(log_path, lines=15)
        _terminate(process, grace_s=DEFAULT_STOP_GRACE_S)
        raise RuntimeError(f"daemon exited while starting (pid {process.pid}); log tail:\n{tail}")


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def _terminate(process: subprocess.Popen, grace_s: float) -> None:
    """SIGTERM a process (and its session), escalating to SIGKILL after the grace."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        with contextlib.suppress(OSError):
            process.terminate()
    try:
        process.wait(timeout=grace_s)
        return
    except subprocess.SubprocessError:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        with contextlib.suppress(OSError):
            process.kill()


def stop(state_dir: Path | None = None, *, grace_s: float | None = None) -> DaemonInfo:
    """Gracefully stop the daemon; idempotent — nothing running is not an error.

    SIGTERM first, then a grace period (``MAESTRO_DAEMON_STOP_GRACE_S`` or the
    argument), then SIGKILL if required. Stale markers are cleaned up either way.
    A process is only signalled when it is confirmed to be the daemon that wrote
    the marker; a live process with a reused pid is left alone and the stale
    marker is removed. The marker is never removed while a daemon holds the
    owner lock: that daemon is running and the marker is (or is about to be)
    its own.
    """
    base = state_dir or _state_dir()
    if grace_s is None:
        raw = os.environ.get("MAESTRO_DAEMON_STOP_GRACE_S", "")
        try:
            grace_s = float(raw) if raw.strip() else DEFAULT_STOP_GRACE_S
        except ValueError:
            grace_s = DEFAULT_STOP_GRACE_S
    if not (base / "daemon.json").is_file():
        return DaemonInfo(running=False, state_dir=base, detail="no daemon running")
    marker = _read_marker(base)
    if marker is None:
        return DaemonInfo(running=False, state_dir=base, stale_marker=True, detail=_remove_stale_marker(base, "the daemon marker was malformed"))
    try:
        pid = int(marker["pid"])
        port = int(marker.get("port") or 0)
    except (KeyError, TypeError, ValueError):
        return DaemonInfo(running=False, state_dir=base, stale_marker=True, detail=_remove_stale_marker(base, "the daemon marker was malformed"))
    host = str(marker.get("host") or "127.0.0.1")
    url = f"http://{host}:{port}" if port else None
    if not _pid_alive(pid):
        return DaemonInfo(
            running=False, pid=pid, port=port or None, host=host, url=url, state_dir=base,
            stale_marker=True, detail=_remove_stale_marker(base, "daemon was not running; the marker was stale"),
        )
    if not _identity_confirmed(base, marker, pid, url):
        return DaemonInfo(
            running=False, pid=pid, port=port or None, host=host, url=url, state_dir=base,
            stale_marker=True,
            detail=_remove_stale_marker(
                base,
                f"stale daemon marker: pid {pid} is alive but is not the Maestro daemon "
                "for this state directory (the pid was probably reused after a crash), so it was not signalled",
            ),
        )
    # Recorded now, while the pid is confirmed to be the daemon, so the
    # process can be recognised again before SIGKILL.
    started = process_start_token(pid)
    deadline = time.monotonic() + grace_s
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass  # died between the liveness check and the signal
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    if _pid_alive(pid):
        # The grace period is long enough for the daemon to exit and for its
        # pid to be reused, so confirm again that it is still the same process.
        # A start time that cannot be read now (ps failing or timing out) is
        # not evidence of another process, so then the owner lock (or, for an
        # older marker, the agent card) decides, as it did before SIGTERM.
        now = process_start_token(pid) if started is not None else None
        if now is not None:
            same_process = now == started
        else:
            same_process = _identity_confirmed(base, marker, pid, url)
        if not same_process:
            return DaemonInfo(
                running=False, pid=pid, port=port or None, host=host, url=url, state_dir=base,
                detail=_remove_stale_marker(
                    base,
                    f"pid {pid} was still alive after the grace period but could no longer be confirmed "
                    "as the daemon, so it was not force-killed",
                ),
            )
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        with contextlib.suppress(OSError):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _pid_alive(pid):
                time.sleep(0.1)
    # The daemon removes its own marker on a clean stop; remove any leftover
    # (unless a new daemon already took the directory over).
    _remove_stale_marker(base, "")
    return DaemonInfo(running=False, pid=pid, port=port or None, host=host, url=url, state_dir=base, detail="daemon stopped")


def _remove_stale_marker(base: Path, detail: str) -> str:
    """Remove ``daemon.json`` unless a daemon holds the owner lock.

    Returns ``detail`` followed by what happened to the marker.
    """
    if owner_lock_holder(base) is not None:
        return f"{detail}; the marker was kept because a daemon holds {OWNER_LOCK_NAME}"
    (base / "daemon.json").unlink(missing_ok=True)
    return f"{detail}; the marker was removed"


def restart(state_dir: Path | None = None, *, grace_s: float | None = None, ready_timeout_s: float = READY_TIMEOUT_S) -> DaemonInfo:
    """Stop (if anything is running) and start a fresh daemon."""
    stop(state_dir=state_dir, grace_s=grace_s)
    return start(state_dir=state_dir, ready_timeout_s=ready_timeout_s)
