"""Adapter execution modes and the shared adapter contract."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..agents import AgentSpec, DEFAULT_BINARIES


#: Environment marker set on every agent process Maestro launches. The global
#: maestro-driven-development skill checks it: a worker running under these
#: variables must perform its assigned implementation directly and must NOT
#: delegate the work back to Maestro (recursion protection).
MAESTRO_CONTEXT_ENV = "MAESTRO_AGENT_CONTEXT"
MAESTRO_TASK_ID_ENV = "MAESTRO_TASK_ID"
MAESTRO_ROLE_ENV = "MAESTRO_ROLE"

#: Matches a valid environment-variable name (used to parse ``env`` output).
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: One-per-process snapshot of the user's login-shell environment. ``None``
#: means "not captured yet"; an empty dict means "captured, nothing found".
_LOGIN_ENV_CACHE: dict[str, str] | None = None
_ENV_MARKER = "__MAESTRO_ENV__"
# After an agent's own process exits, how long to keep reading output that a
# child it left running may still hold open.
_EXIT_GRACE_S = 2.0
# After a cancel, how long an rpc agent gets to act on its abort command (and
# how long Maestro tries to write that command) before its group is stopped.
_RPC_CANCEL_GRACE_S = 5.0
# After SIGTERM, how long the processes in an agent's group get to exit (to
# remove a lock file, for example) before SIGKILL, and how often they are checked.
_GROUP_TERM_GRACE_S = 2.0
_GROUP_POLL_S = 0.05
# How long to wait for the agent's own process after SIGTERM, and again after SIGKILL.
_LEADER_WAIT_S = 5.0


def capture_login_env(timeout_s: float | None = None) -> dict[str, str]:
    """Snapshot the user's login-shell environment (profile exports applied).

    The daemon may be started from a context that lacks variables the user's
    shell profile exports — API keys above all (launchd, a GUI app, an old
    terminal). Once per process we run ``$SHELL -lc`` with a command that
    prints a marker and then ``env -0``, so spawned agents see the same
    defaults as an interactive session. ``env -0`` separates variables with
    NUL, so a value that spans several lines (a PEM key) stays whole, and the
    marker skips anything the profile itself prints first. Values are decoded
    like ``os.environ`` (surrogateescape), so bytes that are not UTF-8 reach
    the agent unchanged.

    If ``env -0`` fails or prints nothing after the marker (an ``env`` without
    ``-0``), the shell is run a second time with plain ``env``, which is parsed
    line by line, and a warning that multi-line values may be cut is printed to
    stderr. Both runs share one timeout. Set ``MAESTRO_LOGIN_ENV=0`` to disable.
    Any other failure (missing shell, timeout, no marker) degrades to the empty
    dict: the daemon's own environment still flows through unchanged.
    """
    global _LOGIN_ENV_CACHE
    if _LOGIN_ENV_CACHE is not None:
        return _LOGIN_ENV_CACHE
    env: dict[str, str] = {}
    if os.environ.get("MAESTRO_LOGIN_ENV", "1") != "0":
        shell = (os.environ.get("SHELL") or "").strip() or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
        try:
            timeout = timeout_s if timeout_s is not None else float(os.environ.get("MAESTRO_LOGIN_ENV_TIMEOUT_S", "10"))
            deadline = time.monotonic() + timeout
            # Bytes, not text: text mode would also turn "\r\n" in a value into "\n".
            proc = subprocess.run([shell, "-lc", f"printf '\\0{_ENV_MARKER}\\0'; env -0"], capture_output=True, timeout=timeout)
            _, found, listing = proc.stdout.rpartition(f"\0{_ENV_MARKER}\0".encode())
            if proc.returncode == 0 and found and listing:
                entries = listing.split(b"\0")
            else:
                print(f"[maestro] warning: `env -0` did not work in the login shell {shell}; reading plain `env` output instead, so multi-line values may be cut", file=sys.stderr)
                proc = subprocess.run(
                    [shell, "-lc", f"printf '\\n{_ENV_MARKER}\\n'; env"],
                    capture_output=True, timeout=max(0.0, deadline - time.monotonic()),
                )
                _, found, listing = proc.stdout.rpartition(f"\n{_ENV_MARKER}\n".encode())
                entries = listing.split(b"\n") if found else []
            for entry in entries:
                key, sep, value = os.fsdecode(entry).partition("=")
                if sep and _ENV_KEY_RE.match(key):
                    env[key] = value
        except (OSError, ValueError, subprocess.SubprocessError):
            env = {}
    _LOGIN_ENV_CACHE = env
    return env


def worker_environment(task_id: str) -> dict[str, str]:
    """Environment for a spawned implementation agent.

    Layered bottom-to-top: login-shell defaults (profile exports such as API
    keys), then the daemon's own environment (explicit values win on conflict),
    then the recursion-guard markers (always authoritative).
    """
    env = dict(capture_login_env())
    env.update(os.environ)
    env[MAESTRO_CONTEXT_ENV] = "1"
    env[MAESTRO_TASK_ID_ENV] = task_id
    env[MAESTRO_ROLE_ENV] = "implementation"
    return env


class AdapterNotAvailable(RuntimeError):
    """The adapter kind exists but is not implemented in this release yet."""


@dataclass
class AdapterPreflight:
    ok: bool
    binary: str | None = None
    version: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "binary": self.binary, "version": self.version, "error": self.error}


@dataclass
class AdapterResult:
    ok: bool
    exit_code: int | None = None
    output_path: str | None = None
    usage: dict[str, Any] | None = None
    question: str | None = None
    error: str | None = None
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "output_path": self.output_path,
            "usage": self.usage,
            "question": self.question,
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
        }


class BaseAdapter:
    """One registered agent's execution contract.

    Modes: ``spawn`` (one-shot process), ``rpc`` (long-lived stdin/stdout JSON
    protocol, pi-style agents) and ``api`` (remote HTTP service with its own
    lifecycle — generic REST task contract in :meth:`_run_api`, A2A wire in the
    ``a2a_remote`` adapter). All three are implemented.
    """

    kind: str = "base"
    mode: str = "spawn"

    def __init__(self, spec: AgentSpec | None = None) -> None:
        self.spec = spec

    # -- identity ---------------------------------------------------------
    def binary(self) -> str | None:
        if self.spec is not None and self.spec.kind == "generic" and self.spec.command:
            # Split the template the same way build_command does, so a quoted
            # executable path that contains spaces is found as one word.
            try:
                words = shlex.split(self.spec.command)
            except ValueError:
                return None  # unbalanced quotes: no executable can be named
            return words[0] if words else None
        return DEFAULT_BINARIES.get(self.kind)

    def preflight(self) -> AdapterPreflight:
        """Binary + version check (plus an optional auth probe when configured)."""
        binary = self.binary()
        if not binary:
            return AdapterPreflight(ok=False, error=f"No executable known for adapter kind {self.kind!r}")
        path = shutil.which(binary)
        if not path:
            return AdapterPreflight(ok=False, binary=binary, error=f"Executable not found on PATH: {binary}")
        version = _probe_version(path)
        probe_cmd = self.auth_probe()
        if probe_cmd:
            try:
                probe = subprocess.run(probe_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            except (OSError, subprocess.SubprocessError) as exc:
                return AdapterPreflight(ok=False, binary=binary, version=version, error=f"Auth probe failed to run: {exc}")
            if probe.returncode != 0:
                detail = (probe.stderr or probe.stdout or "").strip().splitlines()
                return AdapterPreflight(
                    ok=False, binary=binary, version=version,
                    error=f"Agent is not authenticated or unhealthy ({binary}): {detail[0] if detail else 'probe exited non-zero'}",
                )
        return AdapterPreflight(ok=True, binary=binary, version=version)

    def auth_probe(self) -> list[str] | None:
        """Cheap authenticated probe command; None means no probe (version check only)."""
        return None

    # -- command construction ---------------------------------------------
    def input_mode(self) -> str:
        if self.spec is not None and self.spec.kind == "generic":
            return self.spec.input_mode
        return "stdin"

    def build_command(self, prompt: str, workspace: Path, task_id: str, settings: dict[str, Any]) -> list[str]:
        raise NotImplementedError(f"Adapter {self.kind!r} does not build spawn commands")

    # -- output parsing ----------------------------------------------------
    def parse_line(self, line: str) -> dict[str, Any] | None:
        """Return {"usage": {...}} / {"question": "..."} for structured lines; else None."""
        return None

    def detect_question(self, output_text: str) -> str | None:
        return None

    # -- execution ----------------------------------------------------------
    def run(
        self,
        prompt: str,
        workspace: Path,
        task_id: str,
        *,
        settings: dict[str, Any] | None = None,
        timeout: float | None = None,
        log_dir: str | Path | None = None,
        on_line: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AdapterResult:
        if self.mode == "spawn":
            return self._run_spawn(prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir, on_line=on_line, should_cancel=should_cancel)
        if self.mode == "rpc":
            return self._run_rpc(prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir, on_line=on_line, should_cancel=should_cancel)
        if self.mode == "api":
            return self._run_api(prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir, on_line=on_line, should_cancel=should_cancel)
        raise AdapterNotAvailable(f"Adapter {self.kind!r} uses mode {self.mode!r}; it is not implemented in this release yet")

    def _run_spawn(
        self,
        prompt: str,
        workspace: Path,
        task_id: str,
        *,
        settings: dict[str, Any] | None,
        timeout: float | None,
        log_dir: str | Path | None,
        on_line: Callable[[str], None] | None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AdapterResult:
        import queue as _queue
        import threading as _threading
        import time

        settings = dict(settings or {})
        if log_dir is not None:
            settings["maestro_log_dir"] = str(log_dir)
        command = self.build_command(prompt, workspace, task_id, settings)
        if log_dir is not None:
            log_path = Path(log_dir) / f"{self.kind}-{task_id}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            log_path = None
        usage: dict[str, Any] | None = None
        started = time.monotonic()
        stdin_data = prompt if self.input_mode() == "stdin" else None
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # Agent output is not always valid UTF-8 (a Latin-1 file, a
                # binary test log). Strict decoding would kill the reader.
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
                env=worker_environment(task_id),
            )
        except (OSError, ValueError) as exc:
            return AdapterResult(ok=False, error=f"Failed to launch agent {self.kind!r}: {exc}")
        line_q: "_queue.Queue[str | None]" = _queue.Queue()

        def _reader() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    line_q.put(line)
            finally:
                line_q.put(None)  # sentinel: stream closed

        # Start reading before writing the prompt: an agent that prints a lot
        # before it reads stdin would otherwise fill the output pipe while we
        # block on a full input pipe, and neither side could move.
        reader = _threading.Thread(target=_reader, daemon=True)
        reader.start()
        if process.stdin is not None:
            _threading.Thread(target=_write_and_close, args=(process.stdin, stdin_data), daemon=True).start()
        deadline = started + timeout if timeout else None
        idle = _exit_watch(process)
        lines: list[str] = []
        line_question: str | None = None

        def _take(line: str) -> None:
            """Record one output line: log, live callback, usage and question."""
            nonlocal usage, line_question
            lines.append(line.rstrip("\n"))
            if log_path is not None:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            if on_line is not None:
                on_line(line.rstrip("\n"))
            parsed = self.parse_line(line.rstrip("\n"))
            if parsed:
                if isinstance(parsed.get("usage"), dict):
                    usage = {**(usage or {}), **parsed["usage"]}
                if isinstance(parsed.get("question"), str) and parsed["question"].strip():
                    line_question = parsed["question"].strip()

        try:
            while True:
                outcome, line = _next_line(line_q, deadline, should_cancel, idle)
                if outcome == "timeout":
                    _kill_group(process)
                    return AdapterResult(ok=False, error=f"Agent {self.kind!r} timed out after {timeout}s", duration_s=time.monotonic() - started)
                if outcome == "cancel":
                    _kill_group(process)
                    return AdapterResult(ok=False, error=f"Agent {self.kind!r} was canceled", duration_s=time.monotonic() - started)
                if outcome == "idle" or line is None:
                    break  # output closed, or the agent exited and a child still holds it
                _take(line)
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.SubprocessError:
                _kill_group(process)
                return AdapterResult(ok=False, error=f"Agent {self.kind!r} did not exit after closing its output", duration_s=time.monotonic() - started)
            _kill_leftovers(process)  # background children the agent left behind
            # With the group stopped the output closes, and the reader hands
            # over the agent's last line even when it has no trailing newline.
            reader.join(timeout=5)
            for line in _drain(line_q):
                _take(line)
        except (OSError, ValueError) as exc:
            _kill_group(process)
            return AdapterResult(ok=False, error=f"Agent {self.kind!r} failed while streaming output: {exc}", duration_s=time.monotonic() - started)
        output_text = "\n".join(lines)
        question = line_question or self.detect_question(output_text)
        ok = exit_code == 0
        error = None
        if not ok:
            tail = "\n".join(lines[-10:])
            error = f"Agent {self.kind!r} exited with code {exit_code}" + (f"; last output:\n{tail}" if tail else "")
        return AdapterResult(
            ok=ok, exit_code=exit_code, output_path=str(log_path) if log_path else None,
            usage=usage, question=question, error=error, duration_s=time.monotonic() - started,
        )

    # -- api execution ------------------------------------------------------
    def api_base_url(self, settings: dict[str, Any] | None) -> str | None:
        """Base URL of the remote service; subclasses may override."""
        settings = settings or {}
        url = settings.get("api_base_url")
        if isinstance(url, str) and url:
            return url.rstrip("/")
        if self.spec is not None and isinstance(self.spec.command, str) and self.spec.command.startswith(("http://", "https://")):
            return self.spec.command.rstrip("/")
        return None

    def api_endpoints(self) -> dict[str, str]:
        """Pluggable REST task contract (relative paths; ``{id}`` is substituted)."""
        return {
            "submit": "/tasks",
            "status": "/tasks/{id}",
            "cancel": "/tasks/{id}/cancel",
        }

    def api_poll_interval_s(self) -> float:
        """Status poll cadence (REST servers without a push channel need polling)."""
        return 1.0

    def _run_api(
        self,
        prompt: str,
        workspace: Path,
        task_id: str,
        *,
        settings: dict[str, Any] | None,
        timeout: float | None,
        log_dir: str | Path | None,
        on_line: Callable[[str], None] | None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AdapterResult:
        import time

        import urllib.error
        import urllib.request

        settings = dict(settings or {})
        base_url = self.api_base_url(settings)
        if not base_url:
            return AdapterResult(ok=False, error=f"Adapter {self.kind!r} needs an api base URL (settings['api_base_url'] or a spec command that is an http(s) URL)")
        endpoints = self.api_endpoints()
        started = time.monotonic()

        def _http(method: str, path: str, payload: dict[str, Any] | None = None):
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            request = urllib.request.Request(base_url + path, data=body, method=method, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}

        try:
            submitted = _http("POST", endpoints["submit"], {"task_id": task_id, "prompt": prompt, "workspace": str(workspace)})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return AdapterResult(ok=False, error=f"Failed to submit task to {self.kind!r} at {base_url}: {exc}", duration_s=time.monotonic() - started)
        if not isinstance(submitted, dict):
            return AdapterResult(ok=False, error=f"Remote {self.kind!r} answered with a non-object payload on submit", duration_s=time.monotonic() - started)
        remote_id = str(submitted.get("id") or task_id)

        deadline = started + timeout if timeout else None
        usage: dict[str, Any] | None = None
        seen_output = ""
        canceled_remote = False
        try:
            while True:
                remaining = (deadline - time.monotonic()) if deadline is not None else None
                if should_cancel is not None and should_cancel():
                    if not canceled_remote:
                        try:
                            _http("POST", endpoints["cancel"].replace("{id}", remote_id))
                            canceled_remote = True
                        except (urllib.error.URLError, OSError, ValueError):
                            pass  # best effort; keep polling for the terminal state
                try:
                    status = _http("GET", endpoints["status"].replace("{id}", remote_id))
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    return AdapterResult(ok=False, error=f"Remote {self.kind!r} status lookup failed: {exc}", usage=usage, duration_s=time.monotonic() - started)
                if not isinstance(status, dict):
                    return AdapterResult(ok=False, error=f"Remote {self.kind!r} answered with a non-object payload on status", usage=usage, duration_s=time.monotonic() - started)
                output = status.get("output")
                if isinstance(output, str) and len(output) > len(seen_output):
                    new_part = output[len(seen_output):]
                    seen_output = output
                    for line in new_part.splitlines():
                        if on_line is not None:
                            on_line(line)
                state = str(status.get("state") or "working")
                if state == "completed":
                    if isinstance(status.get("usage"), dict):
                        usage = {**(usage or {}), **status["usage"]}
                    return AdapterResult(ok=True, exit_code=0, usage=usage, duration_s=time.monotonic() - started)
                if state in ("failed", "canceled"):
                    error = status.get("error") or f"Remote task ended {state}"
                    if isinstance(status.get("usage"), dict):
                        usage = {**(usage or {}), **status["usage"]}
                    return AdapterResult(ok=False, exit_code=None, error=str(error), usage=usage, duration_s=time.monotonic() - started)
                if remaining is not None and time.monotonic() >= deadline:
                    break
                time.sleep(self.api_poll_interval_s())
        except (OSError, ValueError) as exc:
            return AdapterResult(ok=False, error=f"Remote {self.kind!r} failed while polling status: {exc}", usage=usage, duration_s=time.monotonic() - started)
        return AdapterResult(ok=False, error=f"Remote {self.kind!r} timed out after {timeout}s", usage=usage, duration_s=time.monotonic() - started)

    # -- rpc execution ------------------------------------------------------
    def rpc_start_command(self, prompt: str, task_id: str) -> dict[str, Any]:
        """First command sent once the agent process is up."""
        raise NotImplementedError(f"Adapter {self.kind!r} does not speak an rpc protocol")

    def rpc_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Classify one protocol event.

        Return a subset of ``{"fail": str}`` (run failed definitively),
        ``{"done": True}`` (run finished successfully) and/or
        ``{"usage": dict}`` (merged into the cumulative usage)."""
        raise NotImplementedError(f"Adapter {self.kind!r} does not speak an rpc protocol")

    def rpc_abort_command(self) -> dict[str, Any] | None:
        """Polite abort command sent before a forced kill; None kills directly."""
        return None

    def _run_rpc(
        self,
        prompt: str,
        workspace: Path,
        task_id: str,
        *,
        settings: dict[str, Any] | None,
        timeout: float | None,
        log_dir: str | Path | None,
        on_line: Callable[[str], None] | None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AdapterResult:
        import json as _json
        import queue as _queue
        import threading as _threading
        import time

        settings = dict(settings or {})
        if log_dir is not None:
            settings["maestro_log_dir"] = str(log_dir)
        command = self.build_command(prompt, workspace, task_id, settings)
        if log_dir is not None:
            log_path = Path(log_dir) / f"{self.kind}-{task_id}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            log_path = None
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
                env=worker_environment(task_id),
            )
        except (OSError, ValueError) as exc:
            return AdapterResult(ok=False, error=f"Failed to launch agent {self.kind!r}: {exc}")

        def _send(obj: dict[str, Any]) -> None:
            # stdin is always a PIPE in rpc mode; writes can still fail once the
            # process dies, and those are swallowed on purpose.
            try:
                process.stdin.write(_json.dumps(obj) + "\n")  # type: ignore[union-attr]
                process.stdin.flush()  # type: ignore[union-attr]
            except (OSError, ValueError):
                # Close our end so the unsent data is dropped now, instead of
                # failing again (and printing an error) when the stream is
                # garbage collected.
                try:
                    process.stdin.close()  # type: ignore[union-attr]
                except (OSError, ValueError):
                    pass

        usage: dict[str, Any] | None = None
        failed: str | None = None

        def _handle(line_text: str) -> str:
            """Classify one line; returns 'fail', 'done' or '' after state updates."""
            nonlocal usage, failed
            try:
                event = _json.loads(line_text)
            except ValueError:
                return ""  # stderr noise or non-JSON chatter
            if not isinstance(event, dict):
                return ""
            classified = self.rpc_event(event)
            if not classified:
                return ""
            if isinstance(classified.get("usage"), dict):
                usage = {**(usage or {}), **classified["usage"]}
            if isinstance(classified.get("fail"), str):
                failed = classified["fail"]
                return "fail"
            if classified.get("done"):
                return "done"
            return ""

        line_q: "_queue.Queue[str | None]" = _queue.Queue()

        def _reader() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    line_q.put(line)
            finally:
                line_q.put(None)  # sentinel: stream closed

        reader = _threading.Thread(target=_reader, daemon=True)
        reader.start()
        # Sent after the reader starts, in its own thread: the start command
        # carries the whole prompt and must not block on a full pipe.
        sender = _threading.Thread(target=_send, args=(self.rpc_start_command(prompt, task_id),), daemon=True)
        sender.start()
        deadline = started + timeout if timeout else None
        idle = _exit_watch(process)
        done = False
        cancel_grace_until: float | None = None
        abort_writer: "_threading.Thread | None" = None
        lines: list[str] = []

        def _take(line: str) -> None:
            """Record one output line: log and live callback."""
            lines.append(line.rstrip("\n"))
            if log_path is not None:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            if on_line is not None:
                on_line(line.rstrip("\n"))

        try:
            while True:
                if cancel_grace_until is None:
                    outcome, line = _next_line(line_q, deadline, should_cancel, idle)
                else:  # already aborting: only the grace period (or the deadline) is left
                    grace_deadline = cancel_grace_until if deadline is None else min(deadline, cancel_grace_until)
                    outcome, line = _next_line(line_q, grace_deadline, None)
                    if outcome == "timeout":
                        break  # abort grace expired; fall through to the kill path
                if outcome == "timeout":
                    _kill_group(process)
                    return AdapterResult(ok=False, error=f"Agent {self.kind!r} timed out after {timeout}s", duration_s=time.monotonic() - started)
                if outcome == "cancel":
                    abort = self.rpc_abort_command()
                    sender.join(timeout=1)  # never interleave the abort with the start command
                    if abort is not None and not sender.is_alive():
                        # Written from its own thread: an agent that does not
                        # read its stdin would block this write for ever, and
                        # neither the cancel grace nor the deadline could fire.
                        abort_writer = _threading.Thread(target=_send, args=(abort,), daemon=True)
                        abort_writer.start()
                    cancel_grace_until = time.monotonic() + _RPC_CANCEL_GRACE_S
                    continue
                if outcome == "idle" or line is None:
                    break  # stream closed (or the agent exited) before it settled
                _take(line)
                action = _handle(line.rstrip("\n"))
                if action == "fail":
                    _kill_group(process)
                    reader.join(timeout=5)
                    return AdapterResult(ok=False, error=failed or f"Agent {self.kind!r} failed", output_path=str(log_path) if log_path else None, usage=usage, duration_s=time.monotonic() - started)
                if action == "done":
                    done = True
                    break
            sender.join(timeout=1)
            if sender.is_alive() or (abort_writer is not None and abort_writer.is_alive()):
                # The agent never took its start command, or did not take the
                # abort within the cancel grace. Closing stdin would wait
                # behind that blocked write, so stop the agent instead.
                _kill_group(process)
                exit_code = process.poll()
            else:
                try:
                    process.stdin.close()  # polite EOF; long-lived agents may exit on their own
                except (OSError, ValueError):
                    pass
                try:
                    exit_code = process.wait(timeout=5)
                except subprocess.SubprocessError:
                    _kill_group(process)
                    exit_code = None
            _kill_leftovers(process)
            # With the group stopped the output closes, and the reader hands
            # over the agent's last line even when it has no trailing newline.
            # Those lines are recorded; they settle the run only if nothing
            # before them did.
            reader.join(timeout=5)
            for line in _drain(line_q):
                _take(line)
                if not done and failed is None:
                    done = _handle(line.rstrip("\n")) == "done"
        except (OSError, ValueError) as exc:
            _kill_group(process)
            return AdapterResult(ok=False, error=f"Agent {self.kind!r} failed while streaming output: {exc}", duration_s=time.monotonic() - started)
        if cancel_grace_until is not None:
            return AdapterResult(
                ok=False, exit_code=exit_code, output_path=str(log_path) if log_path else None,
                usage=usage, error=f"Agent {self.kind!r} was canceled", duration_s=time.monotonic() - started,
            )
        if done:
            return AdapterResult(
                ok=True, exit_code=exit_code, output_path=str(log_path) if log_path else None,
                usage=usage, duration_s=time.monotonic() - started,
            )
        if failed is not None:  # a failure event among the last lines
            return AdapterResult(
                ok=False, exit_code=exit_code, output_path=str(log_path) if log_path else None,
                usage=usage, error=failed or f"Agent {self.kind!r} failed", duration_s=time.monotonic() - started,
            )
        tail = "\n".join(lines[-10:])
        error = f"Agent {self.kind!r} closed its stream before settling (exit code {exit_code})" + (f"; last output:\n{tail}" if tail else "")
        return AdapterResult(
            ok=False, exit_code=exit_code, output_path=str(log_path) if log_path else None,
            usage=usage, error=error, duration_s=time.monotonic() - started,
        )


