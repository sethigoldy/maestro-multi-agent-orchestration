import React from "react";
import TaskCard from "./TaskCard.jsx";
import { filterTasks, groupByWorkspace } from "../lib/overview.js";
import { inputStyle } from "./ui.jsx";

const VIEWS = [
  ["", "All"],
  ["needs-you", "Needs you"],
  ["running", "Running"],
  ["queued", "Queued"],
  ["finished", "Finished"],
];

// The task list with search, a state view, an agent filter, and optional
// grouping by workspace (with each workspace's use of its max_parallel limit).
export default function TaskList({ tasks, workspaces, filters, setFilters, selected, onSelect, loaded }) {
  const shown = filterTasks(tasks, filters);
  const agents = [...new Set(Object.values(tasks).map((t) => t.target_agent).filter(Boolean))].sort();
  const set = (patch) => setFilters({ ...filters, ...patch });
  const groups = filters.grouped ? groupByWorkspace(shown, workspaces) : [{ workspace: null, tasks: shown }];

  return (
    <>
      <div style={{ padding: 8, borderBottom: "1px solid var(--border)", display: "flex", flexDirection: "column", gap: 6 }}>
        <input
          value={filters.query || ""}
          onChange={(e) => set({ query: e.target.value })}
          placeholder="Search title, task id or workspace"
          style={inputStyle}
          aria-label="search tasks"
        />
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {VIEWS.map(([value, label]) => (
            <button
              key={label}
              onClick={() => set({ view: value })}
              style={{
                font: "inherit",
                fontSize: 12,
                padding: "2px 8px",
                borderRadius: 10,
                cursor: "pointer",
                background: "transparent",
                border: `1px solid ${(filters.view || "") === value ? "var(--accent)" : "var(--border)"}`,
                color: (filters.view || "") === value ? "var(--text)" : "var(--dim)",
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12 }}>
          <select value={filters.agent || ""} onChange={(e) => set({ agent: e.target.value })} style={{ ...inputStyle, fontSize: 12 }} aria-label="agent filter">
            <option value="">every agent</option>
            {agents.map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
          <label style={{ color: "var(--dim)" }}>
            <input type="checkbox" checked={Boolean(filters.grouped)} onChange={(e) => set({ grouped: e.target.checked })} /> group by workspace
          </label>
        </div>
      </div>
      {!loaded && <div style={styles.dim}>loading tasks…</div>}
      {loaded && Object.keys(tasks).length === 0 && <div style={styles.dim}>no tasks yet — delegate one from any agent or the CLI</div>}
      {loaded && Object.keys(tasks).length > 0 && shown.length === 0 && <div style={styles.dim}>no tasks match these filters</div>}
      {groups.map((group) => (
        <div key={group.workspace || "all"}>
          {group.workspace && (
            <div style={{ padding: "6px 12px", fontSize: 12, color: "var(--dim)", background: "var(--panel)", borderBottom: "1px solid var(--border)", wordBreak: "break-all" }}>
              {group.label}
            </div>
          )}
          {group.tasks.map((task) => (
            <TaskCard key={task.task_id} task={task} selected={task.task_id === selected} onSelect={() => onSelect(task.task_id)} />
          ))}
        </div>
      ))}
    </>
  );
}

const styles = { dim: { color: "var(--dim)", padding: 12 } };
