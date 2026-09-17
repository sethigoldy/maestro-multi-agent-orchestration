"""A2A client helpers shared by the CLI and the ``a2a_remote`` adapter.

Pure stdlib (urllib): JSON-RPC 2.0 over POST, SSE event iteration, and Agent
Card discovery. Errors surface as :class:`ValueError` with a human-readable
message so callers can map them onto task errors directly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterator


def post_jsonrpc(base_url: str, method: str, params: dict[str, Any], *, timeout: float = 60) -> Any:
    """Send one JSON-RPC 2.0 request to ``{base}/`` and return its result."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    request = urllib.request.Request(f"{base_url}/", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # A2A servers answer JSON-RPC errors with HTTP 400
        try:
            err = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            raise ValueError(f"remote answered HTTP {exc.code}") from None
        if not isinstance(err, dict) or "error" not in err:
            raise ValueError(f"remote answered HTTP {exc.code} with a non JSON-RPC body") from None
        raise ValueError(str((err.get("error") or {}).get("message", f"remote answered HTTP {exc.code}"))) from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError(f"cannot reach A2A endpoint at {base_url}: {reason}") from None
    if not isinstance(payload, dict):
        raise ValueError("remote answered with a non-object JSON-RPC payload")
    return payload.get("result")


def sse_events(url: str, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_name, data_dict)`` from an SSE endpoint.

    Blocks on the socket between events — this is a push stream, not polling.
    The per-task stream closes after a terminal state; the global one runs
    until the caller stops it (Ctrl-C).
    """
    with urllib.request.urlopen(url + path, timeout=None) as resp:
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


def fetch_agent_card(base_url: str, *, timeout: float = 10) -> dict[str, Any]:
    """GET ``{base}/.well-known/agent.json`` and return the card as a dict."""
    url = f"{base_url}/.well-known/agent.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"agent card request answered HTTP {exc.code} at {url}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError(f"cannot fetch agent card at {url}: {reason}") from None
    if not isinstance(payload, dict):
        raise ValueError(f"agent card at {url} is not a JSON object")
    return payload
