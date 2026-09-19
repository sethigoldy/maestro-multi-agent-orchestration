// SSE + REST glue for the Maestro daemon. Strictly event-driven: one initial
// fetch for the task list, then a single EventSource for everything else.

import { authHeaders, withToken } from "./auth.js";

export function unwrap(envelope) {
  // TaskEvent.to_dict nests the payload under "data".
  return (envelope && envelope.data) || {};
}

// GET /tasks speaks A2A (id / status.state / metadata.*); the console works
// with a flat internal shape. This is the single place that bridges them.
export function normalizeTask(record) {
  const meta = record.metadata || {};
  return {
    task_id: record.id,
    title: meta.title,
    state: (record.status && record.status.state) || "unknown",
    workspace: meta.workspace,
    branch: meta.branch,
    origin_agent: meta.origin_agent,
    target_agent: meta.target_agent,
    usage: meta.usage || null,
    attempts: meta.attempts || [],
    error: meta.error || null,
  };
}

export function loadTasks() {
  return fetch(withToken("/tasks"), { headers: { Accept: "application/json", ...authHeaders() } })
    .then((res) => res.json())
    .then((body) => (body.tasks || []).map(normalizeTask));
}

// The execution receipt is a durable projection of the same task state that
// /tasks serves — one extra fetch per selected task, no second data model.
export function loadReceipt(taskId) {
  return fetch(withToken(`/tasks/${encodeURIComponent(taskId)}/receipt`), {
    headers: { Accept: "application/json", ...authHeaders() },
  })
    .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))));
}

export function connectEvents(handlers) {
  const source = new EventSource(withToken("/events"));
  for (const type of ["state", "output", "usage"]) {
    source.addEventListener(type, (message) => {
      let envelope;
      try {
        envelope = JSON.parse(message.data);
      } catch {
        return; // keepalive or malformed frame: ignore
      }
      const data = unwrap(envelope);
      if (typeof handlers[type] === "function") {
        handlers[type](envelope.task_id, data, envelope);
      }
    });
  }
  source.onopen = () => {
    if (typeof handlers.open === "function") handlers.open();
  };
  source.onerror = () => {
    if (typeof handlers.error === "function") handlers.error();
  };
  return source;
}
