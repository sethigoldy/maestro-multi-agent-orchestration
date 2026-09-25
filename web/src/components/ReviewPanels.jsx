import React, { useEffect, useState } from "react";
import { loadDiff, loadVerification } from "../lib/events.js";
import { parseDiff } from "../lib/diff.js";

const COLORS = { add: "var(--ok)", del: "var(--err)", hunk: "var(--accent)", file: "var(--text)", meta: "var(--dim)", context: "var(--text)" };

// Loads data for the selected task, again whenever its state changes.
function useTaskData(load, taskId, state) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    load(taskId)
      .then((value) => !cancelled && setData(value))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [taskId, state]);
  return [data, error];
}

// The task's changes: its run directory compared with the commit it started from.
export function ChangesPanel({ taskId, state }) {
  const [diff, error] = useTaskData(loadDiff, taskId, state);
  if (error) return <div style={styles.dim}>could not load the changes: {error}</div>;
  if (!diff) return <div style={styles.dim}>loading changes…</div>;
  if (!diff.available) return <div style={styles.dim}>{diff.reason}</div>;
  const empty = !diff.files.length && !diff.untracked.length;
  return (
    <div>
      <div style={{ padding: "8px 16px", fontSize: 13, borderBottom: "1px solid var(--border)" }}>
        <div style={{ color: "var(--dim)" }}>in {diff.run_dir}, compared with {String(diff.base).slice(0, 12)}</div>
        {empty && <div style={{ color: "var(--dim)", marginTop: 4 }}>no changes</div>}
        {diff.files.map((file) => (
          <div key={file.path}>
            <span style={{ color: "var(--ok)" }}>+{file.added ?? "bin"}</span>{" "}
            <span style={{ color: "var(--err)" }}>-{file.removed ?? "bin"}</span> {file.path}
          </div>
        ))}
        {diff.untracked.map((path) => (
          <div key={path}><span style={{ color: "var(--warn)" }}>new, not added</span> {path}</div>
        ))}
        {diff.truncated && <div style={{ color: "var(--warn)", marginTop: 4 }}>the diff is long; only its first part is shown</div>}
      </div>
      <pre style={styles.pre}>
        {parseDiff(diff.diff).map((line, index) => (
          <div key={index} style={{ color: COLORS[line.kind], fontWeight: line.kind === "file" ? 700 : 400 }}>{line.text || " "}</div>
        ))}
      </pre>
    </div>
  );
}

// The text of the task's last verification report.
export function VerificationPanel({ taskId, state }) {
  const [data, error] = useTaskData(loadVerification, taskId, state);
  if (error) return <div style={styles.dim}>could not load the verification report: {error}</div>;
  if (!data) return <div style={styles.dim}>loading the verification report…</div>;
  if (!data.report) return <div style={styles.dim}>no verification has run for this task yet</div>;
  return <pre style={styles.pre}>{data.report}</pre>;
}

const styles = {
  dim: { color: "var(--dim)", padding: 16 },
  pre: { margin: 0, padding: 16, whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 13 },
};
