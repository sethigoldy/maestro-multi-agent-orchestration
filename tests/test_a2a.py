from __future__ import annotations

import json

from maestro.a2a import (
    ERR_INVALID_PARAMS,
    ERR_METHOD_NOT_FOUND,
    ERR_TASK_NOT_FOUND,
    A2ADispatcher,
    agent_card,
    jsonrpc_error,
    jsonrpc_ok,
    new_message_id,
    sse_encode,
)


class _FakeRegistry:
    """Empty registry: nothing is resolvable locally (hop-name re-targeting applies)."""

    def get(self, name):
        return None


class FakeDaemon:
    def __init__(self):
        self.calls = []
        self.registry = _FakeRegistry()

    def default_target(self):
        return "codex"

    def delegate(self, doc, workspace):
        self.calls.append(("delegate", doc, workspace))
        if doc.title == "queued":
            return {"task_id": None, "queued": True, "state": "submitted", "ts": "t0"}
        return {"task_id": f"task-{doc.title}", "queued": False, "state": "submitted", "ts": "t0"}

    def status_a2a(self, task_id):
        if not task_id.startswith("task-"):
            raise KeyError(f"Unknown task reference {task_id!r}")
        return {"kind": "task", "id": task_id, "status": {"state": "completed", "timestamp": "t1"}}

    def resolve(self, ref):
        if not str(ref).startswith("task-"):
            raise KeyError(f"Unknown task reference {ref!r}")
        return ref

    def cancel(self, task_id, reason=""):
        if task_id == "task-done":
            raise ValueError("already finished")
        self.calls.append(("cancel", task_id, reason))
        return {"task_id": task_id, "state": "canceled"}

    def followup(self, task_id, instruction, context_mode="reuse"):
        if task_id == "task-shallow":
            raise ValueError("Max delegation depth exceeded")
        self.calls.append(("followup", task_id, instruction, context_mode))
        return {"task_id": task_id, "state": "submitted"}


def _dispatch():
    return A2ADispatcher(FakeDaemon())


# ------------------------------------------------------------------ primitives
def test_agent_card_shape():
    card = agent_card(name="maestro-node", url="http://127.0.0.1:9", skills=[{"id": "codex"}])
    assert card["name"] == "maestro-node" and card["url"].endswith(":9")
    assert card["capabilities"]["streaming"] is True
    assert card["skills"] == [{"id": "codex"}]


def test_jsonrpc_helpers():
    assert jsonrpc_ok(1, {"a": 1}) == {"jsonrpc": "2.0", "id": 1, "result": {"a": 1}}
    assert jsonrpc_error(2, -32601, "nope")["error"]["code"] == -32601


def test_sse_encode():
    text = sse_encode("state", {"state": "working"})
    assert text.startswith("event: state\n") and text.endswith("\n\n")
    payload = json.loads(text.split("data: ", 1)[1].strip())
    assert payload == {"state": "working"}


def test_new_message_id_unique():
    assert new_message_id() != new_message_id()


