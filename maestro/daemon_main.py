"""Console entry point for the Maestro daemon (``maestro-daemon``)."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from typing import Any


def run_daemon(state_dir: str | None = None, port: int = 0, bind: str = "127.0.0.1", allowed_origins: list[str] | None = None) -> dict[str, Any]:
    """Start the daemon (HTTP on ``bind``; token-auth when non-loopback) and return its connection info.

    ``allowed_origins`` of None means the daemon reads MAESTRO_DAEMON_ALLOWED_ORIGINS.
    """
    from .daemon import MaestroDaemon

    daemon = MaestroDaemon(state_dir=state_dir, start_http=True, port=port, bind=bind, allowed_origins=allowed_origins)
    info: dict[str, Any] = {"pid": os.getpid(), "port": daemon.port, "bind": daemon.bind, "advertised_host": daemon.advertised_host, "state_dir": str(daemon.state_dir), "daemon": daemon}
    if daemon.token is not None:
        info["token"] = daemon.token
    return info


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="maestro-daemon", description="Run the Maestro local broker daemon")
    parser.add_argument("--port", type=int, default=0, help="Port to bind (0 = pick a free port)")
    parser.add_argument("--bind", default="127.0.0.1", help="Interface to listen on: 127.0.0.1 (default, local only), 0.0.0.0 (all interfaces — enables token auth), or an explicit IP")
    parser.add_argument("--state-dir", default=None, help="State directory (default: ~/.maestro or $MAESTRO_HOME)")
    parser.add_argument("--allow-origin", action="append", default=None, metavar="ORIGIN", help="A browser origin, such as https://maestro.example.com, that may POST to the daemon besides its own address; use it for the public address of a reverse proxy. Repeat for more than one. Overrides $MAESTRO_DAEMON_ALLOWED_ORIGINS")
    return parser.parse_args(argv)


def run_forever(install_handlers: bool = True) -> None:
    """Block until SIGINT/SIGTERM. Handlers are only installed on the main thread
    and are restored on exit so callers (and tests) keep their signal state."""
    stop = threading.Event()

    def _handle(signum: int, frame: Any) -> None:
        stop.set()

    previous: dict[int, Any] = {}
    if install_handlers and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, _handle)
    try:
        stop.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    from .daemonctl import DaemonAlreadyRunning

    args = _parse_args(argv)
    try:
        info = run_daemon(state_dir=args.state_dir, port=args.port, bind=args.bind, allowed_origins=args.allow_origin)
    except DaemonAlreadyRunning as exc:
        # Starting a second daemon here would overwrite the running daemon's
        # marker and mark its running tasks as failed, so refuse instead.
        print(
            f"maestro-daemon: {exc}. Stop that daemon with 'maestro daemon stop', "
            "or pass a different --state-dir.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    daemon = info.pop("daemon")
    print(json.dumps(info, indent=2), flush=True)  # flushed: consumers wait for this line
    try:
        run_forever()
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
