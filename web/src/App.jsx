import React, { useEffect, useMemo, useReducer } from "react";
import { loadTasks, connectEvents } from "./lib/events.js";
import TaskCard from "./components/TaskCard.jsx";
import DetailPane from "./components/DetailPane.jsx";

const TRANSCRIPT_CAP = 2000;

function emptyState() {
  return { tasks: {}, order: [], selected: null, live: false, loaded: false };
}

function applyEvent(state, taskId, type, data) {
  const existing = state.tasks[taskId] || { task_id: taskId, state: "submitted" };
  const next = { ...existing };
  if (type === "state") {
    next.state = data.state;
    if (data.error) next.error = data.error;
    if (data.question) next.question = data.question;
  } else if (type === "output") {
    const line = typeof data.line === "string" ? data.line : JSON.stringify(data);
    next.transcript = [...(next.transcript || []), line].slice(-TRANSCRIPT_CAP);
  } else if (type === "usage") {
    next.usage = { ...(next.usage || {}), ...data };
  }
  const order = state.order.includes(taskId) ? state.order : [taskId, ...state.order];
  return { ...state, tasks: { ...state.tasks, [taskId]: next }, order };
}

function reducer(state, action) {
  if (action.kind === "event") {
    return applyEvent(state, action.taskId, action.type, action.data);
  }
  switch (action.type) {
    case "load": {
      const tasks = {};
      for (const record of action.records) tasks[record.task_id] = record;
      return { ...state, tasks, order: action.records.map((r) => r.task_id), loaded: true };
    }
    case "select":
      return { ...state, selected: action.taskId };
    case "live":
      return { ...state, live: action.live };
    default:
      return state;
  }
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, undefined, emptyState);

  useEffect(() => {
    let source = null;
    loadTasks()
      .then((records) => dispatch({ type: "load", records }))
      .catch(() => dispatch({ type: "live", live: false }));
    source = connectEvents({
      state: (taskId, data) => dispatch({ kind: "event", taskId, type: "state", data }),
      output: (taskId, data) => dispatch({ kind: "event", taskId, type: "output", data }),
      usage: (taskId, data) => dispatch({ kind: "event", taskId, type: "usage", data }),
      open: () => dispatch({ type: "live", live: true }),
      error: () => dispatch({ type: "live", live: false }),
    });
    return () => source.close();
  }, []);

  const counts = useMemo(() => {
    const out = {};
    for (const id of state.order) {
      const s = state.tasks[id].state || "unknown";
      out[s] = (out[s] || 0) + 1;
    }
    return out;
  }, [state]);

  const selectedTask = state.selected ? state.tasks[state.selected] : null;

  return (
    <div>
      <header style={styles.header}>
        <span style={styles.logo}>MAESTRO</span>
        <span style={{ color: "var(--dim)" }}>console</span>
        <span style={{ flex: 1 }} />
        {Object.entries(counts).map(([s, n]) => (
          <span key={s} style={{ ...styles.count, borderColor: stateColor(s) }}>
            {n} {s}
          </span>
        ))}
        <span
          title={state.live ? "event stream connected" : "event stream disconnected"}
          style={{ ...styles.dot, background: state.live ? "var(--ok)" : "var(--err)" }}
        />
      </header>
      <div style={styles.body}>
        <aside style={styles.list}>
          {!state.loaded && <div style={styles.dim}>loading tasks…</div>}
          {state.loaded && state.order.length === 0 && (
            <div style={styles.dim}>no tasks yet — delegate one from any agent or the CLI</div>
          )}
          {state.order.map((id) => (
            <TaskCard
              key={id}
              task={state.tasks[id]}
              selected={id === state.selected}
              onSelect={() => dispatch({ type: "select", taskId: id })}
            />
          ))}
        </aside>
        <main style={styles.detail}>
          {selectedTask ? (
            <DetailPane task={selectedTask} />
          ) : (
            <div style={{ ...styles.dim, padding: 24 }}>select a task</div>
          )}
        </main>
      </div>
    </div>
  );
}

export function stateColor(state) {
  switch (state) {
    case "completed":
      return "var(--ok)";
    case "failed":
    case "canceled":
      return "var(--err)";
    case "input-required":
      return "var(--warn)";
    default:
      return "var(--accent)";
  }
}

const styles = {
  header: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "10px 16px",
    borderBottom: "1px solid var(--border)",
    background: "var(--panel)",
  },
  logo: { fontWeight: 700, letterSpacing: 2 },
  count: {
    fontSize: 12,
    padding: "2px 8px",
    border: "1px solid var(--border)",
    borderRadius: 10,
    color: "var(--dim)",
  },
  dot: { width: 10, height: 10, borderRadius: "50%", display: "inline-block" },
  body: { display: "flex", flex: 1, minHeight: 0 },
  list: { width: 340, minWidth: 260, overflowY: "auto", borderRight: "1px solid var(--border)" },
  detail: { flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" },
  dim: { color: "var(--dim)", padding: 12 },
};