# How often a run checks for cancellation while the agent prints nothing.
_CANCEL_POLL_S = 0.5


def _next_line(
    line_q: Any, deadline: float | None, should_cancel: Callable[[], bool] | None,
    idle: Callable[[], bool] | None = None,
) -> tuple[str, str | None]:
    """Wait for the agent's next output line.

    Returns ("line", text) with text None once the output has closed,
    ("timeout", None) when the deadline passes, ("cancel", None) when
    ``should_cancel`` reports a cancel, or ("idle", None) as soon as ``idle``
    returns True, even when more output is waiting (the caller drains it
    later). Cancellation and ``idle`` are checked on every call and at least
    every _CANCEL_POLL_S seconds, so an agent that is silent for a long time
    (for example while it runs a test suite) is still stopped promptly, and a
    child that prints all the time cannot hold a finished run open.
    """
    import queue as _queue

    while True:
        if should_cancel is not None and should_cancel():
            return "cancel", None
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return "timeout", None
        if idle is not None and idle():
            return "idle", None
        wait = remaining
        if should_cancel is not None or idle is not None:
            wait = _CANCEL_POLL_S if wait is None else min(wait, _CANCEL_POLL_S)
        try:
            return "line", line_q.get(timeout=wait)
        except _queue.Empty:
            pass


