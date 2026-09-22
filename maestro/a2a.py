"""Thin native A2A wire layer (v1 core subset).

Implements just enough of the Agent2Agent protocol for Maestro nodes:
Agent Card, ``message/send``, ``tasks/get``, ``tasks/cancel`` over JSON-RPC 2.0,
and SSE encoding for task event streams. Conformance is verified against the
published field shapes in tests; no framework dependency is pulled in.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

A2A_VERSION = "1.0"

# A2A TaskState values used by Maestro (subset of the spec's enum).
STATE_SUBMITTED = "submitted"
STATE_WORKING = "working"
STATE_INPUT_REQUIRED = "input-required"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"

TERMINAL_STATES = (STATE_COMPLETED, STATE_FAILED, STATE_CANCELED)


def agent_card(*, name: str, url: str, skills: list[dict[str, Any]], version: str = A2A_VERSION) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Maestro multi-agent orchestration node: delegates tasks to registered agents and reports durable task state.",
        "url": url,
        "version": version,
        "capabilities": {"streaming": True, "pushNotifications": False},
        "skills": skills,
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/event-stream"],
    }


def jsonrpc_ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_TASK_NOT_FOUND = -32004


def _extract_handoff(message: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Pull the handoff document (or a text request) out of an A2A message."""
    parts = message.get("parts") or []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "data" and isinstance(part.get("data"), dict):
            data = part["data"]
            if any(section in data for section in ("handoff", "routing", "expectations", "constraints")):
                return data, None
    text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("kind") == "text" and isinstance(p.get("text"), str)]
    if text_parts:
        return None, "\n".join(t for t in text_parts if t)
    return None, None


def sse_encode(event_type: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


class A2ADispatcher:
    """Maps JSON-RPC requests onto daemon operations."""

    def __init__(self, daemon: Any) -> None:
        self.daemon = daemon

    def handle(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            return jsonrpc_error(body.get("id") if isinstance(body, dict) else None, ERR_INVALID_PARAMS, "Expected a JSON-RPC 2.0 request")
        request_id = body.get("id")
        method = str(body["method"])
        params = body.get("params") or {}
        if method == "message/send":
            return self._send(request_id, params)
        if method == "tasks/get":
            return self._get(request_id, params)
        if method == "tasks/cancel":
            return self._cancel(request_id, params)
        if method == "tasks/followup":
            return self._followup(request_id, params)
        return jsonrpc_error(request_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method}")

    def _send(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message")
        if not isinstance(message, dict):
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, "params.message is required")
        data, text = _extract_handoff(message)
        metadata = message.get("metadata") or {}
        maestro_meta = metadata.get("maestro") if isinstance(metadata, dict) else None
        maestro_meta = maestro_meta if isinstance(maestro_meta, dict) else {}
        workspace = maestro_meta.get("workspace")
        if not workspace:
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, "message.metadata.maestro.workspace is required")
        try:
            from .handoff import HandoffDoc, from_dict

            if data is not None:
                doc = from_dict(data)
            else:
                if not text:
                    return jsonrpc_error(request_id, ERR_INVALID_PARAMS, "Message carries neither a handoff data part nor text")
                named_target = maestro_meta.get("target_agent")
                doc = HandoffDoc(
                    title=str(maestro_meta.get("title") or "A2A delegation"),
                    request=text,
                    target_agent=str(named_target or self.daemon.default_target()),
                    # A caller-named target is an explicit choice; the implicit
                    # fallback stays open for [defaults] resolution / the
                    # routing question at delegate time.
                    explicit_target=bool(named_target),
                )
            # Cross-machine hop semantics: the handoff's target_agent names the
            # *hop* (e.g. machine B's "remote-b" spec), which does not exist on
            # this machine. When it cannot be resolved locally, re-target to
            # this daemon's default agent — "delegate to that machine" means
            # "that machine runs its own default agent with this handoff".
            from .agents import BUILTIN_ADAPTERS

            if self.daemon.registry.get(doc.target_agent) is None and doc.target_agent not in BUILTIN_ADAPTERS:
                doc.target_agent = self.daemon.default_target()
            result = self.daemon.delegate(doc, str(workspace))
        except (ValueError, KeyError) as exc:
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, str(exc))
        if result.get("queued"):
            task_obj = {"kind": "task", "id": None, "status": {"state": STATE_SUBMITTED, "timestamp": result["ts"]}, "metadata": {"queued": True}}
        else:
            task_obj = self.daemon.status_a2a(str(result["task_id"]))
        return jsonrpc_ok(request_id, {"task": task_obj})

    def _get(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        task_ref = params.get("id")
        if not isinstance(task_ref, str) or not task_ref:
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, "params.id is required")
        try:
            return jsonrpc_ok(request_id, {"task": self.daemon.status_a2a(self.daemon.resolve(task_ref))})
        except KeyError as exc:
            return jsonrpc_error(request_id, ERR_TASK_NOT_FOUND, str(exc.args[0]))

    def _cancel(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        task_ref = params.get("id")
        if not isinstance(task_ref, str) or not task_ref:
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, "params.id is required")
        try:
            result = self.daemon.cancel(self.daemon.resolve(task_ref), reason=str(params.get("reason") or ""))
        except KeyError as exc:
            return jsonrpc_error(request_id, ERR_TASK_NOT_FOUND, str(exc.args[0]))
        except ValueError as exc:
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, str(exc))
        return jsonrpc_ok(request_id, {"task": self.daemon.status_a2a(result["task_id"])})

    def _followup(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        task_ref = params.get("id")
        if not isinstance(task_ref, str) or not task_ref:
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, "params.id is required")
        instruction = params.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, "params.instruction is required (non-empty string)")
        context_mode = params.get("context_mode", "reuse")
        if context_mode not in ("reuse", "fresh"):
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, "params.context_mode must be 'reuse' or 'fresh'")
        try:
            result = self.daemon.followup(self.daemon.resolve(task_ref), instruction, context_mode=context_mode)
        except KeyError as exc:
            return jsonrpc_error(request_id, ERR_TASK_NOT_FOUND, str(exc.args[0]))
        except ValueError as exc:
            return jsonrpc_error(request_id, ERR_INVALID_PARAMS, str(exc))
        return jsonrpc_ok(request_id, {"task": self.daemon.status_a2a(result["task_id"])})


def new_message_id() -> str:
    return f"msg-{uuid.uuid4().hex[:12]}"
