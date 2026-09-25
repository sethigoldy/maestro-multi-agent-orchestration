"""The console's action logic (web/src/lib/rpc.js, web/src/lib/actions.js), run in node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "web" / "src" / "lib"


def _run(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; console JS not tested")
    prelude = 'globalThis.sessionStorage = { store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = v; } };\n'
    result = subprocess.run([node, "--input-type=module", "-e", prelude + script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _import(name: str) -> str:
    return json.dumps(str(LIB / name))


def test_rpc_posts_json_rpc_with_the_token_and_returns_the_result():
    out = _run(f"""
import {{ rpc }} from {_import("rpc.js")};
sessionStorage.setItem("maestro_token", "tok");
const calls = [];
const fake = async (url, init) => {{ calls.push({{ url, init }}); return {{ ok: true, status: 200, json: async () => ({{ jsonrpc: "2.0", id: 1, result: {{ task_id: "t", state: "working" }} }}) }}; }};
const result = await rpc("tasks/answer", {{ id: "t", answer: "codex" }}, fake);
const sent = JSON.parse(calls[0].init.body);
console.log(JSON.stringify({{ url: calls[0].url, method: calls[0].init.method, type: calls[0].init.headers["Content-Type"], auth: calls[0].init.headers.Authorization, rpc: sent.method, params: sent.params, result }}));
""")
    got = json.loads(out)
    assert got["url"] == "/" and got["method"] == "POST" and got["type"] == "application/json"
    assert got["auth"] == "Bearer tok"
    assert got["rpc"] == "tasks/answer" and got["params"] == {"id": "t", "answer": "codex"}
    assert got["result"] == {"task_id": "t", "state": "working"}


def test_rpc_raises_the_daemons_own_error_message():
    out = _run(f"""
import {{ rpc }} from {_import("rpc.js")};
const refuse = async () => ({{ ok: false, status: 400, json: async () => ({{ jsonrpc: "2.0", id: 1, error: {{ code: -32602, message: "Task t is not awaiting input (state=working)" }} }}) }});
const broken = async () => ({{ ok: false, status: 502, json: async () => {{ throw new Error("not json"); }} }});
const messages = [];
for (const f of [refuse, broken]) {{ try {{ await rpc("tasks/answer", {{}}, f); }} catch (e) {{ messages.push(e.message); }} }}
console.log(JSON.stringify(messages));
""")
    assert json.loads(out) == ["Task t is not awaiting input (state=working)", "the daemon answered HTTP 502"]


def test_which_actions_each_state_allows():
    out = _run(f"""
import {{ availableActions }} from {_import("actions.js")};
const rows = {{}};
for (const [name, task] of Object.entries({{
  working: {{ state: "working" }},
  submitted: {{ state: "submitted" }},
  parked: {{ state: "input-required" }},
  done_in_workspace: {{ state: "completed", run_dir_kind: "workspace" }},
  done_in_worktree: {{ state: "failed", run_dir_kind: "worktree" }},
  worktree_gone: {{ state: "completed", run_dir_kind: "worktree", run_dir_missing: true }},
  canceled: {{ state: "canceled", run_dir_kind: "worktree" }},
}})) rows[name] = availableActions(task);
console.log(JSON.stringify(rows));
""")
    rows = json.loads(out)
    assert rows["working"] == {"answer": False, "cancel": True, "followup": False, "cleanup": False, "rename": False}
    assert rows["submitted"]["cancel"] is True and rows["submitted"]["rename"] is False
    assert rows["parked"] == {"answer": True, "cancel": True, "followup": False, "cleanup": False, "rename": True}
    assert rows["done_in_workspace"] == {"answer": False, "cancel": False, "followup": True, "cleanup": False, "rename": True}
    assert rows["done_in_worktree"]["cleanup"] is True and rows["canceled"]["cleanup"] is True
    assert rows["worktree_gone"]["cleanup"] is False


def test_routing_answer_text_and_agent_options():
    out = _run(f"""
import {{ routingAnswer, agentOptions }} from {_import("actions.js")};
const agents = [
  {{ name: "claude", kind: "claude_code", model: null, status: {{ found: false, version: null }} }},
  {{ name: "codex", kind: "codex", model: "gpt-6-luna", status: {{ found: true, version: "codex-cli 0.146.1" }} }},
];
console.log(JSON.stringify({{ a: routingAnswer("codex", ""), b: routingAnswer("codex", " gpt-6-luna "), options: agentOptions(agents) }}));
""")
    got = json.loads(out)
    assert got["a"] == "agent=codex" and got["b"] == "agent=codex model=gpt-6-luna"
    assert [o["name"] for o in got["options"]] == ["codex", "claude"]  # installed agents first
    assert got["options"][0] == {"name": "codex", "installed": True, "model": "gpt-6-luna", "label": "codex — codex-cli 0.146.1 — default model gpt-6-luna"}
    assert got["options"][1]["label"] == "claude — not installed"


def test_needs_you_lists_waiting_tasks_oldest_first():
    out = _run(f"""
import {{ needsYou }} from {_import("actions.js")};
const tasks = {{
  a: {{ task_id: "a", state: "input-required", started_at: "2026-09-25T10:00:00+00:00" }},
  b: {{ task_id: "b", state: "working", started_at: "2026-09-25T09:00:00+00:00" }},
  c: {{ task_id: "c", state: "input-required", started_at: "2026-09-25T08:00:00+00:00" }},
}};
console.log(JSON.stringify(needsYou(tasks).map((t) => t.task_id)));
""")
    assert json.loads(out) == ["c", "a"]


def test_agent_options_include_installed_agents_that_are_not_registered():
    out = _run(f"""