def _drain(line_q: Any) -> list[str]:
    """Take the lines already waiting in the queue, without blocking.

    Stops at the end-of-output sentinel. It takes at most the number of lines
    waiting when it starts, so a process outside the agent's group that keeps
    printing cannot keep the run in this loop. The caller is the only reader
    of the queue, so every line counted by qsize() is still there."""
    out: list[str] = []
    for _ in range(line_q.qsize()):
        item = line_q.get_nowait()
        if item is None:
            break
        out.append(item)
    return out


def _write_and_close(stream: Any, data: str | None) -> None:
    """Write the prompt to the agent's stdin and close it (EOF), ignoring a
    pipe the agent already closed."""
    try:
        stream.write(data)
        stream.close()
    except (OSError, ValueError):
        pass


def _exit_watch(process: subprocess.Popen) -> Callable[[], bool]:
    """An ``idle`` check for _next_line: True once _EXIT_GRACE_S has passed
    since the agent's own process exited, whether or not output is still
    arriving. A child the agent started in the background can keep the output
    pipe open for ever, and may print all the time (a dev server, a watcher);
    without this the run would wait for that child instead of the agent. The
    exit is noticed within _CANCEL_POLL_S, because _next_line calls this at
    least that often."""
    exited_at: list[float] = []

    def _idle() -> bool:
        if process.poll() is None:
            return False
        if not exited_at:
            exited_at.append(time.monotonic())
        return time.monotonic() - exited_at[0] >= _EXIT_GRACE_S

    return _idle


