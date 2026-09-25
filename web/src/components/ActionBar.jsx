import React, { useState } from "react";
import { rpc } from "../lib/rpc.js";
import { availableActions } from "../lib/actions.js";
import { Button, ErrorText, inputStyle } from "./ui.jsx";

// The actions allowed in the task's current state: cancel, follow-up,
// remove the worktree, rename the branch. Each calls the daemon's JSON-RPC
// method; the daemon's own message is shown when it refuses.
export default function ActionBar({ task, onDone }) {
  const allowed = availableActions(task);
  const [open, setOpen] = useState(null); // "followup" | "rename" | null
  const [instruction, setInstruction] = useState("");
  const [fresh, setFresh] = useState(false);
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [dirty, setDirty] = useState(false);

  async function call(method, params, after) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await rpc(method, params);
      if (after) after(result);
      onDone(task.task_id);
      return true;
    } catch (e) {
      setError(e.message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  function cancel() {
    if (window.confirm("Cancel this task? A running agent is stopped.")) {
      call("tasks/cancel", { id: task.task_id, reason: "canceled from the console" });
    }
  }

  async function cleanup(force) {
    if (force && !window.confirm("Remove the worktree and its uncommitted changes? The branch is kept.")) return;
    const ok = await call("tasks/cleanup", { id: task.task_id, force }, (result) => setNotice(result.cleanup.reason));
    setDirty(!ok && !force);
  }

  async function followup() {
    const params = { id: task.task_id, instruction, context_mode: fresh ? "fresh" : "reuse" };
    if (branch.trim()) params.branch = branch.trim();
    if (await call("tasks/followup", params)) {
      setOpen(null);
      setInstruction("");
      setBranch("");
    }
  }

  async function rename() {
    if (await call("tasks/renameBranch", { id: task.task_id, branch: branch.trim() })) {
      setOpen(null);
      setBranch("");
    }
  }

  if (!allowed.cancel && !allowed.followup && !allowed.cleanup && !allowed.rename) return null;
  return (
    <div style={{ padding: "10px 16px", borderBottom: "1px solid var(--border)" }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {allowed.followup && <Button disabled={busy} onClick={() => setOpen(open === "followup" ? null : "followup")}>Follow-up…</Button>}
        {allowed.rename && <Button disabled={busy} onClick={() => setOpen(open === "rename" ? null : "rename")}>Rename branch…</Button>}
        {allowed.cleanup && <Button disabled={busy} onClick={() => cleanup(false)}>Remove worktree</Button>}
        {allowed.cleanup && dirty && <Button kind="danger" disabled={busy} onClick={() => cleanup(true)}>Remove anyway</Button>}
        {allowed.cancel && <Button kind="danger" disabled={busy} onClick={cancel}>Cancel task</Button>}
      </div>
      {open === "followup" && (
        <div style={{ marginTop: 8 }}>
          <textarea
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            rows={3}
            placeholder="What should the agent do next?"
            style={{ ...inputStyle, width: "100%", resize: "vertical" }}
            aria-label="follow-up instruction"
          />
          <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 6, flexWrap: "wrap" }}>
            <label style={{ fontSize: 13 }}>
              <input type="checkbox" checked={fresh} onChange={(e) => setFresh(e.target.checked)} /> start with a fresh context
            </label>
            <input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="new branch name (optional)" style={inputStyle} aria-label="new branch name" />
            <Button kind="primary" disabled={busy || !instruction.trim()} onClick={followup}>Send follow-up</Button>
          </div>
        </div>
      )}
      {open === "rename" && (
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder={task.branch || "new branch name"} style={inputStyle} aria-label="branch name" />
          <Button kind="primary" disabled={busy || !branch.trim()} onClick={rename}>Rename</Button>
        </div>
      )}
      {notice && <div style={{ color: "var(--dim)", fontSize: 13, marginTop: 6 }}>{notice}</div>}
      <ErrorText error={error} />
    </div>
  );
}
