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

from .a2a import ERR_METHOD_NOT_FOUND, ERR_TASK_NOT_FOUND, STATE_INPUT_REQUIRED, TERMINAL_STATES

_STOP_STATES = TERMINAL_STATES + (STATE_INPUT_REQUIRED,)


class DaemonUnavailable(ValueError):
    """The owner daemon did not answer, or answered with something that is not JSON-RPC."""


class OwnerTooOld(ValueError):
    """The owner answered -32601 Method not found: it predates the methods this client forwards."""


def unanswered_message(pid: int | None, state_dir: Path, url: str | None) -> str:
    """The error for an owner that holds the state directory but does not answer."""
    return (
        f"the Maestro daemon (pid {pid}) owns the state directory {state_dir} but does not answer at "
        f"{url}. This MCP server does not run tasks beside it. Try again, or restart it with "
        "'maestro daemon restart'."
    )


def _or_embedded(method: Callable[..., Any]) -> Callable[..., Any]:
    """Send the call to the owner; once the owner turned out to be too old, to the embedded daemon.

    An owner from an older version answers -32601 to the methods it lacks.
    The call is then made on the daemon that ``legacy`` provides (one inside
    this process, see maestro.daemon.get_daemon), and so is every later call.
    """
    name = method.__name__

    def call(self: "DaemonClient", *args: Any, **kwargs: Any) -> Any:
        if self._embedded is None:
            try:
                return method(self, *args, **kwargs)
            except OwnerTooOld:
                if self._legacy is None:
                    raise
                self._embedded = self._legacy()
        return getattr(self._embedded, name)(*args, **kwargs)

    call.__name__ = name
    call.__doc__ = method.__doc__
    return call


class DaemonClient:
    """The owner daemon of ``state_dir``, reached over HTTP."""

    RPC_TIMEOUT_S = 60.0  # longest wait for the reply to any call other than tasks/wait
    WAIT_SLICE_S = 30.0  # one tasks/wait request blocks at most this long
    WAIT_REPLY_GRACE_S = 5.0  # how much longer than its slice a tasks/wait reply may take

    def __init__(
        self,
        state_dir: Path,
        url: str,
        token: str | None,
        pid: int | None,
        *,
        fallback: Callable[..., Any],
        legacy: Callable[[], Any] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.url = url
        self.token = token
        self.pid = pid
        # Returns the daemon to use once this owner stopped answering; it is
        # called with give_up_at, the moment by which it must give up. See get_daemon.
        self._fallback = fallback
        # Returns the daemon to use when the owner is too old for these calls.
        self._legacy = legacy
        self._embedded: Any = None

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
                if error.get("code") == ERR_METHOD_NOT_FOUND:
                    raise OwnerTooOld(message) from None
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
    @_or_embedded
    def delegate(self, doc: Any, workspace: str | Path) -> dict[str, Any]:
        # The owner runs in another directory, so a relative workspace is made absolute here.
        return self._call("tasks/delegate", {"handoff": doc.to_dict(), "workspace": str(Path(workspace).expanduser().resolve())})

    @_or_embedded
    def wait(self, task_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until the task is terminal or needs input, or ``timeout`` seconds pass.

        The wait is split into ``tasks/wait`` requests of at most WAIT_SLICE_S
        seconds, so an owner that goes away is noticed. One deadline covers
        the whole wait, across any change of daemon:

        - When a request fails, the daemon that replaces the owner is looked
          up (the same owner again when it still answers probes) and the wait
          goes on there with the time that is left.
        - When the deadline passes, the last task object received is
          returned, as a normal timed-out wait does. If no answer came at all,
          DaemonUnavailable is raised.
        - When requests keep failing for OWNER_RETRY_S seconds (see
          maestro.daemon), DaemonUnavailable is raised, even without a
          deadline.
        """
        from . import daemon as _daemon

        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        current: Any = self
        last: dict[str, Any] | None = None
        failing_since: float | None = None
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if current is not self:
                # Another owner, or a daemon in this process, replaced this one.
                return current.wait(task_id, timeout=remaining)
            slice_s = self.WAIT_SLICE_S if remaining is None else min(self.WAIT_SLICE_S, remaining)
            try:
                task = self._call("tasks/wait", {"id": task_id, "timeout": slice_s}, timeout=slice_s + self.WAIT_REPLY_GRACE_S)["task"]
            except DaemonUnavailable:
                now = time.monotonic()
                failing_since = now if failing_since is None else failing_since
                out_of_time = deadline is not None and now >= deadline
                if out_of_time and last is not None:
                    return last
                if out_of_time or now - failing_since >= _daemon.OWNER_RETRY_S:
                    raise DaemonUnavailable(unanswered_message(self.pid, self.state_dir, self.url)) from None
                try:
                    current = self._fallback(give_up_at=deadline)
                except DaemonUnavailable:
                    if last is not None and deadline is not None and time.monotonic() >= deadline:
                        return last  # no replacement before the deadline: report the task as last seen
                    raise
                if isinstance(current, DaemonClient) and current.url == self.url and current.pid == self.pid:
                    current = self  # the same owner still answers probes: keep this wait's own bookkeeping
                continue
            failing_since = None
            last = task
            if task["status"]["state"] in _STOP_STATES or (deadline is not None and time.monotonic() >= deadline):
                return task

    @_or_embedded
    def resolve(self, ref: str) -> str:
        return str(self._call("tasks/resolve", {"id": ref})["id"])

    @_or_embedded
    def cancel(self, task_id: str, reason: str = "") -> dict[str, Any]:
        return self._call("tasks/cancel", {"id": task_id, "reason": reason})["cancel"]

    @_or_embedded
    def answer_question(self, task_id: str, answer: str) -> dict[str, Any]:
        return self._call("tasks/answer", {"id": task_id, "answer": answer})

    @_or_embedded
    def followup(self, task_id: str, instruction: str, context_mode: str = "reuse", branch: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"id": task_id, "instruction": instruction, "context_mode": context_mode}
        if branch is not None:
            params["branch"] = branch
        return self._call("tasks/followup", params)["followup"]

    @_or_embedded
    def rename_branch(self, task_id: str, new_branch: str) -> dict[str, Any]:
        return self._call("tasks/renameBranch", {"id": task_id, "branch": new_branch})["rename"]

    @_or_embedded
    def agents(self) -> list[dict[str, Any]]:
        return self._call("agents/list", {})["agents"]

    @_or_embedded
    def status_a2a(self, task_id: str) -> dict[str, Any]:
        return self._call("tasks/get", {"id": task_id})["task"]

    @_or_embedded
    def list_tasks(self) -> list[dict[str, Any]]:
        payload = self._request(urllib.request.Request(f"{self.url}/tasks"), self.RPC_TIMEOUT_S)
        return payload["tasks"]
