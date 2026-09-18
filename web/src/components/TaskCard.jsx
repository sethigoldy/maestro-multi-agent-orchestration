import React from "react";
import { stateColor } from "../App.jsx";

export default function TaskCard({ task, selected, onSelect }) {
  const state = task.state || "unknown";
  return (
    <button
      onClick={onSelect}
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        padding: "10px 12px",
        background: selected ? "#1c2431" : "transparent",
        border: "none",
        borderBottom: "1px solid var(--border)",
        color: "var(--text)",
        cursor: "pointer",
        font: "inherit",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: stateColor(state),
            flexShrink: 0,
          }}
        />
        <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {task.title || task.task_id}
        </span>
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 4, fontSize: 12, color: "var(--dim)" }}>
        <span>{state}</span>
        {task.target_agent && <span>→ {task.target_agent}</span>}
        {typeof task.usage?.cost_usd === "number" && (
          <span>${task.usage.cost_usd.toFixed(3)}</span>
        )}
      </div>
    </button>
  );
}
