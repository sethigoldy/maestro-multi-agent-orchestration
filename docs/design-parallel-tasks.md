# Design: Parallel tasks in one workspace, using a git worktree per task

Status: implemented in 0.15.0. Date: 2026-09-24.

## 1. The problem

Today Maestro runs one task at a time in each workspace. The daemon keeps one
slot per workspace path. The task in that slot runs `git checkout -b <branch>`
directly in the workspace, and every other task for the same workspace waits in
a queue until the slot is free. The v1 architecture chose this on purpose
(`docs/architecture-proposal.md`, "No parallel agents in the same worktree in
v1"), because two agents working in one directory on two branches would
overwrite each other's files.

This has two costs:

- Independent tasks for the same repository run one after another, even when
  they touch different parts of the code.
- A task that stops to wait for an answer keeps the slot. If nobody answers, every
  later task for that workspace waits behind it indefinitely. This happened on
  2026-09-24, when two tasks parked on the routing question and the tasks
  delegated after them never started.

## 2. The decision

When a task would have to wait because another task is using the workspace,
Maestro gives it its own git worktree instead, and starts it immediately.

- The first task for a workspace still runs in the workspace itself, as today.
- A task that arrives while the workspace is in use runs in a new git worktree,
  on its own branch, and starts at once.
- Each task always stays in the directory where it started. Follow-ups and
  answers continue there.
- Maestro still never commits. A task's result is the uncommitted changes in the
  directory where it ran, as today. The only difference is that for some tasks
  that directory is a worktree.

### Alternatives that were considered

| Option | Why it was not chosen |
|---|---|
| Every task gets a worktree, including the first | Changes where every task's work lands, including for users who run one task at a time and review it in their own checkout. The user chose to keep today's behaviour for the first task. |
| Worktrees only when switched on in config | Keeps the blocking queue for everyone who does not find the setting. |
| All tasks share the workspace and its current branch | Agents would edit the same files at the same time, and each task's tests would run over the other tasks' half-finished changes. |
| Tasks share the workspace but declare which files they own | Needs every handoff to list its files correctly, and a wrong list silently mixes changes. |
| Keep the queue, but a parked task gives up the slot | Fixes the blocked queue, but tasks still run one at a time. |

## 3. Where a task runs

Maestro stores a new value for every task: its **run directory**
(`run_dir`). This is the directory where the agent works, where verification
runs, and where the task's changes are. `workspace` keeps its current meaning:
the path the caller passed.

Maestro decides the run directory when a turn starts. A turn starts at
delegation, when a queued task starts, when a question is answered, and on a
follow-up. The rules, in order:

1. **The task already has a run directory.** The turn runs there.
   - If the run directory is the workspace and another task is using the
     workspace right now, the turn waits in the queue, as today. The task's
     uncommitted work is in the workspace, so it cannot move.
   - If the run directory is a worktree that no longer exists (it was removed
     with `task cleanup`, or deleted by hand), Maestro runs `git worktree prune`,
     creates the worktree again from the task's branch, and writes a note in the
     task's output saying so. Only committed work comes back; uncommitted work
     was in the removed directory.
2. **The workspace is free.** The task runs in the workspace. Maestro runs
   `git checkout -b <branch>` there, exactly as today.
3. **The workspace is in use, and the workspace is below its limit.** Maestro
   runs `git worktree add -b <branch> ~/.maestro/worktrees/<task-id> <commit>`,
   where `<commit>` is the commit currently checked out in the workspace, and
   the task runs there.
4. **The workspace is at its limit.** The task waits in the queue, as today.

"In use" means a task whose run directory is the workspace is running a turn or
is waiting for an answer. This is the same rule as today's slot. A finished task
does not hold the workspace.

### Details of the worktree

- **Starting point.** The worktree starts from the workspace's current commit.
  Uncommitted changes in the workspace, including the work of the task that is
  running there, are not copied. If the workspace has no commit yet, the task
  fails with a message that says so.
- **Location.** `~/.maestro/worktrees/<task-id>` (under `$MAESTRO_HOME`). It is
  outside the repository, so `git status` and test discovery in the user's
  checkout are not affected. The path uses the task id, not the branch name, so
  renaming the branch never makes the path wrong.
- **Branch.** The same name the task would get today: the handoff's `branch`, or
  `maestro/<task-id>`. The same checks apply at delegation: the branch must not
  exist yet, and must not clash with an existing branch as a folder.

### The limit

At most 4 tasks run turns at the same time for one workspace: the workspace
itself plus 3 worktrees. Tasks beyond that wait in the queue. The limit is set
with `max_parallel` in the `[defaults]` table of the config file, and `1` gives
today's behaviour exactly.

- The limit counts tasks that are running a turn. A task waiting for an answer
  and a finished task do not count, because they are not running an agent.
- The limit applies to the workspace path the caller passed. Two different
  worktrees that a caller created itself (for example two Claude Code worktrees
  of the same repository) are two workspaces, as today, and each has its own
  limit.
- The limit exists because every parallel task is another paid agent run.
  The budget caps (`MAESTRO_BUDGET_*_USD`) still apply on top of it.

### Tasks that never get a worktree

- **`commit_policy = "no-commit"`.** These tasks work in place without a branch.
  They run in the workspace, and wait in the queue while the workspace is in
  use.
- **Tasks sent to a remote daemon (`a2a_remote` agents).** They never used the
  local checkout. Nothing changes for them.

## 4. What the agent, verification and callers see

- **The agent's prompt** names the run directory in the line "Workspace: …
  (work only inside this directory)". Gate passes (review and verify) and fix
  passes use the same directory.
- **Adapters** start the agent with the run directory as its working
  directory. No adapter needs its own change; each already uses the path it is
  given.
- **Verification** runs in the run directory: the test command,
  `git diff --check`, the check for changes, the turn baseline and the diff
  excerpt given to reviewers.
- **The Python interpreter for verification.** A new worktree has no `.venv/`.
  Maestro uses `MAESTRO_PYTHON` when it is set, as today. Otherwise it looks
  for `.venv/` or `venv/` in the run directory, then in the main checkout of
  the repository (found with `git rev-parse --git-common-dir`), and finally
  uses its own interpreter. Without the main-checkout step, every Python task
  in a worktree would fail with "pytest missing".
- **Task knowledge** (the list of changed files used by follow-ups) is read from
  the run directory.
- **Context files, skills and config** still resolve against the workspace.
  Files that are not in git, such as a local skill directory, exist only in the
  user's checkout.
- **A new `run_dir` field** appears in `task status`, `task list`, `task audit`,
  the receipt, MCP results, A2A task metadata, the terminal dashboard and the web
  console. For a task that ran in the workspace, it equals `workspace`. The
  terminal dashboard shows a `run dir:` line only when it differs from the
  workspace.
- **The result of delegating** says where the task runs. It says "queued" only
  when the limit is reached, or when a `no-commit` task or a task whose run
  directory is the workspace has to wait for the workspace. The CLI and MCP
  messages name which of these applies.
- **`rename-branch`** keeps working. Git allows renaming a branch that is checked
  out in a worktree.

### Stored data

- New claim `task_run_dir`: the absolute path of the run directory.
- New claim `task_run_dir_kind`: `workspace` or `worktree`.
- New claim `task_run_dir_removed`: set to `true` by `task cleanup`, and cleared
  when the worktree is created again.
- Tasks created before this change have none of these claims. They are treated
  as having run in their workspace, which is where they did run.

## 5. Removing worktrees

Maestro never deletes uncommitted work without being told to.

- **`maestro task cleanup <task> [--force]`**, and the MCP tool
  `cleanup_task_worktree`, remove the task's worktree with
  `git worktree remove`.
  - Refused while the task is running a turn or a turn is starting.
  - Refused when the worktree has uncommitted changes. The message lists the
    changed files. `--force` removes the worktree anyway.
  - The branch and its commits are always kept.
  - For a task whose run directory is the workspace, the command does nothing
    and says so. Maestro never removes or changes the user's checkout.
- **`maestro gc`** already deletes the records of old finished tasks. It now
  also removes their worktrees, but only worktrees with no uncommitted changes.
  A task whose worktree has uncommitted changes keeps both its worktree and its
  record, and `gc` lists it in its output.
- **Nothing is removed automatically** when a task finishes.

## 6. Failures

- **`git worktree add` fails** (for example, the disk is full, or the path
  exists but is not a worktree). The task fails with git's message. Maestro does
  not fall back to running in the workspace, because that could mix two tasks'
  changes in one directory.
- **The daemon stops or crashes.** Worktrees stay on disk. The existing startup
  check marks interrupted tasks as failed, and `task continue` resumes each one
  in its own worktree.
- **The user deletes a worktree by hand.** Status shows the run directory as
  missing. The next turn creates it again (rule 1 above).

## 7. Testing

- Unit tests for choosing the run directory: workspace free, workspace in use,
  at the limit, `max_parallel = 1`, `no-commit`, a follow-up in the workspace
  while another task uses it, a follow-up in a worktree, a removed worktree, and
  a workspace with no commit.
- Unit tests for the interpreter fallback to the main repository's `.venv/`.
- Unit tests for `task cleanup`: refused while running, refused with
  uncommitted changes, `--force`, a task that ran in the workspace, and a task
  from before this change.
- Unit tests for `gc` keeping worktrees with uncommitted changes.
- An integration test with a real git repository and a fake agent: three tasks
  delegated to one workspace at the same time all start at once, each on its own
  branch; the workspace holds only the first task's changes, and each worktree
  holds only its own.
- Coverage stays at 100%.

## 8. Not in this change

- Maestro still does not commit, merge or push.
- No automatic removal of worktrees when a branch is merged.
- No sharing of one worktree between two tasks.
