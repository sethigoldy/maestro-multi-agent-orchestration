import React, { useState } from "react";
import { rpc } from "../lib/rpc.js";
import { agentOptions, routingAnswer } from "../lib/actions.js";
import { Button, ErrorText, inputStyle } from "./ui.jsx";

// Answers a parked task. The control depends on what the task waits for:
// an agent choice (routing), an approval, or a free-text answer.
export default function AnswerForm({ task, agents, discovered, onDone }) {
  const options = agentOptions(agents, discovered);
  const [agent, setAgent] = useState(options.length ? options[0].name : "");
  const [model, setModel] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function send(answer) {
    setBusy(true);
    setError(null);
    try {
      await rpc("tasks/answer", { id: task.task_id, answer });
      onDone(task.task_id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!window.confirm("Cancel this task?")) return;
    setBusy(true);
    setError(null);
    try {
      await rpc("tasks/cancel", { id: task.task_id, reason: "canceled from the console" });
      onDone(task.task_id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (task.awaiting === "routing") {
    const chosen = options.find((o) => o.name === agent);
    return (
      <div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <select value={agent} onChange={(e) => setAgent(e.target.value)} style={inputStyle} aria-label="agent">
            {options.map((o) => (
              <option key={o.name} value={o.name}>{o.label}</option>
            ))}
          </select>
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={chosen && chosen.model ? `model (default ${chosen.model})` : "model (optional)"}
            style={{ ...inputStyle, width: 200 }}
            aria-label="model"
          />
          <Button kind="primary" disabled={busy || !agent} onClick={() => send(routingAnswer(agent, model))}>
            Run with this agent
          </Button>
        </div>
        {options.length === 0 && <div style={{ color: "var(--dim)", fontSize: 13, marginTop: 6 }}>no agents are registered — run maestro agents register-discovered</div>}
        <ErrorText error={error} />
      </div>
    );
  }
  if (task.awaiting === "approval") {
    return (
      <div>
        <div style={{ display: "flex", gap: 8 }}>
          <Button kind="primary" disabled={busy} onClick={() => send("approved")}>Approve</Button>
          <Button kind="danger" disabled={busy} onClick={cancel}>Cancel task</Button>
        </div>
        <ErrorText error={error} />
      </div>
    );
  }
  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={2}
          placeholder="Your answer"
          style={{ ...inputStyle, flex: 1, resize: "vertical" }}
          aria-label="answer"
        />
        <Button kind="primary" disabled={busy || !text.trim()} onClick={() => send(text)}>Send answer</Button>
      </div>
      <ErrorText error={error} />
    </div>
  );
}
