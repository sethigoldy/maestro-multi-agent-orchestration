"""A2A client helpers shared by the CLI and the ``a2a_remote`` adapter.

Pure stdlib (urllib): JSON-RPC 2.0 over POST, SSE event iteration, and Agent
Card discovery. Errors surface as :class:`ValueError` with a human-readable
message so callers can map them onto task errors directly.

Remote daemons may require a shared token (see ``maestro-daemon --bind``):
pass it via the ``token`` keyword; it is sent as an
``Authorization: Bearer <token>`` header on every request.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterator


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def post_jsonrpc(base_url: str, method: str, params: dict[str, Any], *, timeout: float = 60, token: str | None = None) -> Any:
    """Send one JSON-RPC 2.0 request to ``{base}/`` and return its result."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    headers = {"Content-Type": "application/json", **_auth_headers(token)}
    request = urllib.request.Request(f"{base_url}/", data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ValueError(f"remote rejected the request (HTTP 401 — check this agent's token)") from None
        try:
            err = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            raise ValueError(f"remote answered HTTP {exc.code}") from None
        if not isinstance(err, dict) or "error" not in err:
            raise ValueError(f"remote answered HTTP {exc.code} with a non JSON-RPC body") from None
        error = err.get("error")
        message = error if isinstance(error, str) else (error or {}).get("message", f"remote answered HTTP {exc.code}")
        raise ValueError(str(message)) from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError(f"cannot reach A2A endpoint at {base_url}: {reason}") from None
    if not isinstance(payload, dict):
        raise ValueError("remote answered with a non-object JSON-RPC payload")
    return payload.get("result")


def sse_events(url: str, path: str, token: str | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_name, data_dict)`` from an SSE endpoint.

    Blocks on the socket between events — this is a push stream, not polling.
    The per-task stream closes after a terminal state; the global one runs
    until the caller stops it (Ctrl-C).
    """
    request = urllib.request.Request(url + path, headers=_auth_headers(token))
    with urllib.request.urlopen(request, timeout=None) as resp:
        event = "message"
        data_lines: list[str] = []
        for raw in resp:  # blocking line iteration
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line:
                if data_lines:
                    yield event, json.loads("\n".join(data_lines))
                event, data_lines = "message", []
                continue
            if line.startswith(":"):
                continue  # keepalive comment
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data_lines.append(line[6:])
        if data_lines:
            yield event, json.loads("\n".join(data_lines))


def fetch_agent_card(base_url: str, *, timeout: float = 10, token: str | None = None) -> dict[str, Any]:
    """GET ``{base}/.well-known/agent.json`` and return the card as a dict."""
    url = f"{base_url}/.well-known/agent.json"
    request = urllib.request.Request(url, headers=_auth_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ValueError(f"agent card request rejected (HTTP 401 — check this agent's token)") from None
        raise ValueError(f"agent card request answered HTTP {exc.code} at {url}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError(f"cannot fetch agent card at {url}: {reason}") from None
    if not isinstance(payload, dict):
        raise ValueError(f"agent card at {url} is not a JSON object")
    return payload
