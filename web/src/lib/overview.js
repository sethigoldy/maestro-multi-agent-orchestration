// The task list's search, filters, grouping and notifications. Pure
// functions, so they are tested without a browser.

const FINISHED = ["completed", "failed", "canceled"];

function view(task) {
  if (task.queued) return "queued";
  if (task.state === "input-required") return "needs-you";
  if (FINISHED.includes(task.state)) return "finished";
  return "running";
}

// Tasks matching the filters, newest first.
// filters: { view: "needs-you" | "running" | "queued" | "finished", agent, query }
export function filterTasks(tasks, filters = {}) {
  const query = String(filters.query || "").trim().toLowerCase();
  return Object.values(tasks || {})
    .filter((task) => !filters.view || view(task) === filters.view)
    .filter((task) => !filters.agent || task.target_agent === filters.agent)
    .filter((task) => !query || [task.title, task.task_id, task.workspace].some((v) => String(v || "").toLowerCase().includes(query)))
    .sort((a, b) => String(b.started_at || "").localeCompare(String(a.started_at || "")));
}

// Tasks grouped by workspace, the group with the newest task first. Each
// label says how much of the workspace's max_parallel limit is in use.
export function groupByWorkspace(tasks, workspaces = []) {
  const info = Object.fromEntries((workspaces || []).map((w) => [w.workspace, w]));
  const groups = [];
  const byName = {};
  for (const task of tasks) {
    const name = task.workspace || "(unknown workspace)";
    if (!byName[name]) {
      byName[name] = { workspace: name, tasks: [] };
      groups.push(byName[name]);
    }
    byName[name].tasks.push(task);
  }
  for (const group of groups) {
    const w = info[group.workspace];
    group.label = w
      ? `${group.workspace} · ${w.running} of ${w.max_parallel} running${w.queued ? ` · ${w.queued} queued` : ""}`
      : group.workspace;
  }
  return groups;
}

// The browser notification for a state change, or null when it should not notify.
export function notificationFor(previousState, task) {
  if (previousState === task.state) return null;
  const name = task.title || task.task_id;
  if (task.state === "input-required") return { title: "Maestro: a task needs you", body: `${name} is waiting for an answer.` };
  if (task.state === "completed") return { title: "Maestro: task completed", body: `${name} completed.` };
  if (task.state === "failed") return { title: "Maestro: task failed", body: `${name} failed.` };
  return null;
}
