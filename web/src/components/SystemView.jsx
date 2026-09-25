import React, { useEffect, useState } from "react";
import { loadSystem } from "../lib/events.js";
import { agentOptions } from "../lib/actions.js";
import { budgetRows } from "../lib/overview.js";

// The daemon, its agents, budget spend and each workspace's effective config
// (docs/design-console.md, section 7).
export default function SystemView({ agents, discovered, workspaces }) {
  const [system, setSystem] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    loadSystem().then(setSystem).catch((e) => setError(e.message));
  }, []);

  return (
    <div style={{ padding: 16, overflowY: "auto" }}>
      <Section title="DAEMON">
        {error && <div style={{ color: "var(--err)" }}>could not reach the daemon: {error}</div>}
        {system && (
          <Table
            rows={[
              ["version", system.version],
              ["address", `${system.bind}:${system.port}`],
              ["process", `pid ${system.pid}, up ${uptime(system.uptime_s)}`],
              ["state directory", system.state_dir],
            ]}
          />
        )}
      </Section>
      <Section title="AGENTS">
        {agentOptions(agents, discovered).map((option) => {
          const spec = (agents || []).find((a) => a.name === option.name) || {};
          return (
            <div key={option.name} style={{ fontSize: 13, marginBottom: 4 }}>
              <span style={{ color: option.installed ? "var(--ok)" : "var(--err)" }}>●</span> {option.label}
              {spec.kind && spec.kind !== option.name && <span style={{ color: "var(--dim)" }}> (kind {spec.kind})</span>}
              {spec.effort && <span style={{ color: "var(--dim)" }}> · effort {spec.effort}</span>}
            </div>
          );
        })}
        {!agents.length && !discovered.length && <div style={{ color: "var(--dim)" }}>no agents registered or installed</div>}
      </Section>
      {system && (
        <Section title="BUDGETS">
          {budgetRows(system.budgets).map((row) => (
            <div key={row.label} style={{ fontSize: 13, marginBottom: 4, color: row.over ? "var(--err)" : "var(--text)" }}>
              {row.label}: {row.spent} spent · cap: {row.cap}{row.over ? " — over the cap, new tasks are refused" : ""}
            </div>
          ))}
        </Section>
      )}
      <Section title="WORKSPACES">
        {workspaces.length === 0 && <div style={{ color: "var(--dim)" }}>no workspaces yet</div>}
        {workspaces.map((w) => (
          <div key={w.workspace} style={{ marginBottom: 10, fontSize: 13 }}>
            <div style={{ wordBreak: "break-all" }}>{w.workspace}</div>
            <div style={{ color: "var(--dim)" }}>
              {w.running} of {w.max_parallel} running · {w.queued} queued · {w.parked} waiting for an answer · {w.finished} finished
            </div>
            <div style={{ color: "var(--dim)" }}>
              default agent {w.config.agent || "none (tasks ask which agent to use)"}
              {w.config.model ? ` · model ${w.config.model}` : ""}
              {w.config.effort ? ` · effort ${w.config.effort}` : ""}
              {" · "}verification limit {w.config.verification_timeout_s ? `${Math.round(w.config.verification_timeout_s / 60)} min` : "none"}
            </div>
          </div>
        ))}
      </Section>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <section style={{ marginBottom: 20 }}>
      <div style={{ color: "var(--dim)", fontSize: 12, marginBottom: 6 }}>{title}</div>
      {children}
    </section>
  );
}

function Table({ rows }) {
  return rows.map(([label, value]) => (
    <div key={label} style={{ fontSize: 13, marginBottom: 4 }}>
      <span style={{ color: "var(--dim)" }}>{label}: </span>
      <span style={{ wordBreak: "break-all" }}>{String(value)}</span>
    </div>
  ));
}

function uptime(seconds) {
  const s = Math.round(Number(seconds) || 0);
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${s}s`;
}
