import React, { useEffect, useRef } from "react";
import { stateColor } from "../App.jsx";

function Meta({ label, value }) {
  if (value === null || value === undefined || value === "") return null;
  return (
    <div style={{ marginBottom: 6 }}>
      <span style={{ color: "var(--dim)" }}>{label}: </span>
      <span>{String(value)}</span>
    </div>
  );
}

export default function DetailPane({ task }) {
  const state = task.state || "unknown";
  const attempts = task.attempts || [];
  const transcript = task.transcript || [];
  const scrollRef = useRef(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight; // follow the live tail
  }, [transcript.length]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{ padding: 16, borderBottom: "1px solid var(--border)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
          <span style={{ fontWeight: 700, fontSize: 16 }}>{task.title || task.task_id}</span>
          <span
            style={{
              fontSize: 12,
              padding: "2px 8px",
              borderRadius: 10,
              border: `1px solid ${stateColor(state)}`,
              color: stateColor(state),
            }}
          >
            {state}
          </span>
        </div>
        <Meta label="task" value={task.task_id} />
        <Meta label="route" value={`${task.origin_agent || "?"} → ${task.target_agent || "?"}`} />
        <Meta label="workspace" value={task.workspace} />
        <Meta label="branch" value={task.branch} />
        {typeof task.usage?.cost_usd === "number" && (
          <Meta label="cost" value={`$${task.usage.cost_usd.toFixed(4)}`} />
        )}
        {task.question && <Meta label="question" value={task.question} />}
        {task.error && (
          <div style={{ marginTop: 6, color: "var(--err)", whiteSpace: "pre-wrap" }}>
            {task.error}
          </div>
        )}
      </div>

      {attempts.length > 0 && (
        <div style={{ padding: "10px 16px", borderBottom: "1px solid var(--border)" }}>
          <span style={{ color: "var(--dim)", fontSize: 12 }}>ATTEMPTS</span>
          {attempts.map((attempt, index) => (
            <div key={index} style={{ marginTop: 6, fontSize: 13 }}>
              <span
                style={{
                  color: attempt.error ? "var(--err)" : "var(--ok)",
                }}
              >
                {attempt.agent || "?"}
              </span>
              {attempt.error ? (
                <span style={{ color: "var(--dim)" }}> — failed: {firstLine(attempt.error)}</span>
              ) : (
                <span style={{ color: "var(--dim)" }}>
                  {" "}— ok{typeof attempt.usage?.cost_usd === "number" ? ` ($${attempt.usage.cost_usd.toFixed(3)})` : ""}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      <pre
        ref={scrollRef}
        style={{
          flex: 1,
          margin: 0,
          padding: 16,
          overflowY: "auto",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          fontSize: 13,
        }}
      >
        {transcript.length === 0 ? (
          <span style={{ color: "var(--dim)" }}>no output yet</span>
        ) : (
          transcript.map((line, index) => (
            <div key={index}>{line}</div>
          ))
        )}
      </pre>
    </div>
  );
}

function firstLine(text) {
  return String(text).split("\n")[0];
}
