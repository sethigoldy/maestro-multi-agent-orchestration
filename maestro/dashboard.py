"""Single-file web dashboard served by the daemon at ``GET /``.

Vanilla HTML + JS, no build step and no dependencies. It is strictly reactive:
the initial task list comes from ``GET /tasks`` (one fetch), and everything
after that flows over the global SSE stream ``GET /events`` — no polling.
"""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Maestro</title>
<style>
  :root { color-scheme: dark; }
  body { font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0; background: #101418; color: #d7e0e8; }
  header { padding: 12px 16px; border-bottom: 1px solid #2a3540; display: flex; gap: 12px; align-items: baseline; }
  header h1 { font-size: 16px; margin: 0; letter-spacing: 1px; }
  header .hint { color: #7d8b98; font-size: 12px; }
  main { display: grid; grid-template-columns: 340px 1fr; gap: 0; height: calc(100vh - 46px); }
  #tasks { overflow-y: auto; border-right: 1px solid #2a3540; }
  .card { padding: 10px 14px; border-bottom: 1px solid #1d262e; cursor: pointer; }
  .card:hover, .card.selected { background: #182129; }
  .card .id { color: #7d8b98; font-size: 11px; }
  .card .title { margin-top: 2px; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 8px; font-size: 11px; margin-right: 6px; background: #223041; color: #9fc2e8; }
  .badge.completed { background: #1d3a2a; color: #7fd6a4; }
  .badge.failed { background: #3d2226; color: #f08a8a; }
  .badge.canceled { background: #3a3320; color: #e0c56b; }
  .badge.working, .badge.submitted { background: #243447; color: #8fc1ea; }
  .badge.input-required { background: #3a3320; color: #e0c56b; }
  #detail { overflow-y: auto; padding: 14px 18px; }
  #detail h2 { font-size: 14px; margin: 0 0 4px; }
  .meta { color: #7d8b98; font-size: 12px; margin-bottom: 10px; white-space: pre-wrap; }
  #transcript { background: #0b0f13; border: 1px solid #1d262e; padding: 10px; height: 55vh; overflow-y: auto; white-space: pre-wrap; }
  .evt-state { color: #8fc1ea; }
  .evt-usage { color: #7fd6a4; }
</style>
</head>
<body>
<header><h1>MAESTRO</h1><span class="hint">local broker dashboard — event-driven, no polling</span></header>
<main>
  <div id="tasks"></div>
  <div id="detail"><h2>Select a task</h2></div>
</main>
<script>
const tasks = new Map();       // id -> {state, title, meta}
let selected = null;

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function renderList() {
  const box = document.getElementById("tasks");
  box.innerHTML = "";
  for (const [id, t] of tasks) {
    const card = document.createElement("div");
    card.className = "card" + (id === selected ? " selected" : "");
    card.innerHTML = '<span class="badge ' + esc(t.state) + '">' + esc(t.state) + '</span>' +
      '<div class="title">' + esc(t.title || id) + '</div><div class="id">' + esc(id) + '</div>';
    card.onclick = () => select(id);
    box.appendChild(card);
  }
}

function detailPane(id) {
  const t = tasks.get(id);
  if (!t) return;
  document.getElementById("detail").innerHTML =
    '<h2>' + esc(t.title || id) + '</h2>' +
    '<div class="meta">' + esc(JSON.stringify(t.meta, null, 2)) + '</div>' +
    '<div id="transcript"></div>';
}

function select(id) { selected = id; renderList(); detailPane(id); }

function appendLine(text, cls) {
  const box = document.getElementById("transcript");
  if (!box) return;
  const div = document.createElement("div");
  if (cls) div.className = cls;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

async function refresh() {
  const res = await fetch("/tasks");
  const list = (await res.json()).tasks || [];
  tasks.clear();
  for (const t of list) {
    const id = t.id || "";
    if (!id) continue;
    tasks.set(id, { state: (t.status && t.status.state) || "unknown", title: (t.metadata || {}).title, meta: t.metadata || {} });
  }
  renderList();
}

function onEvent(ev) {
  const envelope = ev.data || {};
  const data = envelope.data || {};   // TaskEvent.to_dict nests the payload under "data"
  const id = envelope.task_id;
  if (!id) return;
  const t = tasks.get(id) || (tasks.set(id, { state: "unknown", title: id, meta: {} }), tasks.get(id));
  if (ev.event === "state") {
    t.state = data.state || t.state;
    if (data.question) t.meta.last_question = data.question;
    renderList();
    if (id === selected) { detailPane(id); appendLine("[state] " + (data.state || "?") + (data.error ? " — " + data.error : ""), "evt-state"); }
  } else if (ev.event === "usage") {
    t.meta.usage = data;
    if (id === selected) appendLine("[usage] " + esc(JSON.stringify(data)), "evt-usage");
  } else if (ev.event === "output") {
    if (id === selected) appendLine(data.line || "");
  }
}

refresh().catch(() => {});
const source = new EventSource("/events");
source.onmessage = onEvent;
for (const name of ["state", "usage", "output"]) source.addEventListener(name, onEvent);
</script>
</body>
</html>
"""