# Stopping an agent's process group
#
# Agents start with start_new_session=True, so the process group id is the
# agent's pid. os.getpgid(pid) is not used: it fails once the agent itself has
# exited, even while children in its group are still running.
#
# Before every signal the group is checked with os.killpg(pgid, 0). A group
# that is seen gone once (ProcessLookupError, or PermissionError because the
# id now names another user's group) is remembered on the Popen object and
# never signalled again. The kernel does not hand out a pid while a process
# group with that id still exists, so while the check finds the group, the id
# is still ours. One race remains in theory: the group empties between the
# check and the signal, and in that instant the pid comes round again and a
# new process makes itself the leader of a group with that id. That needs the
# whole pid range to wrap within microseconds, and the window is only the time
# between two system calls.


def _group_alive(process: subprocess.Popen) -> bool:
    """True while the agent's process group exists and may be signalled."""
    if getattr(process, "_maestro_group_gone", False):
        return False
    try:
        os.killpg(process.pid, 0)
    except OSError:
        process._maestro_group_gone = True  # type: ignore[attr-defined]
        return False
    return True


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    if not _group_alive(process):
        return
    try:
        os.killpg(process.pid, sig)
    except OSError:
        process._maestro_group_gone = True  # type: ignore[attr-defined]  # gone between the check and the signal


