"""Forward the MCP server's daemon calls to the daemon that owns the state directory.

Only one daemon may run tasks for a state directory. When the MCP server starts
and another daemon already owns the directory (for example the background
daemon from ``maestro daemon start``), the MCP server does not start a second
one. :func:`maestro.daemon.get_daemon` returns a :class:`DaemonClient` instead,
and every MCP tool call goes to the owner over its HTTP API (JSON-RPC, with the
owner's token when it has one). The tasks then run in the owner, so the owner
can cancel them and one workspace never has two active tasks.

The client offers the methods the MCP tools call on a daemon, with the same
arguments, results and errors: an unknown task raises KeyError and a refused
request raises ValueError. When the owner stops answering, a call raises
:class:`DaemonUnavailable` (a ValueError, so the tools report it as an error).
The next ``get_daemon()`` waits a bounded time for the owner while it still
holds the directory, and starts a new daemon only once the owner is gone. A
blocking :meth:`DaemonClient.wait` makes that switch itself and keeps waiting
in the daemon that replaces the owner.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .a2a import ERR_TASK_NOT_FOUND, STATE_INPUT_REQUIRED, TERMINAL_STATES

_STOP_STATES = TERMINAL_STATES + (STATE_INPUT_REQUIRED,)


class DaemonUnavailable(ValueError):
    """The owner daemon did not answer, or answered with something that is not JSON-RPC."""


class DaemonClient:
    """The owner daemon of ``state_dir``, reached over HTTP."""

    RPC_TIMEOUT_S = 60.0  # longest wait for any one reply, on top of a tasks/wait slice
    WAIT_SLICE_S = 30.0  # one tasks/wait request blocks at most this long

    def __init__(self, state_dir: Path, url: str, token: str | None, pid: int | None, *, fallback: Callable[[], Any]) -> None:
        self.state_dir = Path(state_dir)
        self.url = url
        self.token = token
        self.pid = pid
        # Returns the daemon to use once this owner has gone away; see get_daemon.
        self._fallback = fallback

    # ------------------------------------------------------------ transport
    def alive(self) -> bool:
        """True while the same owner still owns the directory and answers HTTP."""
        from . import daemonctl

        info = daemonctl.status(self.state_dir)
        return info.running and info.pid == self.pid and info.url == self.url

    def stop(self) -> None:
        """Nothing to release: the owner daemon keeps running."""

    def _request(self, request: urllib.request.Request, timeout: float) -> Any:
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ValueError(f"the Maestro daemon at {self.url} rejected the request (HTTP 401): check its token") from None
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except ValueError:
                raise DaemonUnavailable(f"the Maestro daemon at {self.url} answered HTTP {exc.code}") from None
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or f"HTTP {exc.code}")
                if error.get("code") == ERR_TASK_NOT_FOUND:
                    raise KeyError(message) from None
                raise ValueError(message) from None
            raise ValueError(str(error or f"the Maestro daemon at {self.url} answered HTTP {exc.code}")) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise DaemonUnavailable(f"the Maestro daemon at {self.url} did not answer: {reason}") from None
        except ValueError:
            raise DaemonUnavailable(f"the Maestro daemon at {self.url} answered with something that is not JSON-RPC") from None

    def _call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
        request = urllib.request.Request(f"{self.url}/", data=body, headers={"Content-Type": "application/json"})
        payload = self._request(request, self.RPC_TIMEOUT_S if timeout is None else timeout)
        if not isinstance(payload, dict) or "result" not in payload:
            raise DaemonUnavailable(f"the Maestro daemon at {self.url} answered with something that is not JSON-RPC")
        return payload["result"]

    # ------------------------------------------------------------ daemon methods
    def delegate(self, doc: Any, workspace: str | Path) -> dict[str, Any]:
        # The owner runs in another directory, so a relative workspace is made absolute here.
        return self._call("tasks/delegate", {"handoff": doc.to_dict(), "workspace": str(Path(workspace).expanduser().resolve())})

    def wait(self, task_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until the task is terminal or needs input, or ``timeout`` seconds pass.

        The wait is split into ``tasks/wait`` requests of at most WAIT_SLICE_S
        seconds, so an owner that goes away is noticed. The wait then carries
        on in the daemon that replaces it, with the time that is left.
        """
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            slice_s = self.WAIT_SLICE_S if remaining is None else min(self.WAIT_SLICE_S, remaining)
            try:
                task = self._call("tasks/wait", {"id": task_id, "timeout": slice_s}, timeout=slice_s + self.RPC_TIMEOUT_S)["task"]
            except DaemonUnavailable:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                return self._fallback().wait(task_id, timeout=remaining)
            if task["status"]["state"] in _STOP_STATES or (deadline is not None and time.monotonic() >= deadline):
                return task

    def resolve(self, ref: str) -> str:
        return str(self._call("tasks/resolve", {"id": ref})["id"])

    def cancel(self, task_id: str, reason: str = "") -> dict[str, Any]:
        return self._call("tasks/cancel", {"id": task_id, "reason": reason})["cancel"]

    def answer_question(self, task_id: str, answer: str) -> dict[str, Any]:
        return self._call("tasks/answer", {"id": task_id, "answer": answer})

    def followup(self, task_id: str, instruction: str, context_mode: str = "reuse", branch: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"id": task_id, "instruction": instruction, "context_mode": context_mode}
        if branch is not None:
            params["branch"] = branch
        return self._call("tasks/followup", params)["followup"]

    def rename_branch(self, task_id: str, new_branch: str) -> dict[str, Any]:
        return self._call("tasks/renameBranch", {"id": task_id, "branch": new_branch})["rename"]

    def agents(self) -> list[dict[str, Any]]:
        return self._call("agents/list", {})["agents"]

    def status_a2a(self, task_id: str) -> dict[str, Any]:
        return self._call("tasks/get", {"id": task_id})["task"]

    def list_tasks(self) -> list[dict[str, Any]]:
        payload = self._request(urllib.request.Request(f"{self.url}/tasks"), self.RPC_TIMEOUT_S)
        return payload["tasks"]
