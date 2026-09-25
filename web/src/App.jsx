import React, { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { loadTasks, connectEvents, normalizeTask, loadOutput, loadWorkspaces } from "./lib/events.js";
import { notificationFor } from "./lib/overview.js";
import TaskList from "./components/TaskList.jsx";
import { rpc } from "./lib/rpc.js";
import { needsYou } from "./lib/actions.js";
import DetailPane from "./components/DetailPane.jsx";
import NeedsYou from "./components/NeedsYou.jsx";
import SystemView from "./components/SystemView.jsx";

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
  } else if (type === "branch") {
    if (data.branch) next.branch = data.branch;
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
    case "upsert": {
      // A task's full record from the daemon; keep the output already streamed.
      const record = action.record;
      const existing = state.tasks[record.task_id] || {};
      const merged = { ...existing, ...record, transcript: existing.transcript };
      const order = state.order.includes(record.task_id) ? state.order : [record.task_id, ...state.order];
      return { ...state, tasks: { ...state.tasks, [record.task_id]: merged }, order };
    }
    case "history": {
      // Output from before the page was opened; live lines already received win.
      const existing = state.tasks[action.taskId];
      if (!existing || (existing.transcript && existing.transcript.length)) return state;
      return { ...state, tasks: { ...state.tasks, [action.taskId]: { ...existing, transcript: action.lines.slice(-TRANSCRIPT_CAP) } } };
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
  const [agents, setAgents] = useState({ agents: [], discovered: [] });
  const [workspaces, setWorkspaces] = useState([]);
  const [filters, setFilters] = useState(() => readSetting("maestro_filters", {}));
  const [notify, setNotify] = useState(() => readSetting("maestro_notify", false));
  const [showSystem, setShowSystem] = useState(false);
  // The latest state and settings, for the event handlers set up once below.
  const latest = useRef({ state, notify });
  latest.current = { state, notify };

  useEffect(() => writeSetting("maestro_filters", filters), [filters]);
  useEffect(() => writeSetting("maestro_notify", notify), [notify]);

  const reloadWorkspaces = useCallback(() => {
    loadWorkspaces().then(setWorkspaces).catch(() => {});
  }, []);
  useEffect(reloadWorkspaces, [reloadWorkspaces]);

  async function toggleNotify() {
    if (notify) return setNotify(false);
    if (typeof Notification === "undefined") return window.alert("This browser cannot show notifications.");
    const permission = await Notification.requestPermission();
    setNotify(permission === "granted");
  }

  // Re-read one task from the daemon: after an action, and on every state
  // change, so the page shows the current question, queue reason and run dir.
  const refresh = useCallback((taskId) => {
    rpc("tasks/get", { id: taskId })
      .then((result) => result && result.task && dispatch({ type: "upsert", record: normalizeTask(result.task) }))
      .catch(() => {}); // the daemon went away or forgot the task: keep what is shown
  }, []);

  useEffect(() => {
    rpc("agents/list", {})
      .then((result) => setAgents({ agents: (result && result.agents) || [], discovered: (result && result.discovered) || [] }))
      .catch(() => setAgents({ agents: [], discovered: [] }));
  }, []);

  useEffect(() => {
    let source = null;
    loadTasks()
      .then((records) => dispatch({ type: "load", records }))
      .catch(() => dispatch({ type: "live", live: false }));
    source = connectEvents({
      state: (taskId, data) => {
        const before = latest.current.state.tasks[taskId] || { task_id: taskId };
        const note = notificationFor(before.state, { ...before, state: data.state });
        if (note && latest.current.notify && typeof Notification !== "undefined" && Notification.permission === "granted") {
          new Notification(note.title, { body: note.body });
        }
        dispatch({ kind: "event", taskId, type: "state", data });
        refresh(taskId);
        reloadWorkspaces();
      },
      output: (taskId, data) => dispatch({ kind: "event", taskId, type: "output", data }),
      usage: (taskId, data) => dispatch({ kind: "event", taskId, type: "usage", data }),
      branch: (taskId, data) => dispatch({ kind: "event", taskId, type: "branch", data }),
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

  // Load the selected task's earlier output once, if nothing has streamed yet.
  useEffect(() => {
    if (!state.selected) return;
    loadOutput(state.selected)
      .then((body) => dispatch({ type: "history", taskId: state.selected, lines: (body && body.lines) || [] }))
      .catch(() => {});
  }, [state.selected]);
  const waiting = needsYou(state.tasks);

  return (
    <div>
      <header className="mc-header" style={styles.header}>
        <span style={styles.logo}>MAESTRO</span>
        <span style={{ color: "var(--dim)" }}>console</span>
        <span style={{ flex: 1 }} />
        {Object.entries(counts).map(([s, n]) => (
          <span key={s} style={{ ...styles.count, borderColor: stateColor(s) }}>
            {n} {s}
          </span>
        ))}
        <button
          onClick={() => setShowSystem(!showSystem)}
          title="The daemon, its agents, budget spend and each workspace's config"
          style={{ font: "inherit", fontSize: 12, padding: "2px 8px", borderRadius: 10, background: "transparent", cursor: "pointer", border: `1px solid ${showSystem ? "var(--accent)" : "var(--border)"}`, color: showSystem ? "var(--text)" : "var(--dim)" }}
        >
          system
        </button>
        <button
          onClick={toggleNotify}
          title="Show a browser notification when a task needs you, completes or fails"
          style={{ font: "inherit", fontSize: 12, padding: "2px 8px", borderRadius: 10, background: "transparent", cursor: "pointer", border: `1px solid ${notify ? "var(--accent)" : "var(--border)"}`, color: notify ? "var(--text)" : "var(--dim)" }}
        >
          notifications {notify ? "on" : "off"}
        </button>
        <span
          title={state.live ? "event stream connected" : "event stream disconnected"}
          style={{ ...styles.dot, background: state.live ? "var(--ok)" : "var(--err)" }}
        />
      </header>
      <NeedsYou
        tasks={waiting}
        agents={agents.agents}
        discovered={agents.discovered}
        onSelect={(taskId) => dispatch({ type: "select", taskId })}
        onDone={refresh}
      />
      <div className="mc-body" style={styles.body}>
        <aside className="mc-list" style={styles.list}>
          <TaskList
            tasks={state.tasks}
            workspaces={workspaces}
            filters={filters}
            setFilters={setFilters}
            selected={state.selected}
            onSelect={(taskId) => {
              setShowSystem(false);
              dispatch({ type: "select", taskId });
            }}
            loaded={state.loaded}
          />
        </aside>
        <main className="mc-detail" style={styles.detail}>
          {showSystem ? (
            <SystemView agents={agents.agents} discovered={agents.discovered} workspaces={workspaces} />
          ) : selectedTask ? (
            <DetailPane task={selectedTask} onChanged={refresh} />
          ) : (
            <div style={{ ...styles.dim, padding: 24 }}>select a task</div>
          )}
        </main>
      </div>
    </div>
  );
}

// Per-browser settings (filters, notifications). Storage can be unavailable
// (a private window, blocked site data), so every access is guarded.
function readSetting(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function writeSetting(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // not stored; the setting still applies to this page
  }
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
