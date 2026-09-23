"""A2A remote adapter: delegate to another Maestro daemon over the A2A wire.

Daemon-to-daemon delegation using the same protocol the local daemon serves
(see :mod:`maestro.a2a`): Agent Card preflight, ``message/send`` for launch,
then the per-task SSE stream for live output/usage until a terminal state.
The registry spec's ``command`` field carries the remote base URL; an optional
spec ``token`` is sent as a Bearer credential when the remote daemon requires
one (non-loopback binds).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..agents import AgentSpec
from .base import _CANCEL_POLL_S, AdapterPreflight, AdapterResult, BaseAdapter, _next_line


class A2ARemoteAdapter(BaseAdapter):
    """One remote Maestro node registered as an agent."""

    kind = "a2a_remote"
    mode = "api"

    def __init__(self, spec: AgentSpec) -> None:
        super().__init__(spec)
        if spec is None or not (isinstance(spec.command, str) and spec.command.startswith(("http://", "https://"))):
            raise ValueError("a2a_remote agents need a registry spec whose command is the remote base URL (http(s)://host:port)")

    def binary(self) -> str | None:
        return self.spec.command if self.spec else None  # type: ignore[return-value]

    def preflight(self) -> AdapterPreflight:
        """Reachability + Agent Card check (no local binary involved)."""
        from ..a2a_client import fetch_agent_card

        base_url = self.api_base_url({})
        if not base_url:
            return AdapterPreflight(ok=False, error="No remote base URL configured for this a2a_remote agent")
        token = (self.spec.token if self.spec else None) or None
        try:
            card = fetch_agent_card(base_url, token=token)
        except ValueError as exc:
            return AdapterPreflight(ok=False, binary=base_url, error=str(exc))
        name = card.get("name") or "remote"
        version = str(card.get("version") or "?")
        return AdapterPreflight(ok=True, binary=base_url, version=f"{name} {version}")

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
        import time

        from ..a2a_client import post_jsonrpc, sse_events

        settings = dict(settings or {})
        base_url = self.api_base_url(settings)
        if not base_url:
            return AdapterResult(ok=False, error="No remote base URL configured for this a2a_remote agent")
        token = settings.get("token") or (self.spec.token if self.spec else None) or None
        started = time.monotonic()

        # Prefer the full handoff document (injected by the daemon) so routing,
        # expectations and constraints survive the hop; fall back to plain text.
        handoff = settings.get("maestro_handoff")
        if isinstance(handoff, dict):
            parts: list[dict[str, Any]] = [{"kind": "data", "data": handoff}]
        else:
            parts = [{"kind": "text", "text": prompt}]
        params = {
            "message": {
                "kind": "message",
                "role": "user",
                "parts": parts,
                "metadata": {"maestro": {"workspace": str(workspace)}},
            }
        }
        try:
            result = post_jsonrpc(base_url, "message/send", params, token=token)
        except ValueError as exc:
            return AdapterResult(ok=False, error=f"A2A message/send failed: {exc}", duration_s=time.monotonic() - started)
        task = (result or {}).get("task") or {}
        if isinstance(task.get("metadata"), dict) and task["metadata"].get("queued"):
            return AdapterResult(
                ok=False,
                error="Remote daemon queued the task (its workspace already has an active task); retry later",
                duration_s=time.monotonic() - started,
            )
        remote_id = task.get("id")
        if not remote_id:
            return AdapterResult(ok=False, error=f"A2A message/send returned no task id: {json.dumps(task)[:300]}", duration_s=time.monotonic() - started)

        usage: dict[str, Any] | None = None
        final_state: str | None = None
        error_text: str | None = None
        cancel_sent = False

        import queue as _queue
        import threading as _threading

        event_q: "_queue.Queue[tuple[str, dict[str, Any]] | BaseException]" = _queue.Queue()

        def _reader() -> None:
            try:
                for item in sse_events(base_url, f"/tasks/{remote_id}/events", token=token):
                    event_q.put(item)
            except BaseException as exc:  # surfaced to the main loop below
                event_q.put(exc)
            finally:
                event_q.put(None)  # stream closed

        reader = _threading.Thread(target=_reader, daemon=True)
        reader.start()
        deadline = started + timeout if timeout else None
        # A failed cancel request is tried again after this moment.
        cancel_retry_at = 0.0
        cancel_due: Callable[[], bool] | None = None
        if should_cancel is not None:
            asked = should_cancel
            cancel_due = lambda: not cancel_sent and time.monotonic() >= cancel_retry_at and asked()  # noqa: E731

        try:
            while True:
                # The events are read on a thread into a queue, so the wait for
                # the next one checks for a cancel at least every _CANCEL_POLL_S
                # seconds even while the remote agent sends nothing.
                outcome, item = _next_line(event_q, deadline, cancel_due)
                if outcome == "timeout":
                    return AdapterResult(ok=False, error=f"A2A remote task timed out after {timeout}s", usage=usage, duration_s=time.monotonic() - started)
                if outcome == "cancel":
                    try:
                        post_jsonrpc(base_url, "tasks/cancel", {"id": remote_id, "reason": "canceled by orchestrator"}, token=token)
                        cancel_sent = True
                    except ValueError:
                        # Best effort: try again shortly; meanwhile the stream
                        # still reports the outcome.
                        cancel_retry_at = time.monotonic() + _CANCEL_POLL_S
                    continue  # keep reading until the remote task reports how it ended
                if item is None:
                    break  # stream closed without a terminal state
                if isinstance(item, BaseException):
                    return AdapterResult(ok=False, error=f"A2A event stream failed: {item}", usage=usage, duration_s=time.monotonic() - started)
                event, envelope = item
                data = envelope.get("data") or {}  # TaskEvent.to_dict nests the payload under "data"
                if event == "output":
                    line = data.get("line")
                    if isinstance(line, str) and on_line is not None:
                        on_line(line)
                elif event == "usage" and isinstance(data, dict):
                    merged = {k: v for k, v in data.items() if k != "agent"}
                    usage = {**(usage or {}), **merged}
                elif event == "state":
                    state = data.get("state")
                    if isinstance(state, str):
                        final_state = state
                    if data.get("error"):
                        error_text = str(data["error"])
        except (OSError, ValueError) as exc:
            return AdapterResult(ok=False, error=f"A2A event stream failed: {exc}", usage=usage, duration_s=time.monotonic() - started)

        duration = time.monotonic() - started
        if final_state == "completed":
            return AdapterResult(ok=True, exit_code=0, usage=usage, duration_s=duration)
        if final_state in ("failed", "canceled"):
            return AdapterResult(ok=False, error=error_text or f"Remote task ended {final_state}", usage=usage, duration_s=duration)
        return AdapterResult(ok=False, error="A2A event stream closed before a terminal state", usage=usage, duration_s=duration)