def _finish_group(process: subprocess.Popen, term_sent_at: float) -> None:
    """Wait until _GROUP_TERM_GRACE_S after SIGTERM for the group to empty,
    then SIGKILL whatever is left."""
    while _group_alive(process):
        if time.monotonic() - term_sent_at >= _GROUP_TERM_GRACE_S:
            _signal_group(process, signal.SIGKILL)
            return
        time.sleep(_GROUP_POLL_S)


def _kill_group(process: subprocess.Popen) -> None:
    """Stop an agent that is still running (timeout, cancel, failure) and
    every process in its group.

    The whole group gets SIGTERM first. The agent's own process then has up
    to _LEADER_WAIT_S to exit before it gets SIGKILL (and up to _LEADER_WAIT_S
    more to be reaped). The rest of the group has until _GROUP_TERM_GRACE_S
    after the SIGTERM, so a child can remove a lock file, before SIGKILL.
    The worst case is about 10 seconds; a group that exits on SIGTERM takes
    only as long as its slowest process."""
    term_sent_at = time.monotonic()
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=_LEADER_WAIT_S)
    except subprocess.SubprocessError:
        _signal_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=_LEADER_WAIT_S)  # reap it
        except subprocess.SubprocessError:
            pass
    _finish_group(process, term_sent_at)


def _kill_leftovers(process: subprocess.Popen) -> None:
    """After a run ends normally, stop any process the agent left running in
    its group. A task is a batch run; nothing it started should outlive it.
    They get SIGTERM, then SIGKILL after at most _GROUP_TERM_GRACE_S. When
    nothing is left, this returns at once."""
    if not _group_alive(process):
        return
    term_sent_at = time.monotonic()
    _signal_group(process, signal.SIGTERM)
    _finish_group(process, term_sent_at)


def _probe_version(path: str) -> str | None:
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0].strip() if output else None