import {{ agentOptions }} from {_import("actions.js")};
const registered = [{{ name: "cc", kind: "claude_code", model: null, status: {{ found: true, version: "2.1" }} }}];
const discovered = [{{ name: "codex", kind: "codex", status: {{ found: true }} }}];
console.log(JSON.stringify(agentOptions(registered, discovered)));
""")
    options = json.loads(out)
    assert [o["name"] for o in options] == ["cc", "codex"]
    assert options[1]["label"] == "codex — installed, not registered"


def test_route_label_does_not_show_a_placeholder_agent():
    out = _run(f"""
import {{ routeLabel }} from {_import("actions.js")};
console.log(JSON.stringify([
  routeLabel({{ target_agent: "codex", awaiting: "routing" }}),
  routeLabel({{ target_agent: "codex", awaiting: null }}),
  routeLabel({{ target_agent: null }}),
]));
""")
    assert json.loads(out) == ["agent not chosen yet", "→ codex", ""]


def test_parse_diff_marks_each_line_kind():
    text = "diff --git a/app.py b/app.py\nindex 1..2 100644\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,3 @@\n x = 1\n+y = 2\n-old\n"
    out = _run(f"""
import {{ parseDiff }} from {_import("diff.js")};
console.log(JSON.stringify(parseDiff({json.dumps(text)})));
""")
    kinds = [(line["kind"], line["text"]) for line in json.loads(out)]
    assert kinds == [
        ("file", "diff --git a/app.py b/app.py"), ("meta", "index 1..2 100644"), ("meta", "--- a/app.py"),
        ("meta", "+++ b/app.py"), ("hunk", "@@ -1 +1,3 @@"), ("context", " x = 1"), ("add", "+y = 2"), ("del", "-old"),
    ]


_TASKS = """{
  a: { task_id: "task-a", title: "Fix login", state: "working", target_agent: "codex", workspace: "/w1", started_at: "2026-09-25T10:00:00+00:00" },
  b: { task_id: "task-b", title: "Write docs", state: "input-required", target_agent: "claude", workspace: "/w2", started_at: "2026-09-25T11:00:00+00:00" },
  c: { task_id: "task-c", title: "Tidy CSS", state: "submitted", queued: true, target_agent: "codex", workspace: "/w1", started_at: "2026-09-25T09:00:00+00:00" },
  d: { task_id: "task-d", title: "Old work", state: "completed", target_agent: "codex", workspace: "/w1", started_at: "2026-09-24T09:00:00+00:00" },
}"""


def test_filters_search_and_newest_first_order():
    out = _run(f"""
import {{ filterTasks }} from {_import("overview.js")};
const tasks = {_TASKS};
const ids = (f) => filterTasks(tasks, f).map((t) => t.task_id);
console.log(JSON.stringify({{
  all: ids({{}}),
  needs: ids({{ view: "needs-you" }}),
  running: ids({{ view: "running" }}),
  queued: ids({{ view: "queued" }}),
  done: ids({{ view: "finished" }}),
  codex: ids({{ agent: "codex" }}),
  search: ids({{ query: "LOGIN" }}),
  byId: ids({{ query: "task-d" }}),
}}));
""")
    got = json.loads(out)
    assert got["all"] == ["task-b", "task-a", "task-c", "task-d"]  # newest first
    assert got["needs"] == ["task-b"] and got["running"] == ["task-a"] and got["queued"] == ["task-c"] and got["done"] == ["task-d"]
    assert got["codex"] == ["task-a", "task-c", "task-d"] and got["search"] == ["task-a"] and got["byId"] == ["task-d"]


def test_grouping_by_workspace_with_limits():
    out = _run(f"""
import {{ filterTasks, groupByWorkspace }} from {_import("overview.js")};
const tasks = filterTasks({_TASKS}, {{}});
const workspaces = [{{ workspace: "/w1", running: 1, queued: 1, max_parallel: 4 }}];
console.log(JSON.stringify(groupByWorkspace(tasks, workspaces).map((g) => [g.workspace, g.label, g.tasks.map((t) => t.task_id)])));
""")
    assert json.loads(out) == [
        ["/w2", "/w2", ["task-b"]],
        ["/w1", "/w1 · 1 of 4 running · 1 queued", ["task-a", "task-c", "task-d"]],
    ]


def test_which_state_changes_notify():
    out = _run(f"""
import {{ notificationFor }} from {_import("overview.js")};
const t = {{ task_id: "task-a", title: "Fix login" }};
console.log(JSON.stringify([
  notificationFor("working", {{ ...t, state: "input-required" }}),
  notificationFor("working", {{ ...t, state: "completed" }}),
  notificationFor("working", {{ ...t, state: "failed" }}),
  notificationFor("submitted", {{ ...t, state: "working" }}),
  notificationFor("completed", {{ ...t, state: "completed" }}),
]));
""")
    got = json.loads(out)
    assert got[0] == {"title": "Maestro: a task needs you", "body": "Fix login is waiting for an answer."}
    assert got[1] == {"title": "Maestro: task completed", "body": "Fix login completed."}
    assert got[2] == {"title": "Maestro: task failed", "body": "Fix login failed."}
    assert got[3] is None and got[4] is None  # starting to work, or no change, does not notify