# ------------------------------------------------------------------ dispatcher
def _req(method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return body


def test_rejects_non_jsonrpc_body():
    resp = A2ADispatcher(FakeDaemon()).handle({"hello": 1})
    assert resp["error"]["code"] == ERR_INVALID_PARAMS


def test_method_not_found():
    resp = _dispatch().handle(_req("tasks/unknown"))
    assert resp["error"]["code"] == ERR_METHOD_NOT_FOUND


def test_send_requires_message():
    resp = _dispatch().handle(_req("message/send", {}))
    assert resp["error"]["code"] == ERR_INVALID_PARAMS and "params.message" in resp["error"]["message"]


def test_send_requires_workspace():
    msg = {"role": "user", "parts": [{"kind": "text", "text": "do it"}]}
    resp = _dispatch().handle(_req("message/send", {"message": msg}))
    assert resp["error"]["code"] == ERR_INVALID_PARAMS and "workspace" in resp["error"]["message"]


def test_send_text_message_delegates_with_default_target():
    daemon = FakeDaemon()
    dispatcher = A2ADispatcher(daemon)
    msg = {
        "role": "user",
        "parts": [{"kind": "text", "text": "line one"}, {"kind": "text", "text": "line two"}],
        "metadata": {"maestro": {"workspace": "/ws", "title": "From A2A"}},
    }
    resp = dispatcher.handle(_req("message/send", {"message": msg}))
    assert "result" in resp and resp["result"]["task"]["id"] == "task-From A2A"
    doc, workspace = daemon.calls[0][1], daemon.calls[0][2]
    assert doc.request == "line one\nline two" and doc.target_agent == "codex" and workspace == "/ws"


def test_send_data_part_carries_full_handoff():
    from maestro.handoff import HandoffDoc

    daemon = FakeDaemon()
    dispatcher = A2ADispatcher(daemon)
    data = HandoffDoc(title="Doc task", request="R", target_agent="pi").to_dict()
    msg = {"role": "user", "parts": [{"kind": "data", "data": data}], "metadata": {"maestro": {"workspace": "/ws"}}}
    resp = dispatcher.handle(_req("message/send", {"message": msg}))
    assert resp["result"]["task"]["id"] == "task-Doc task"
    doc = daemon.calls[0][1]
    assert isinstance(doc, HandoffDoc) and doc.target_agent == "pi"


def test_send_queued_returns_placeholder_task():
    from maestro.handoff import HandoffDoc

    dispatcher = A2ADispatcher(FakeDaemon())
    data = HandoffDoc(title="queued", request="R").to_dict()
    msg = {"role": "user", "parts": [{"kind": "data", "data": data}], "metadata": {"maestro": {"workspace": "/ws"}}}
    resp = dispatcher.handle(_req("message/send", {"message": msg}))
    assert resp["result"]["task"]["status"]["state"] == "submitted"
    assert resp["result"]["task"]["metadata"]["queued"] is True


def test_send_empty_message_rejected():
    msg = {"role": "user", "parts": [], "metadata": {"maestro": {"workspace": "/ws"}}}
    resp = _dispatch().handle(_req("message/send", {"message": msg}))
    assert resp["error"]["code"] == ERR_INVALID_PARAMS and "neither" in resp["error"]["message"]


def test_send_invalid_handoff_maps_to_param_error():
    from maestro.handoff import HandoffDoc

    daemon = FakeDaemon()
    dispatcher = A2ADispatcher(daemon)
    data = HandoffDoc(title="", request="R").to_dict()  # invalid: empty title
    msg = {"role": "user", "parts": [{"kind": "data", "data": data}], "metadata": {"maestro": {"workspace": "/ws"}}}
    resp = dispatcher.handle(_req("message/send", {"message": msg}))
    assert resp["error"]["code"] == ERR_INVALID_PARAMS


def test_tasks_get_and_unknown():
    dispatcher = _dispatch()
    resp = dispatcher.handle(_req("tasks/get", {"id": "task-1"}))
    assert resp["result"]["task"]["status"]["state"] == "completed"
    bad = dispatcher.handle(_req("tasks/get", {}))
    assert bad["error"]["code"] == ERR_INVALID_PARAMS
    missing = dispatcher.handle(_req("tasks/get", {"id": "missing"}))
    assert missing["error"]["code"] == ERR_TASK_NOT_FOUND


def test_tasks_cancel_paths():
    dispatcher = _dispatch()
    ok = dispatcher.handle(_req("tasks/cancel", {"id": "task-1", "reason": "stop"}))
    assert ok["result"]["task"]["status"]["state"] == "completed"  # fake returns post-cancel status_a2a
    already = dispatcher.handle(_req("tasks/cancel", {"id": "task-done"}))
    assert already["error"]["code"] == ERR_INVALID_PARAMS and "finished" in already["error"]["message"]
    missing = dispatcher.handle(_req("tasks/cancel", {"id": "missing"}))
    assert missing["error"]["code"] == ERR_TASK_NOT_FOUND


def test_send_data_part_without_sections_falls_back_to_text():
    daemon = FakeDaemon()
    dispatcher = A2ADispatcher(daemon)
    msg = {
        "role": "user",
        "parts": [
            {"kind": "data", "data": {"unrelated": "payload"}},
            {"kind": "text", "text": "fallback text"},
        ],
        "metadata": {"maestro": {"workspace": "/ws"}},
    }
    resp = dispatcher.handle(_req("message/send", {"message": msg}))
    assert "result" in resp
    doc = daemon.calls[0][1]
    assert doc.request == "fallback text"


def test_tasks_cancel_empty_id():
    resp = _dispatch().handle(_req("tasks/cancel", {"id": ""}))
    assert resp["error"]["code"] == ERR_INVALID_PARAMS


# ------------------------------------------------------------------ tasks/followup
def test_tasks_followup_happy_path_defaults_to_reuse():
    daemon = FakeDaemon()
    dispatcher = A2ADispatcher(daemon)
    resp = dispatcher.handle(_req("tasks/followup", {"id": "task-1", "instruction": "keep going"}))
    assert "result" in resp, resp
    assert resp["result"]["task"]["id"] == "task-1"
    assert daemon.calls[0] == ("followup", "task-1", "keep going", "reuse")


def test_tasks_followup_passes_context_mode():
    daemon = FakeDaemon()
    dispatcher = A2ADispatcher(daemon)
    resp = dispatcher.handle(_req("tasks/followup", {"id": "task-1", "instruction": "clean turn", "context_mode": "fresh"}))
    assert "result" in resp, resp
    assert daemon.calls[0][3] == "fresh"


def test_tasks_followup_param_errors():
    dispatcher = _dispatch()
    for params in (
        {},  # no id
        {"id": ""},
        {"id": 7, "instruction": "x"},
        {"id": "task-1"},  # no instruction
        {"id": "task-1", "instruction": "   "},
        {"id": "task-1", "instruction": "x", "context_mode": "sometimes"},
    ):
        resp = dispatcher.handle(_req("tasks/followup", params))
        assert resp["error"]["code"] == ERR_INVALID_PARAMS, (params, resp)


def test_tasks_followup_unknown_task():
    resp = _dispatch().handle(_req("tasks/followup", {"id": "missing", "instruction": "x"}))
    assert resp["error"]["code"] == ERR_TASK_NOT_FOUND


def test_tasks_followup_depth_guard_maps_to_param_error():
    resp = _dispatch().handle(_req("tasks/followup", {"id": "task-shallow", "instruction": "one more"}))
    assert resp["error"]["code"] == ERR_INVALID_PARAMS and "depth" in resp["error"]["message"].lower()


def test_send_retargets_unresolvable_hop_name_to_local_default():
    """Cross-machine semantics: a handoff whose target is the remote hop name
    (unknown locally) runs under this daemon's default agent."""
    d = _dispatch()
    doc_msg = {
        "kind": "message",
        "role": "user",
        "parts": [
            {"kind": "data", "data": {
                "handoff": {"title": "Hop work", "request": "Do it"},
                "routing": {"target_agent": "remote-b", "origin_agent": "human"},
            }}
        ],
        "metadata": {"maestro": {"workspace": "/ws"}},
    }
    resp = d.handle(_req("message/send", {"message": doc_msg}))
    assert "error" not in resp, resp
    (kind, doc, ws) = d.daemon.calls[0]
    assert kind == "delegate" and ws == "/ws"
    assert doc.target_agent == "codex"  # re-targeted from the hop name


def test_send_keeps_resolvable_builtin_target():
    """A target that is a builtin kind (e.g. codex) runs as-is — no re-targeting."""
    d = _dispatch()
    doc_msg = {
        "kind": "message",
        "role": "user",
        "parts": [
            {"kind": "data", "data": {
                "handoff": {"title": "Direct work", "request": "Do it"},
                "routing": {"target_agent": "codex", "origin_agent": "human"},
            }}
        ],
        "metadata": {"maestro": {"workspace": "/ws"}},
    }
    resp = d.handle(_req("message/send", {"message": doc_msg}))
    assert "error" not in resp, resp
    (kind, doc, ws) = d.daemon.calls[0]
    assert doc.target_agent == "codex"


# ------------------------------------------------ malformed params and unexpected errors
def test_params_that_are_not_an_object_are_rejected():
    dispatcher = _dispatch()
    for method in ("message/send", "tasks/get", "tasks/cancel", "tasks/followup"):
        for params in (["task-1"], "task-1", 5):
            resp = dispatcher.handle(_req(method, params, rid=9))
            assert resp["id"] == 9 and resp["error"]["code"] == ERR_INVALID_PARAMS, (method, params)
            assert "params must be an object" in resp["error"]["message"]
    # Missing or null params still mean "no parameters".
    assert dispatcher.handle({"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": None})["error"]["code"] == ERR_INVALID_PARAMS
    assert "params.id" in dispatcher.handle({"jsonrpc": "2.0", "id": 1, "method": "tasks/get"})["error"]["message"]


def test_message_parts_that_are_not_a_list_are_ignored():
    dispatcher = _dispatch()
    msg = {"role": "user", "parts": 5, "metadata": {"maestro": {"workspace": "/ws"}}}
    resp = dispatcher.handle(_req("message/send", {"message": msg}))
    assert resp["error"]["code"] == ERR_INVALID_PARAMS and "neither" in resp["error"]["message"]


def test_unexpected_exception_becomes_an_internal_error(capsys):
    from maestro.a2a import ERR_INTERNAL

    class Broken(FakeDaemon):
        def status_a2a(self, task_id):
            raise RuntimeError("journal exploded")

    resp = A2ADispatcher(Broken()).handle(_req("tasks/get", {"id": "task-1"}, rid=3))
    assert resp == {"jsonrpc": "2.0", "id": 3, "error": {"code": ERR_INTERNAL, "message": "Internal error: RuntimeError"}}
    assert "journal exploded" in capsys.readouterr().err  # the operator still gets the traceback
