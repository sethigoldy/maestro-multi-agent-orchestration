import React, { useEffect, useRef, useState } from "react";
import { stateColor } from "../App.jsx";
import { loadReceipt } from "../lib/events.js";

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

      <ReceiptPanel taskId={task.task_id} />

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

// Durable execution receipt for the selected task (GET /tasks/<id>/receipt):
// the IMPLEMENT/VERIFY/REVIEW/FIX chain, final verification, and final state.
function ReceiptPanel({ taskId }) {
  const [receipt, setReceipt] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setReceipt(null);
    loadReceipt(taskId)
      .then((data) => {
        if (!cancelled) setReceipt(data);
      })
      .catch(() => {}); // no daemon / unknown task: the panel stays hidden
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  if (!receipt) return null;
  const attempts = receipt.attempts || [];
  const verification = receipt.verification || {};
  const totals = receipt.totals || {};
  const finalState = (receipt.state || "unknown").toUpperCase();
  const verified = receipt.state === "completed" && verification.ran && verification.result === "PASSED";

  return (
    <div style={{ padding: "10px 16px", borderBottom: "1px solid var(--border)" }}>
      <span style={{ color: "var(--dim)", fontSize: 12 }}>RECEIPT</span>
      {attempts.map((attempt, index) => (
        <div key={index} style={{ marginTop: 4, fontSize: 13 }}>
          <span style={{ color: "var(--accent)", fontWeight: 600 }}>{(attempt.phase || "IMPLEMENT").padEnd(8)}</span>
          <span>{attempt.agent || "?"}</span>
          {typeof attempt.duration_s === "number" && (
            <span style={{ color: "var(--dim)" }}> · {fmtDuration(attempt.duration_s)}</span>
          )}
          {typeof attempt.cost_usd === "number" && (
            <span style={{ color: "var(--dim)" }}> · ${attempt.cost_usd.toFixed(2)}</span>
          )}
          <span style={{ color: attempt.ok ? "var(--ok)" : "var(--err)" }}> {attempt.ok ? "✓" : "✗"}</span>
        </div>
      ))}
      <div style={{ marginTop: 6, fontSize: 13 }}>
        <span style={{ color: "var(--dim)" }}>Final verification: </span>
        {verification.ran ? (
          <span style={{ color: verification.result === "PASSED" ? "var(--ok)" : "var(--err)" }}>
            {verification.result === "PASSED" ? "✓ PASSED" : `✗ ${verification.result}`}
          </span>
        ) : (
          <span style={{ color: "var(--dim)" }}>skipped</span>
        )}
      </div>
      <div style={{ marginTop: 4, fontSize: 13 }}>
        <span style={{ color: "var(--dim)" }}>Final: </span>
        <span style={{ fontWeight: 700, color: stateColor(receipt.state || "unknown") }}>{finalState}</span>
        {verified && (
          <span style={{ color: "var(--ok)", marginLeft: 8 }}>VERIFIED</span>
        )}
        {typeof totals.duration_s === "number" && (
          <span style={{ color: "var(--dim)" }}> · {fmtDuration(totals.duration_s)}</span>
        )}
        {typeof totals.cost_usd === "number" && (
          <span style={{ color: "var(--dim)" }}> · ${totals.cost_usd.toFixed(2)}</span>
        )}
      </div>
    </div>
  );
}

function fmtDuration(seconds) {
  const total = Math.round(Number(seconds));
  if (!Number.isFinite(total) || total < 0) return "—";
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function firstLine(text) {
  return String(text).split("\n")[0];
}
