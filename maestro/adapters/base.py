"""Adapter execution modes and the shared adapter contract."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..agents import AgentSpec, DEFAULT_BINARIES


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

    Modes: ``spawn`` (one-shot process, implemented here), ``rpc`` (stdin/stdout
    JSON protocol) and ``api`` (HTTP service) — the latter two land with their
    adapters in M4/M5 and raise :class:`AdapterNotAvailable` until then.
    """

    kind: str = "base"
    mode: str = "spawn"

    def __init__(self, spec: AgentSpec | None = None) -> None:
        self.spec = spec

    # -- identity ---------------------------------------------------------
    def binary(self) -> str | None:
        if self.spec is not None and self.spec.kind == "generic" and self.spec.command:
            return self.spec.command.split()[0]
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
                probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
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
        if self.mode != "spawn":
            raise AdapterNotAvailable(f"Adapter {self.kind!r} uses mode {self.mode!r}; it is not implemented in this release yet")
        return self._run_spawn(prompt, workspace, task_id, settings=settings, timeout=timeout, log_dir=log_dir, on_line=on_line, should_cancel=should_cancel)

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

        settings = settings or {}
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
                text=True,
                start_new_session=True,
                env=os.environ.copy(),
            )
        except (OSError, ValueError) as exc:
            return AdapterResult(ok=False, error=f"Failed to launch agent {self.kind!r}: {exc}")
        # Popen gets a stdin pipe exactly when we have a prompt to pipe, so the
        # write below is unconditional once process.stdin exists.
        if process.stdin is not None:
            try:
                process.stdin.write(stdin_data)  # type: ignore[arg-type]
                process.stdin.close()  # EOF must be visible before we read the first output line
            except (OSError, ValueError):
                pass
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
        deadline = started + timeout if timeout else None
        try:
            lines: list[str] = []
            line_question: str | None = None
            while True:
                remaining = (deadline - time.monotonic()) if deadline is not None else None
                try:
                    line = line_q.get(timeout=remaining)
                except _queue.Empty:
                    _kill_group(process)
                    return AdapterResult(ok=False, error=f"Agent {self.kind!r} timed out after {timeout}s", duration_s=time.monotonic() - started)
                if line is None:
                    break
                if should_cancel is not None and should_cancel():
                    _kill_group(process)
                    return AdapterResult(ok=False, error=f"Agent {self.kind!r} was canceled", duration_s=time.monotonic() - started)
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
                exit_code = process.wait(timeout=5)
            except subprocess.SubprocessError:
                _kill_group(process)
                return AdapterResult(ok=False, error=f"Agent {self.kind!r} did not exit after closing its output", duration_s=time.monotonic() - started)
        except (OSError, ValueError) as exc:
            _kill_group(process)
            return AdapterResult(ok=False, error=f"Agent {self.kind!r} failed while streaming output: {exc}", duration_s=time.monotonic() - started)
        reader.join(timeout=5)
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


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.SubprocessError:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _probe_version(path: str) -> str | None:
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0].strip() if output else None
