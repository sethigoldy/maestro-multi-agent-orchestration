import React from "react";
import AnswerForm from "./AnswerForm.jsx";

// Every task waiting for input, with its question and a way to answer it.
export default function NeedsYou({ tasks, agents, discovered, onSelect, onDone }) {
  if (!tasks.length) return null;
  return (
    <section style={{ borderBottom: "1px solid var(--border)", background: "#1d1a10", padding: "10px 16px" }}>
      <div style={{ color: "var(--warn)", fontSize: 12, marginBottom: 8 }}>
        NEEDS YOU · {tasks.length} task{tasks.length === 1 ? "" : "s"} waiting for an answer
      </div>
      {tasks.map((task) => (
        <div key={task.task_id} style={{ marginBottom: 12 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
            <button
              onClick={() => onSelect(task.task_id)}
              style={{ font: "inherit", fontWeight: 600, background: "none", border: "none", color: "var(--text)", padding: 0, cursor: "pointer" }}
            >
              {task.title || task.task_id}
            </button>
            <span style={{ color: "var(--dim)", fontSize: 12 }}>{task.workspace}</span>
          </div>
          <div style={{ whiteSpace: "pre-wrap", fontSize: 13, margin: "4px 0 6px", color: "var(--dim)" }}>
            {task.question || "This task is waiting for input."}
          </div>
          <AnswerForm task={task} agents={agents} discovered={discovered} onDone={onDone} />
        </div>
      ))}
    </section>
  );
}
