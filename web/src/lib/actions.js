// Which actions the console offers for a task, and the small pieces of text it
// builds for them. Pure functions, so they are tested without a browser.

const RUNNING = ["submitted", "working"];
const FINISHED = ["completed", "failed", "canceled"];

export function availableActions(task) {
  const state = task.state || "unknown";
  const parked = state === "input-required";
  const finished = FINISHED.includes(state);
  return {
    answer: parked,
    cancel: RUNNING.includes(state) || parked,
    followup: finished,
    cleanup: finished && task.run_dir_kind === "worktree" && !task.run_dir_missing,
    rename: parked || finished,
  };
}

// The answer to a routing question: "agent=<name>", plus " model=<model>" when one is given.
export function routingAnswer(agent, model) {
  const chosenModel = String(model || "").trim();
  return chosenModel ? `agent=${agent} model=${chosenModel}` : `agent=${agent}`;
}

// The agents the daemon can run, as picker options: registered ones
// (installed first), then CLIs installed on PATH that are not registered.
export function agentOptions(agents, discovered = []) {
  const options = (agents || []).map((agent) => {
    const status = agent.status || {};
    const installed = status.found !== false;
    const parts = [agent.name];
    if (!installed) parts.push("not installed");
    else if (status.version) parts.push(status.version);
    if (installed && agent.model) parts.push(`default model ${agent.model}`);
    return { name: agent.name, installed, model: agent.model || null, label: parts.join(" — ") };
  });
  options.sort((a, b) => Number(b.installed) - Number(a.installed));
  const names = new Set(options.map((o) => o.name));
  for (const agent of discovered || []) {
    if (names.has(agent.name)) continue;
    options.push({ name: agent.name, installed: true, model: null, label: `${agent.name} — installed, not registered` });
  }
  return options;
}

// The "→ agent" part of a task card. A task waiting for its agent to be
// chosen carries a placeholder target, which must not be shown as a choice.
export function routeLabel(task) {
  if (task.awaiting === "routing") return "agent not chosen yet";
  return task.target_agent ? `→ ${task.target_agent}` : "";
}

// Tasks waiting for input, the one that has waited longest first.
export function needsYou(tasks) {
  return Object.values(tasks || {})
    .filter((task) => task.state === "input-required")
    .sort((a, b) => String(a.started_at || "").localeCompare(String(b.started_at || "")));
}
