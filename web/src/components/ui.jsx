import React from "react";

// Small shared controls, styled like the rest of the console.
export function Button({ children, onClick, disabled, kind = "normal", title }) {
  const color = kind === "danger" ? "var(--err)" : kind === "primary" ? "var(--accent)" : "var(--text)";
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        font: "inherit",
        fontSize: 13,
        padding: "4px 10px",
        borderRadius: 6,
        border: `1px solid ${disabled ? "var(--border)" : color}`,
        background: "transparent",
        color: disabled ? "var(--dim)" : color,
        cursor: disabled ? "default" : "pointer",
      }}
    >
      {children}
    </button>
  );
}

export const inputStyle = {
  font: "inherit",
  fontSize: 13,
  padding: "4px 8px",
  borderRadius: 6,
  border: "1px solid var(--border)",
  background: "var(--bg)",
  color: "var(--text)",
};

export function ErrorText({ error }) {
  if (!error) return null;
  return <div style={{ color: "var(--err)", fontSize: 13, marginTop: 6, whiteSpace: "pre-wrap" }}>{error}</div>;
}
