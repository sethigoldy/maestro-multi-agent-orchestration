# Changelog

All notable changes to Maestro are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semantic versioning.

## [Unreleased]

## [0.16.2] — 2026-09-25

### Fixed

- **A task can have as many follow-ups as it needs.** Every follow-up spent one unit of the task's delegation depth (`max_depth_remaining`, 3 by default), so the third follow-up of any task was refused with "Max delegation depth exceeded". On 2026-09-25 a Codex session reviewing its tasks hit this on its third review round and fell back to editing the task branches itself, outside Maestro. The depth budget limits tasks whose agents delegate further tasks; a follow-up is another turn of the same task. Follow-ups now neither spend nor check it, and tasks whose budget earlier follow-ups used up can be followed up again.
- **The agent skill says a refused request is not "Maestro is unavailable".** Its fallback, doing the work directly, is only for a Maestro that cannot run. For a request Maestro refuses, the agent should fix what the message asks for, or report the refusal and ask the user.

## [0.16.1] — 2026-09-25

### Fixed

- **Stopping the daemon stops the agents it started.** `maestro daemon stop`, `maestro daemon restart` and stopping `maestro-daemon` marked each running task as failed, but left its agent running with no daemon to report to. On 2026-09-25 an agent kept running after its daemon had stopped. The daemon now cancels its running turns first, so no retry starts a new agent, and then stops each of those agents' process groups, including anything the agent started. It sends SIGTERM, gives the processes the same grace period a timeout or a cancel gives them, and then sends SIGKILL. Only the processes this daemon started for its own tasks are stopped; other processes, including agents started by another daemon, are left alone.

## [0.16.0] — 2026-09-25

### Added

- **A System view in the web console.** A "system" button in the header shows the daemon's version, address, process, uptime and state directory. It lists the registered agents and the agent CLIs installed on PATH, with versions and default models. It shows budget spend against `MAESTRO_BUDGET_*_USD` today and per agent, and for each workspace its task counts, its `max_parallel` limit, its default agent, model and effort, and its verification time limit. A workspace with no default agent says that its tasks will ask which agent to use. The daemon serves its part as `GET /system`.

- **Find what needs you in the web console.** The task list has a search box (title, task id or workspace), views (All, Needs you, Running, Queued, Finished), an agent filter, and grouping by workspace. Each workspace heading shows how much of its `max_parallel` limit is in use and how many tasks are queued. A queued task's card says why it waits. Tasks are listed newest first. A "notifications" switch in the header shows a browser notification when a task starts waiting for an answer, completes or fails; it is off until you turn it on. The console remembers the filters and the switch in this browser. The daemon serves the workspace figures as `GET /workspaces`, with each workspace's running, queued, parked and finished counts, its `max_parallel`, and the effective `[defaults]` and verification time limit its next task would get.

- **Review a task's work in the web console.** The detail pane has tabs: Output, Changes, Verification and Receipt. Changes shows the task's run directory compared with the commit the task started from, so commits the agent made count too: the changed files with added and removed line counts, new files that are not added to git, and the coloured diff. Verification shows the last verification report. The daemon serves these as `GET /tasks/<id>/diff` (at most 400 KB of diff text, with `truncated: true` when there is more; `available: false` with a reason when the directory is gone or git cannot compare it) and `GET /tasks/<id>/verification`. It records the commit a task started from on its first turn (`task_start_commit`).
- **Earlier output in the web console.** The console collected output only while it was open, so a finished task showed "no output yet" after a reload. `GET /tasks/<id>/output` returns the latest 2000 lines from the task's agent logs, and the console loads them when you select a task.
- **The web console works on narrow screens.** Below 760 pixels wide, the task list goes above the detail pane instead of beside it.

- **Act on tasks from the web console.** The console at `http://127.0.0.1:9785/` could only show tasks, so a parked task had to be answered from the CLI or an MCP client. A "Needs you" strip now lists every task waiting for input, with its question and a way to answer it. For "which agent?" questions it offers a picker of the registered agents and of agent CLIs installed on PATH, with an optional model. Sensitive-workspace approvals get Approve and Cancel buttons. Agent questions and failed checks get a text box. The detail pane shows the actions the task's state allows: Cancel, Follow-up, Remove worktree and Rename branch. The daemon's message is shown when it refuses. The console calls the same JSON-RPC methods as the CLI and MCP, so the daemon's existing rules against requests from other web pages cover every action. The design is in `docs/design-console.md`.
- **Task metadata for acting on tasks.** A2A task metadata (and so `tasks/get`, `GET /tasks` and the MCP results) now includes `question` and `awaiting` for a parked task, `run_dir_kind`, `queued`, `queue_reason` for a queued task, and `started_at`. The `agents/list` JSON-RPC result also has a `discovered` list: agent CLIs on PATH that are not registered, which the daemon can still run.

### Fixed

- **A task that a daemon restart failed is no longer reported as queued.** The daemon fails tasks that were still queued when it restarts, but their saved record kept `queued: true`, so the console listed them under Queued and counted them in each workspace's queue. A task is now reported as queued only while it is still waiting to start.
- **A new task branch is announced.** The daemon published a `branch` event only when a task's branch was renamed, so the console and the terminal dashboard learned a new task's branch only on their next full reload. It now also publishes one when the branch is first created.
- **The console's receipt updates when the task's state changes.** It loaded once, when a task was selected, and kept showing "Final: WORKING" after the task finished or was canceled.

## [0.15.1] — 2026-09-25

### Fixed

- **The agent skill describes Maestro 0.15.** `maestro skill install` copies the `maestro-driven-development` skill into every agent, for example into `~/.codex/AGENTS.md`, and the skill tells agents it is the complete contract. It still said "there is no CLI answer command", so a Codex session could not answer a parked task and asked the user to find an MCP client. It also lacked `task answer`, `task cancel`, `task cleanup`, `delegate --agent-may-commit`, `daemon start --port`, `[defaults] max_parallel`, `[verification] timeout_s`, `[daemon] port` and `run_dir`. The skill now documents all of them, and says that `[defaults]` in the user or project config applies to the next task without a restart. A new test fails whenever a CLI command, one of the options agents use, an MCP tool or a config key is missing from the skill. Run `maestro skill install` (or the installer) to update the copies agents read.
- **Maestro starts current Codex CLIs with `--sandbox workspace-write`.** Codex 0.146 removed `--approve-for-me` and deprecated `--full-auto`. Maestro chose `--approve-for-me` whenever `codex exec --help` did not list `--full-auto`, so every Codex run first failed with "unexpected argument '--approve-for-me'" and was then retried with the deprecated `--full-auto`, which printed a warning. Maestro now uses `--approve-for-me` only when the help lists it, then `--sandbox workspace-write` when the help lists `--sandbox`, then `--full-auto`. With no usable help it assumes `--sandbox workspace-write`. If the CLI rejects the chosen flag, Maestro tries each remaining one once.
- **The daemon now reads a project's config, and config edits apply without a restart.** The daemon read its config once, when it started, and only from the user file (`~/.maestro/config.toml`). A `[defaults]` table in a project's `.maestro/config.toml` was ignored, although the documentation and the agent skill said it applied. An edit to the user file only took effect after `maestro daemon restart`, while `maestro config` already showed the new value. So a user or an agent that set `[defaults] agent` kept seeing new tasks stop to ask which agent to use. The daemon now reads the user, project and worktree files for each task's workspace whenever it needs them. That covers `[defaults]` (including `max_parallel`), `[modes]`, `[context]`, `[verification] timeout_s` and `[continuation]`. An invalid file refuses the delegation with its reason.

## [0.15.0] — 2026-09-25

### Added

- **The daemon listens on one fixed port, 9785, and you can choose another.** Until now the daemon picked a random free port every time it started, so its address changed on every restart. That made it hard to open in a firewall, reach from another machine, or put behind a reverse proxy. It now listens on 9785, next to the discovery port 9786. To use another port, pass `--port N` to `maestro daemon start`, `maestro daemon restart` or `maestro-daemon`, set `MAESTRO_DAEMON_PORT`, or set `[daemon] port` in `~/.maestro/config.toml`. The first of these that is set wins, and `0` still means any free port. If the port is used by another program, the daemon does not start, and the error names the port and these options. Clients still find the port in `daemon.json`, so nothing else needs to change.

- **The agent is told not to commit, unless the task allows it.** Maestro never commits, and the supervisor reviews the agent's changes and commits them. But the agent's prompt only said `commit policy: branch`, so agents often tried to commit anyway. Codex's sandbox then refused to write `.git/index.lock`, and the agent reported a failure that was not one. The prompt now says: do not commit, do not create, switch or delete branches, and leave the changes uncommitted. A handoff can allow commits with `[expectations] agent_may_commit = true` (CLI: `delegate --agent-may-commit`). The prompt then allows commits to the checked-out branch, but still forbids pushing and switching branches. The fix-pass prompt follows the same setting. `agent_may_commit = true` together with `commit_policy = "no-commit"` is refused.
- **Several tasks can run in one workspace at the same time.** Until now a workspace ran one task at a time, and every other task for it waited in a queue. A task that stopped to ask a question kept its place, so if nobody answered, every later task for that workspace waited forever. Now the first task runs in the workspace, as before, and a task delegated while the workspace is busy runs in a git worktree of its own under `~/.maestro/worktrees/<task-id>`, on its own branch, and starts at once. The worktree starts from the commit checked out in the workspace; uncommitted changes in the workspace are not copied. Each task stays in the directory where it first ran, so its follow-ups and answers continue there. At most 4 tasks run turns at the same time for one workspace; set `[defaults] max_parallel` to change this, and `max_parallel = 1` gives the old behaviour. A task waiting for an answer does not count toward the limit. `commit_policy = "no-commit"` tasks work in place, so they still wait for the workspace. The design is in `docs/design-parallel-tasks.md`.
- **Where a task's work is: `run_dir`.** `task status`, `task audit`, receipts, MCP results, A2A task metadata, the terminal dashboard and the web console show `run_dir`: the workspace, or the task's worktree. A worktree task's changes are there, not in your checkout, so review them there. When a worktree task's directory does not exist, `task status` and the task metadata also carry `run_dir_missing: true`, and the next turn creates the worktree again from the branch. Tasks created before this version report their workspace.
- **Removing worktrees: `maestro task cleanup <task> [--force]`**, the MCP tool `cleanup_task_worktree` and the A2A method `tasks/cleanup`. They refuse while the task is running, and refuse when the worktree has uncommitted changes unless `--force` is given. The branch is always kept, and the workspace itself is never removed. `maestro gc` now also removes the worktrees of the old tasks it deletes, but keeps any task whose worktree has uncommitted changes and lists it under `kept_worktrees`.
- **A time limit for verification.** After the agent finishes, Maestro runs the project's tests. That run had no time limit, so tests that hung (waiting for a database, the network or a prompt) kept the task working forever, and the task kept its workspace. The tests may now run for `[verification] timeout_s` seconds, 1800 (30 minutes) by default. When the time runs out, Maestro stops the command and every process it started, and verification fails with a reason that names the setting. Set `timeout_s = 0` for no limit.
- **You can see when a task is running its tests.** While verification runs, the task's phase is `VERIFYING` instead of `IMPLEMENTING`, and the task's output shows `[verify] running: <command> (time limit 30 minutes)` and then how the command ended. `maestro task tail` and the dashboards show these lines.

### Changed

- **`maestro task status` always shows the task's `state`** (`submitted`, `working`, `input-required`, `completed`, `failed` or `canceled`) when it is recorded, not only for a task waiting for an answer. The `phase` field is coarser: a waiting task and a finished one both show `REVIEWING`.
- **A queued delegation says why it waits.** The CLI and the MCP `delegate` tool used to print "workspace already has an active task; this handoff is next in line". They now print the daemon's reason: the workspace is at its limit of running tasks, or the task works in place (`no-commit`) or has its work in the workspace, and another task is using the workspace. Follow-ups and answers that have to wait carry the same `reason`.
- **Verification in a worktree uses the main checkout's virtual environment** (`.venv/` or `venv/`) when the worktree has none, after `MAESTRO_PYTHON` and the run directory's own virtual environment.

### Fixed

- **The terminal dashboard shows the task list as soon as it opens.** `maestro dashboard` loaded the tasks at start but drew nothing until the daemon sent an event or a key was pressed. When no task was running, the daemon sent no events, so the screen stayed empty. The dashboard now draws the first screen straight away.
- **The terminal dashboard shows the title, route and workspace of a task delegated after it opened.** The dashboard loads the task list once, when it opens. A task delegated later reached it only through live events, which carry the state and output but not the title or the agents. Its row showed the task id, and its detail showed `route: ? → ?`. The dashboard now asks the daemon for the task's record (JSON-RPC `tasks/get`) the first time it sees an event for it. If that request fails, the row still appears, as before.

## [0.14.0] — 2026-09-24

### Added

- **Answer or cancel a waiting task from the shell.** Before this change, a task in state `input-required` could only be answered or canceled through the MCP tools `answer_task_question` and `cancel_task`. When no session had those tools loaded, the task stayed parked for good. It also kept its workspace, so every task delegated to that workspace afterwards waited behind it in the queue. `maestro task answer <task> <answer>` and `maestro task cancel <task> [--reason TEXT]` now do the same through the running daemon, using the existing A2A methods `tasks/answer` and `tasks/cancel`. `task answer` streams the resumed turn like `task continue`, or prints the result with `--no-wait`. If the task is still waiting after the answer, for example because a routing answer named an unknown agent, the command prints the result and exits instead of waiting.

### Fixed

- **`maestro task status` shows what a waiting task is asking.** The status's `phase` reported a waiting task as `REVIEWING`, the same phase a finished task has, and the question was not shown anywhere in the CLI. For a task in state `input-required`, the status now includes `state`, `awaiting` (`routing`, `approval`, `question` or `gate`) and `question`.
- **The terminal dashboard draws each row at the start of its line.** `maestro dashboard` puts the terminal in raw mode, which turns off the terminal's own conversion of a line feed into a carriage return plus line feed. Each row therefore began in the column where the previous row ended, and the screen was unreadable. The dashboard now writes `\r\n` at the end of each line.

## [0.13.0] — 2026-09-23

### Added

- **Choose the task branch name when you delegate.** Until now every task branch was called `maestro/<task-id>`, and the only way to follow a repository's naming convention was to rename the branch by hand afterwards. A handoff can now name the branch: `[expectations] branch = "feat/login-form"` in the file, `maestro delegate --branch feat/login-form` on the CLI, or `delegate(..., branch="feat/login-form")` over MCP (the flag and the MCP argument override the file). The name is checked against git's branch-name rules when the handoff is read, and delegation is refused if the branch already exists, so an agent never starts work on a branch that holds something else. Delegation is also refused when the name clashes with an existing branch as a folder: git keeps `feat/login` as a file inside a folder called `feat`, so with a branch `feat` present, `feat/login` cannot be created, and with `feat/x` present, `feat` cannot be created. Before this check, such a name passed delegation and the turn then failed in `git checkout -b`. Setting a branch together with `commit_policy = "no-commit"` is refused, because that policy creates no branch. Every later turn of the task, including follow-ups, stays on the named branch. If git cannot create the branch when the turn starts (for example, two queued handoffs asked for the same name, or someone created it after delegation), the task fails with that reason instead of letting the agent work on whatever branch was checked out. The failure message names the commands that fix it: `maestro task rename-branch <task> <new-name>` changes the name the next turn will create, and `maestro task continue <task> --request "…" --branch <new-name>` does that and starts the turn in one step. The name applies only to the workspace of the daemon that runs the task. When the task runs on a remote daemon (an `a2a_remote` agent), the handoff Maestro forwards to it has `branch` removed, because the remote gets a new request on every attempt and every turn and would otherwise refuse each one after the first as "already exists". The remote daemon puts its work on its own default branch, `maestro/<remote-task-id>`.

- **Rename a task's branch after it has run.** `maestro task rename-branch <task> <new-name>`, the MCP tool `rename_task_branch`, and the A2A method `tasks/renameBranch` rename the git branch and update the task's durable record in one step. Before this, a branch renamed with `git branch -m` left `maestro task list`, `task status` and receipts showing the old, deleted name, and a follow-up would recreate the old branch from the current checkout. If the branch was already renamed by hand, the command only updates the record, after checking git's reflog to confirm the new branch was renamed from the task's branch; an unrelated branch that happens to exist is refused. If the task has no branch yet, for example because its first turn could not create the branch it asked for, the command changes the name that the next turn will create, and its result has `"pending": true`. The rename is refused while an agent is working on the task or a turn is starting, when the new name already exists or clashes with an existing branch as a folder, and when neither name exists. The check and the rename run under the daemon's lock, and follow-ups and answers start their turn under the same lock, so a follow-up sent at the same moment waits for the rename to finish instead of checking out the old name. It works after a daemon restart, and the CLI writes the state directory directly when no daemon is running. The daemon publishes a `branch` event so the terminal dashboard shows the new name straight away. Only the local branch is renamed; a copy already pushed to a remote keeps its old name.

- **Rename the branch as part of a follow-up.** `maestro task continue <task> --request "…" --branch <new-name>`, the MCP `followup` tool's `branch` argument, and the `branch` parameter of the A2A method `tasks/followup` rename the task's branch first, with the same rules as `rename-branch`, and then run the turn on it. For a task that has no branch yet, they set the name the turn creates.

### Changed

- **The test suite runs in parallel.** Tests now run with pytest-xdist, one worker per CPU, and coverage is collected from every worker with pytest-cov. On a 10-core Mac the full suite (1672 tests, 100% line and branch coverage) went from about 13 minutes to 2 minutes. Each worker uses its own discovery port, so daemons started by parallel tests never hear each other. In tests, the cancel poll and the durable-state poll run every 50 ms instead of every 500 ms; these intervals only decide how soon a waiting loop notices a change, so no outcome changes. CI runs the coverage gate on Python 3.12 and runs the tests without coverage on 3.11 and 3.13. To run the suite locally: `python -m pytest -n auto -q --cov --cov-fail-under=100`.

### Fixed

- **A Python project's tests are no longer skipped when pytest is missing.** Maestro looks for pytest in `MAESTRO_PYTHON`, then in the project's `.venv/`, then in Maestro's own interpreter. When pytest was not in the interpreter it picked, verification used to fall back to `git diff --check` and reported PASSED whenever the workspace had changes, so a project whose environment lives in `venv/`, in poetry or conda, or in the main checkout of a git worktree had its failing tests skipped. Now, when the project clearly has a test suite and pytest cannot be imported, verification fails. A project has a test suite when it has a `tests/` or `test/` directory anywhere outside hidden directories, virtual environments and `node_modules` (for example `src/pkg/tests/`), a `conftest.py` at the root, a file named `test_*.py` or `*_test.py`, or a pytest configuration. In a git repository Maestro reads the file list from `git ls-files`; otherwise it walks the directory tree and stops after 20,000 entries. The report names the interpreter and says how to fix it: set `MAESTRO_PYTHON`, or configure an explicit verification command. Maestro also looks for a `venv/` directory in addition to `.venv/` when it chooses the interpreter.

- **A Makefile without a `check` target no longer fails every task.** Maestro used to run `make check` for any repository with a `Makefile`, and every task then failed with "No rule to make target 'check'". Maestro now runs `make check` only when the makefile defines an explicit rule whose targets include `check`, such as `check:`, `check::` or `lint check:`. The makefile is the first of `GNUmakefile`, `makefile` and `Makefile` that exists, which is the one make reads, and files it includes by a literal path inside the workspace are read too. The decision is made from the text alone and make is never run, because even a `make -n` dry run can create files, and a dry run also accepts targets that only a built-in or catch-all rule can build, for which `make check` runs no tests. Lines inside `define ... endef` blocks, recipe lines, and target-specific variables such as `check: PYTEST_ARGS = -q` do not count as a `check` rule. Otherwise detection continues with the next test runner.

- **A Python project with no tests no longer fails every task.** pytest exits with code 5 when it collects no tests, and Maestro treated that as a test failure. Maestro now records at the start of each turn, before the agent runs, whether the project has a Python test suite, and keeps that in the task record so a restarted daemon still has it. For the auto-detected pytest command, exit code 5 means "no tests to run" only when the project had no test suite at the start of the turn. It is then not a failure, but it does not prove any work was done either, so the task passes only if the turn left working-tree changes or new commits, which is the same rule that applies to the `git diff --check` fallback. When the project had a test suite at the start of the turn, exit code 5 is a failure, and the report says that pytest collected no tests although the project had a test suite, so an agent cannot pass by deleting or hiding every test. The same record is used when pytest is missing: a project that had a test suite at the start of the turn is verified with the failing pytest command, with the note that says how to fix it, even if the agent deleted every test, instead of falling back to `git diff --check`. A task with no recorded value, such as one started by an older Maestro, is treated as having had a test suite for exit code 5; for missing pytest, the workspace as it is now decides. An explicitly configured command still fails on any non-zero exit code.

- **The default `npm init` test script is no longer treated as a failing test suite.** The script `echo "Error: no test specified" && exit 1` always fails. Maestro now recognises it as "this package has no tests" and continues with the next test runner instead of running `npm test`.

- **Arrow keys quit the dashboard.** The help line says the arrow keys select a task, but an arrow key sends ESC [ A, and the dashboard quit on the first ESC byte. It now reads the whole sequence, so up and down arrows move the selection and a lone Esc still quits. The arrow keys also work when the terminal sends them in application cursor mode (ESC O A and ESC O B). The dashboard waits at most 50 ms for each further byte of a sequence. If a sequence stops early, for example after Alt+[, the dashboard ignores it instead of waiting, so the next key you press is read on its own. When Esc is followed at once by a byte that does not start a sequence, such as a second Esc, the dashboard treats the first byte as the Esc key and quits.

- **The terminal dashboard no longer adds a duplicate row for every event.** `maestro dashboard` loaded the task list from `GET /tasks` but did not index those tasks by id. Every event, including the history the daemon replays on connect, therefore added a new untitled row at the top, and the original row never changed from its first state. The loaded tasks are now indexed, so each event updates the row of its task.

- **The terminal cursor is visible again after you quit the dashboard.** On exit the dashboard wrote the escape sequence that hides the cursor instead of the one that shows it, so the shell was left without a cursor. It now writes the show-cursor sequence before it leaves the alternate screen.

- **`maestro task tail 1` follows task number 1 instead of waiting forever.** The command passed the number straight to the event stream, which only matches full task ids, so no event ever arrived. It now asks the daemon to resolve the number or id first, and an unknown reference exits 2 with a message. A task migrated from the legacy journal without a workspace claim is followed too: the daemon's `tasks/get` answer now falls back to the workspace in the task's registry record, the same fallback `maestro task status` uses, so such a task is no longer reported as unknown.

- **`maestro task tail` no longer waits forever on a task that has already finished.** The task's event stream only carried events that the running daemon had published and still kept in its replay buffer. For a task that finished in an earlier daemon run, or whose events had left the buffer, no final state ever arrived, so tail never returned. When the stream for a finished task starts and its final state is not in the buffer, the daemon now sends the task's final state, with its error if it failed, and ends the stream. Tail prints the result and exits 0 for a completed task and 1 for a failed or canceled one.

- **`maestro task continue` works on a task migrated from the legacy journal.** After a restart the daemon rebuilds a finished task's record from its durable claims, and it required the `task_workspace` claim. The legacy migration writes that claim only when the old journal had one, so continuing such a task failed with "Unknown task reference". The daemon now falls back to the workspace in the task's registry record, both when it rebuilds the record and when it builds the task-knowledge snapshot for the follow-up turn. `maestro task tail --all` also works now; before, it failed because a task reference was required even with `--all`. Giving neither a reference nor `--all`, or giving both, exits 2 with a message.

- **`maestro doctor` reports a storage backend that cannot be loaded instead of crashing.** With `[storage] backend = "memvara"` configured and the `memvara` package not installed, `maestro doctor` stopped with a Python traceback. It now reports "storage backend unavailable" as a blocking problem and exits 1, like other configuration problems.

- **The `maestro task <n>` shorthand works after top-level options.** `maestro --workspace /repo task 1` failed with "invalid choice: '1'", while `maestro --workspace=/repo task 1` worked. The shorthand now finds the `task` command after skipping the top-level options and their values. It also no longer rewrites a `task` token that is an argument of a different command.

- **A per-task model or effort now overrides the registry default for Copilot, Cursor, Hermes, Cline and Pi.** These adapters read the registry entry first, so a task that asked for a different model still ran on the registered one. They now read the task settings first and fall back to the registry entry, as the Codex, Claude Code and OpenCode adapters already did.

- **A per-task model or effort now reaches only the task's target agent.** The daemon used to pass the handoff's `model` and `effort` to every agent in the task, so a Cursor fallback registered with `sonnet-4` would have been started with a Codex model name. Fallback agents, the verifier and reviewer, and a fixer that is a different agent now run with their own registry model and effort. Other `agent_settings` keys still reach every agent. `[defaults].model` and `[defaults].effort` also no longer override a model or effort set in the target agent's registry entry; they fill in only when neither the handoff nor the registry sets one. When a task asks Cline for an effort it does not support, such as `max`, Cline now uses its registry effort instead of dropping `--thinking`.

- **Generic command templates now work with real paths and prompts.** The template is split into arguments first, and each placeholder is then filled inside its own argument in a single pass. A workspace path with spaces now stays one argument, a path with an apostrophe no longer fails the task with "No closing quotation", and a prompt that contains the text `{workspace}` or `{task_id}` is passed through unchanged. The executable name is now read with the same quoting rules, so a quoted path with spaces is found. A template with unbalanced quotes now fails with an error that names the agent. When the template starts a shell with a script, as in `sh -c "mytool {prompt}"`, the values placed inside that script are shell-quoted, so text such as `$(...)`, backticks or `;` in a prompt cannot run as commands. This applies to `sh`, `bash`, `zsh`, `dash`, `ksh` and `fish` with `-c` (or a combined option such as `-lc`); every other argument receives the value raw.

- **Handoff documents with wrong field types are now rejected with a clear error.** A single string in a list field (for example `fallback = "claude"`) used to be split into one-letter agent names; it is now refused. A `max_depth_remaining` that is not a real integer (null, `0.5`, `true` or the string `"2"`), a list in `mode` or an agent field, a non-numeric `budget_hint` and similar mistakes now raise a ValueError that names the field, which the MCP and A2A handlers report to the caller instead of crashing. A null `title` or `request` now counts as missing instead of becoming the text "None".

- **A skill that cannot be staged no longer fails the turn.** If a skill directory is missing or cannot be copied when a turn starts, the context block now says the skill is unavailable and why, as the context rules promise, instead of raising an error. Relative skill paths are resolved against the task workspace. The daemon's delegate-time skill check now uses the new `check_skill_entries(entries, workspace)` function with the task workspace, so the check and the copy made when a turn starts look in the same directory. Before, the check resolved a relative path against the daemon's own working directory.

- **Context entries whose labels reduce to the same file name no longer overwrite each other.** Labels such as `code review` and `code/review` both became `code-review`, so one staged skill or large-file copy replaced the other. The later entry now gets a numbered name. Names that differ only in letter case also count as the same name.

- **The daily budget cap now counts money spent today on older tasks.** Daily spend was grouped by the day the task started, so a follow-up turn today on a task from an earlier day counted as zero. Each attempt is now dated by its own finish time, and an attempt without one is dated by the task start time.

- **Saving an agent whose fields contain control characters no longer corrupts the registry.** Carriage returns, escape characters and other control characters are now escaped as TOML requires, and keys that are not plain words are quoted. The registry now checks that the new text is valid TOML before it writes anything, and writes through a temporary file that is renamed into place, so a failed save can no longer replace a good entry with a broken one. The temporary file is removed after any failure, including a text that cannot be encoded as UTF-8, so a failed save never leaves a copy of the token behind. The file is readable by its owner only (mode 0600), because an entry can hold a bearer token.

- **Reinstalling the skill for OpenHands now replaces the old skill text.** The managed block was left unchanged when it already existed, so upgrades kept the old instructions while reporting success.

- **The Codex skill is now installed to `~/.codex/AGENTS.md`.** Current Codex CLIs read global instructions from that file and ignore `~/.codex/instructions.md`, where Maestro used to write. Installing or uninstalling now also removes a managed block left in the old file, and deletes the old file when the block was all it held.

- **An instructions file that is not valid UTF-8 no longer breaks the skill commands.** A file such as `~/.codex/AGENTS.md` saved in another encoding made `maestro skill status`, `install` and `uninstall` stop with an error for every agent. `status` now reports the problem in an `error` field for that agent, and `install` and `uninstall` refuse that one file with a clear message and never overwrite it. The other agents are handled normally.

- **Without a preset fixer, fixes now go to the agent that implemented the task.** When a work mode had no `fixer` and the handoff named its own target, fix bounces went to the preset's implementer instead of the handoff's target. The fixer now defaults to the task's actual implementer.

- **`maestro gc` could lose claims the daemon wrote while it ran.** gc rewrote the claim journal without a lock, so claims the daemon appended during the rewrite disappeared and a task's status went back to an older value. Appends and rewrites now take the same lock file, and rewrites use a unique temporary file.

- **`maestro gc` could delete a task the daemon had just registered.** gc saved the task registry without the registry lock, and both writers used the same temporary file. gc now removes a task through one locked step. Under the same locks it checks again that the task is still finished and still old, so a task whose status changed after gc chose it (a follow-up that started, say) is kept.

- **A damaged `registry.json` wiped the task list.** A registry that could not be parsed was read as empty, and the next registration wrote a registry holding only the new task, restarting numbering at 1. The damaged file is now moved aside and the registry is rebuilt from the task claims; a missing registry is rebuilt the same way. The move and the rebuild happen under the registry lock, after reading the file again, so a file that another process has just repaired is never moved away. The rebuilt registry is saved, so it is not rebuilt again on every listing. The rebuild keeps tasks that have claims but no `task_number` claim (they get a new number), and takes `created_at` from the time in the task id or the task folder. A registry that exists but cannot be read at that moment (too many open files, no permission) now stops the command with an error instead of being rebuilt and written over.

- **Task numbers were reused after gc.** The next number was the highest number in the registry plus one, so removing the newest task gave its number out again. The highest number ever given out is now kept in `task-counter`. Importing a legacy project journal (`.maestro/project-state.jsonl`) now uses the same counter and the registry lock.

- **Locking failed silently where it is not available.** On Windows (no `fcntl`) and on file systems that refuse `flock`, Maestro ran without locks and said nothing. It now prints a warning once that names the lock file.

- **`maestro gc` crashed on the memvara backend** after deleting the task's files, and crashed again on every later run. It is now refused before anything is changed.

- **`maestro task list` got very slow as tasks accumulated** (85 seconds for 300 tasks), because it re-read the whole claim journal for every claim of every task. Parsed claims are now cached until the journal changes on disk, and the registry is read once per listing. A change is detected from the file's inode, size, modification and change times, and its last 512 bytes, because on Linux ext4 a rewritten journal can report the same stat values as the old one.

- **One byte that is not valid UTF-8 killed a healthy run.** Agent output was decoded strictly, so a single bad byte (a Latin-1 file, a binary test log) stopped the output reader, and Maestro then killed the agent. Output is now decoded with bad bytes replaced; version and auth probes are decoded the same way.

- **A background child could hang a finished run.** If the agent started something in the background that kept its output open, a successful run waited for that child and reported a timeout, or hung for ever with no timeout set. Once the agent's own process exits, Maestro now waits two seconds for remaining output and then finishes the run.

- **Processes an agent left running were never stopped.** At the end of every run Maestro now stops any process still in the agent's process group, including after a success. A timeout or cancel also reaches those children when the agent itself has already exited; before, `os.getpgid` failed in that state and the kill was skipped. A killed agent is now reaped.

- **A large prompt could deadlock a run.** A prompt over 64 KB sent on stdin to an agent that printed a lot before reading it filled both pipes, with no timeout or cancel able to stop it. The prompt is now written from its own thread after output reading starts; RPC agents get the same treatment for their start command, and a cancel never interleaves the abort command with a start command that is still being written.

- **Multi-line environment values were cut.** The login-shell snapshot parsed `env` output line by line, so a value such as a PEM key was cut to its first line, and a continuation line shaped like `NAME=value` became a fake variable. It now reads `env -0` output after a marker, so values stay whole and profile banners are ignored.

- **A child that kept printing still held a finished run open.** The two-second exit grace only started after half a second with no output, so a background child that printed at least that often (a dev server, `tail -f`, a watcher) kept the run going until its timeout, or for ever without one. The grace now starts when the agent's own process exits, whatever the child prints, and the run ends when it expires. Output that arrives during the grace is still recorded.

- **The agent's last line could be lost when the run ended through the exit grace.** Lines that arrived after the grace ended were never read, and a last line without a trailing newline only arrives when the output closes, which happens after the leftover processes are stopped. That line is often the agent's question or error. Maestro now reads everything still waiting after it stops the leftovers, including that last line, and handles it like any other output: it is logged, checked for a question and included in the error message. This applies to spawn and RPC agents. For an RPC agent, a done or fail event among these last lines settles the run, unless an earlier event already did. A process that moved itself out of the agent's process group and still holds the output open is not covered: Maestro waits at most five seconds for the output to close, and a last line without a newline is then lost.

- **Cancelling an RPC agent that had stopped reading its input could hang the run.** The abort command was written on the run's own thread with no time limit, so with the input pipe full, neither the cancel grace nor the timeout could fire. The abort is now written from a separate thread. If the agent is still running when the five-second cancel grace ends, whether the abort could not be written or the agent ignored it, Maestro now stops the process group at once. Before, an agent that read and ignored the abort was given another five seconds to exit after its input was closed, so a cancel took about ten seconds instead of five.

- **Processes in an agent's group had no time to clean up.** After a cancel or timeout, the group got SIGKILL as soon as the agent's own process exited, and leftovers after a successful run got only SIGKILL. A child that was about to remove a lock file, such as `.git/index.lock`, left it behind and blocked later git commands. Every stop now sends SIGTERM to the whole group first, gives it up to two seconds to exit, and then sends SIGKILL to whatever is left. After a successful run this adds at most two seconds, and nothing when the agent left no process behind. After a cancel or timeout, the agent's own process still gets up to five seconds after SIGTERM, and up to five more after SIGKILL, so a stop takes at most about ten seconds.

- **A leftover kill could signal an unrelated process group.** Once the agent's children had all exited, its group id was free, and the pid could be reused before Maestro sent SIGKILL to that id. Maestro now checks that the group still exists before each signal (`os.killpg(pgid, 0)`), and never signals a group it has already seen gone, or one that now belongs to another user. The kernel does not reuse a pid while a group with that id exists, so the only remaining gap is the moment between the check and the signal; see the comment above `_group_alive` in `maestro/adapters/base.py`.

- **Two version probes still stopped on bytes that are not valid UTF-8.** `maestro agents status` (which the MCP `agents_list` tool also uses) and `maestro doctor` (including its `git --version` check) decoded `--version` output strictly and failed with UnicodeDecodeError, although this changelog already said version probes replace bad bytes. They now replace them too.

- **A login-shell value that was not valid UTF-8 reached agents changed.** The snapshot replaced bad bytes with U+FFFD, so a path such as `/data/caf\xe9` named a different path in the agent. Values are now decoded like `os.environ` (surrogateescape) and reach the agent byte for byte. A `\r\n` inside a value is also kept as it is, instead of becoming `\n`.

- **The login-shell snapshot was silently empty where `env -0` does not work.** With an `env` that does not support `-0`, nothing followed the marker and an empty environment was cached without a word, where plain `env` used to work. The login shell now prints an `env -0` listing and then a plain `env` listing in one run, so a slow profile gets the whole `MAESTRO_LOGIN_ENV_TIMEOUT_S` limit. If `env -0` fails or prints nothing, Maestro reads the plain listing line by line as before and prints one warning to stderr that multi-line values may be cut. When nothing can be captured, because the shell is missing, times out, never runs the command (`SHELL=/usr/bin/false`, `/sbin/nologin`, a profile that exits early) or prints neither listing, Maestro prints one warning that the login environment could not be captured, instead of returning an empty environment without a word.

- **Queued tasks after the second were lost.** With three or more tasks queued for one workspace, freeing the workspace promoted the next task and also removed every later task for that workspace from the queue; those tasks stayed `submitted` forever. The promoted task now takes the workspace slot at the moment it leaves the queue.

- **A follow-up ran its instruction as a shell command.** On a task with `verification = "command"`, the follow-up's instruction replaced the verification command, so "touch X" was executed. The original command is now carried into every follow-up. A new optional `[expectations] verification_command` field also lets a handoff keep `request` as prose.

- **Two tasks could work in the same working tree.** Follow-ups, and answers given after a daemon restart, started without taking the workspace slot. They now take it, or wait in the queue behind the task that holds it.

- **Cancel was not noticed while an agent printed nothing.** The run checked for a cancel only when a line of output arrived, so a quiet agent kept editing files after its workspace was handed to the next task. The check now happens at least every half second in the spawn, RPC and `a2a_remote` run loops. For an `a2a_remote` agent, the cancel is sent to the remote daemon even when the remote agent sends no events; if that request fails, it is tried again every half second.

- **A canceled task could come back.** A cancel set a flag that was never cleared, so every follow-up on a canceled task failed at once. A cancel during a review or verification turn was overwritten and the task carried on to fixes and reviews. Follow-ups now start with a clear flag, the gate cycle stops after a canceled gate turn, and a canceled task only leaves `canceled` through a new turn.

- **A queued task canceled while it was being started ran anyway.** A cancel that arrived after the task left the queue, but before its turn began, did not stop it, and the workspace was not handed on. The task now stops at whichever step the cancel reaches it, runs no agent, and the workspace goes to the next queued task.

- **An old turn could interfere with a follow-up.** After a cancel and a follow-up, the canceled turn could still be finishing, for example in its verification. It then marked the follow-up's turn completed, freed the workspace the follow-up was using, or started a reviewer, fixer or retry of its own, because it looked up the task's current cancel flag, which the follow-up had just cleared. Each turn now keeps its own cancel flag from start to end. A turn that was canceled or replaced cannot change the task's state, free its workspace or start an agent.

- **Two follow-ups or two answers at the same moment both ran.** Both calls passed the state check before either started a turn. The check and the start of the turn now happen in one step, so only one call starts a turn and the other gets an error. An answer now moves the task out of `input-required` at once (to `submitted`, then `working`), so a `task_wait` right after an answer waits for the new turn.

- **A task could be reported both completed and canceled.** A cancel checked the state and then changed it in two steps, and a turn that completed in between was then also marked canceled. The check and the change now happen in one step, under the same lock the turn uses to finish.

- **A stale routing question captured later answers.** A task parked on the routing question kept that mark after a cancel and a follow-up, also across a daemon restart. A later ordinary answer was then read as an agent choice and failed with "Unknown agent". Leaving `input-required` for any reason now clears what the task was waiting for and its question, and every park records its own reason.

- **Answers to parked tasks were lost.** An answer to a task parked by a gate, or waiting for approval, never reached the agent's next prompt. The question and the answer are now recorded together. After a daemon restart, a task parked on the routing question also forgot that it was waiting for routing and asked again; the rebuilt task now keeps what it was waiting for and its gate and verification details.

- **Waiting could miss the finishing event.** `wait()` (used by the MCP `delegate`, `followup` and `task_wait` tools) checked the state before subscribing, so a task that finished in between left the caller blocked for the full timeout. It now subscribes first.

- **`task continue` and `task tail` stopped at the previous turn.** A task's event stream replayed earlier turns and ended at the first "completed" it saw, while the new turn was still running. The stream now starts at the task's current turn.

- **A completed task could say verification `FAILED` after a fix made it pass.** The label now uses the final verification result.

- **A task turn no longer runs on the wrong branch when checking out its branch fails.** Before, if `git checkout` of the task's existing branch failed (for example, an uncommitted edit on another branch would be overwritten), the turn carried on in whatever branch was checked out while the record still named the task branch. The agent could then work on `main`. The turn now fails with git's reason before any agent runs.

- **A later turn no longer creates a new, empty branch when the task's branch is missing.** Before, if the task's branch had been renamed or deleted, the next follow-up ran `git checkout -b` with the old name, which quietly created a fresh branch from whatever was checked out, without the task's earlier commits. Now, when the recorded branch is missing, the turn looks in git's reflog for a rename. If exactly one branch was renamed from it, the turn uses that branch, updates the task's record and publishes a `branch` event. If git has no record of a rename (the branch was deleted), or records more than one branch that came from it (a renamed branch was later copied with `git branch -c`), the turn fails before any agent runs, and the message says how to fix it. The first turn of a task still creates its branch as before.

- **A second daemon no longer takes over a state directory that a live daemon owns.** Before, the MCP server's built-in daemon or a foreground `maestro-daemon` started on the same state directory as a running daemon, overwrote its `daemon.json` marker, and marked that daemon's running tasks as failed. The daemon that serves HTTP now holds a lock file (`daemon.owner.lock`) for its whole life. A second `maestro-daemon` refuses to start, names the running daemon, and exits with status 1. The MCP server does not start a second daemon either: it sends its tool calls to the running daemon (see the next entries).

- **`maestro daemon stop` no longer signals an unrelated process.** After a crash, the marker's pid can be reused by another program. `stop` used to send it SIGTERM and then SIGKILL, and `start` refused to start because the pid looked alive. `status`, `stop` and `start` now confirm that the pid is the daemon that wrote the marker: it must hold the owner lock, or, for a marker written by an older version, its HTTP endpoint must answer with a Maestro agent card. A card that names a pid and state directory (this version adds them) must name the marker's pid and the same state directory. A card from 0.12.0 or earlier names neither; it is accepted when the marker's pid runs a Maestro daemon or MCP server: one word of its command line must be the program `maestro-daemon` or `maestro-mcp`, or the word after `-m` must be `maestro.daemon_main` or `maestro.mcp_server`. Whole words are compared, so a command such as `vim maestro-daemon.py` does not count, so a daemon started before an upgrade is still found and stopped instead of left running beside a new one. When the identity cannot be confirmed, the marker is treated as stale: `stop` removes it without signalling anything and says so, and `start` starts a new daemon. `stop` never removes the marker while a daemon holds the owner lock.

- **P2P discovery accepts less from the network.** The presence socket was bound to every address, so anyone who could reach UDP port 9786 could add a peer with any name and any URL. The receive socket is now bound to the multicast group and the send socket to `MAESTRO_DISCOVERY_IF`, and a loopback interface ignores announcements from other hosts. An advertised host must be an IP address (a value such as `169.254.169.254/latest/meta-data#` is dropped), the port must be valid, and names lose control characters, so `maestro peers list` cannot print terminal escape codes. `peers.json` holds at most 256 peers, prunes discovered peers unheard for an hour, and is rewritten only when something changed instead of on every packet. A daemon that listens on loopback only no longer runs discovery unless `MAESTRO_DISCOVERY=1` is set.

- **The HTTP API refuses requests that could tie it up.** A negative `Content-Length` made the handler wait until the client hung up, and any declared size was read into memory. A negative or non-numeric `Content-Length` now gets 400 and a body over 8 MiB gets 413, before anything is read. Each event-stream (SSE) client used to have a queue with no limit, which grew for as long as the client stopped reading; the queue now holds at most 2048 events, and a client that falls further behind, or that accepts no data for 30 seconds, is disconnected. A client that fell behind is first sent an `overflow` event (see the next entries). `EventBus.subscribe` takes an optional `maxsize` for this; callers that do not pass it keep an unbounded queue.

- **Malformed A2A requests get an error reply instead of a dropped connection.** A JSON-RPC request whose `params` was not an object raised an exception inside the handler, and the client saw the connection close with no reply. Such a request now gets error `-32602`, a message whose `parts` is not a list is treated as having no parts, and any other unexpected exception in the dispatcher becomes error `-32603` (HTTP 500), with the traceback written to the daemon's stderr.

- **The MCP server sends its tool calls to the daemon that owns the state directory.** Before, when a daemon was already running (for example one started with `maestro daemon start`), the MCP server started a second daemon without an HTTP endpoint and ran its tasks there. The running daemon could not cancel those tasks: `tasks/cancel` answered "canceled" while the agent kept working and later completed. The two processes also kept separate workspace queues, so one workspace could have two active tasks. Now the MCP server runs no tasks while another daemon owns the state directory. Every tool call (`delegate`, `task_wait`, `followup`, `answer_task_question`, `cancel_task`, `rename_task_branch`, `agents_list`) goes to that daemon over its HTTP API, with the daemon's token when it has one. `delegate`, `followup` and `task_wait` still block until the task finishes or needs input and still honour their timeouts. When no daemon owns the state directory, the MCP server starts the same detached background daemon that `maestro daemon start` starts, with the MCP server's environment, and forwards to it. Closing a session therefore never stops tasks, including tasks that other sessions sent to that daemon. Only when a background daemon cannot be started does the MCP server run the daemon inside its own process, and it says so on stderr. If the daemon stops answering while it is still alive, a tool call waits and asks again with growing pauses for up to 20 seconds and then returns an error; it never starts a second daemon beside it. Once the daemon has exited, the next tool call starts a new one. A wait for a task that another process runs now reads the task's durable record every half second, so it returns as soon as that process finishes the task instead of waiting for its whole timeout. The daemon gained the JSON-RPC methods this needs: `tasks/delegate`, `tasks/wait`, `tasks/resolve`, `tasks/answer` and `agents/list`. The replies to `tasks/cancel` and `tasks/followup` now also carry the daemon's own result, under `cancel` and `followup`. A daemon from 0.12.0 or earlier does not have the methods these calls use; when one owns the state directory, the MCP server runs its tasks in a daemon inside its own process, without an HTTP endpoint and without taking the directory over, as older versions did, and prints a note on stderr saying to restart the older daemon. A blocking wait has one deadline for the whole wait, however often the daemon changes: when it passes, the wait returns the task as last seen, and when the owner answers probes but not requests, the wait gives up after 20 seconds with an error instead of reconnecting for ever.

- **A task whose daemon died is marked failed at the next start, even while another daemon is running.** A leftover "working" task was failed at startup only when no other daemon process used the state directory. The MCP server's daemon used the directory for as long as the host agent ran, so a task whose daemon had crashed stayed "working" for ever. Each task's runtime record now names the daemon process that runs it: its pid and its start time, so a later process that reuses the pid is not mistaken for it. On Linux the start time is read from `/proc/<pid>/stat` together with the boot id; elsewhere `ps` reads it in UTC, so the time zone and locale of the reading process do not change it. A daemon that starts alone in the state directory fails every leftover "working" or queued task, whatever the record says, since nothing else can be running it (after a container restart the new daemon may even have the old pid). When other daemon processes share the directory, it fails such a task only when the recorded process is certainly gone, and leaves it alone when the process still runs, when its start time cannot be read now, or when the record names no process (a record from an older version). A daemon that is about to be refused as a second daemon fails nothing.

- **The MCP server stops a daemon it runs inside its own process when it exits.** When the host agent closed the MCP server, or sent it SIGTERM, the daemon inside it was not stopped, so its tasks were never marked as interrupted and its marker was left behind. The MCP server now normally uses a background daemon (see above), which keeps running. When it had to run the daemon inside its own process, it stops that daemon on a normal exit and on SIGTERM, which removes its marker, releases its locks and marks its running tasks as failed. After that the process still ends the way SIGTERM normally ends it.

- **A port that answers with something other than HTTP no longer breaks the daemon commands.** When a stale marker pointed at a port that answered with a non-HTTP line (an SSH server, for example), the check raised an error that was not caught. The daemon that was starting kept its lock on the state directory, so every later daemon waited for ever, and `maestro daemon status`, `stop` and `start` stopped with a Python traceback. Such a reply now means "this is not a Maestro daemon", a daemon that fails to start always releases its lock, and the `maestro daemon` commands report any unexpected error as one `maestro:` line with exit status 1.

- **A client that sends a body over 8 MiB now receives the 413.** The daemon replied and closed the connection without reading the body, so the kernel reset the connection and a client such as `urllib` saw "connection reset" or "broken pipe" instead of the reply. The daemon now sends the reply first and then reads and discards the body (at most 64 MiB, and it stops when the client sends nothing for 5 seconds) before it closes the connection. A request with a bad `Content-Length` gets its 400 the same way.

- **A task's event stream says when the reader fell behind, and Maestro's readers keep following.** A reader that fell more than 2048 events behind was disconnected with only a comment line, and the comment told it to reconnect to catch up. That was not possible: the daemon keeps only the newest 1000 events for replay. `maestro task tail` then exited 0 without a final state, and the `a2a_remote` adapter failed the task while the remote task kept running. Now a task stream sends the task's final state when the task has finished meanwhile, and otherwise an `overflow` event with the task's current state; the global `/events` stream also sends `overflow`. `maestro task tail` and the `a2a_remote` adapter subscribe again after an `overflow` event and skip the events they have already seen, and `task tail` says that some output lines may be missing. When a task stream ends without a final state, both ask the daemon for the task's state with `tasks/get`.

- **Discovery no longer accepts a loopback address, or someone else's link-local address, from another host.** A host on the network could announce `127.0.0.1`, `::1` or `169.254.169.254` as its address, which pointed this machine's peer list at its own loopback services or at a cloud metadata endpoint. A loopback address is now accepted only from an announcement sent on loopback, that is, from this machine. A link-local address is accepted only when it is the address the announcement came from, so discovery still works on a link-local-only network such as a Thunderbolt bridge, and a daemon bound to a link-local address still announces itself. A host on the same network can fake the address a packet comes from, so that check stops mistakes, not a determined neighbour; the cloud metadata addresses `169.254.169.254`, `169.254.170.2` and `fd00:ec2::254` are therefore dropped from every sender. A daemon that listens on loopback only and has `MAESTRO_DISCOVERY=1` no longer announces `127.0.0.1` on a network interface; it announces only when `MAESTRO_DISCOVERY_IF` is a loopback address, and otherwise it still listens for peers.

- **The CLI no longer trusts a marker only because its pid is alive.** `maestro task tail`, `maestro delegate`, `maestro dashboard` and the other commands that talk to the daemon checked only that the marker's pid was alive. A stale marker whose pid had been reused therefore gave a connection error instead of "no daemon reachable". They now use the same check as `maestro daemon status`: the process must be confirmed as the daemon and must answer HTTP.

- **`maestro doctor` no longer reports a stale marker as a reachable daemon.** Doctor also checked only that the marker's pid was alive, so when that pid had been reused by another program and something answered on the marker's port, it reported a reachable daemon. It now uses the same check as `maestro daemon status`. A stale marker is reported as "no daemon configured", with `stale_marker: true` and a `detail` that says why, and the text report prints that reason.

- **`maestro daemon stop` confirms the daemon again before SIGKILL.** After the grace period, `stop` sent SIGKILL after checking only that the pid was alive, although the daemon could have exited and its pid been reused during the grace period. It now records the process's start time before SIGTERM and sends SIGKILL only when the pid still has that start time (or, when the start time cannot be read, when the process still holds the owner lock). Otherwise it leaves the process alone and says so.

### Security

- **A web page can no longer drive the local daemon.** A daemon on 127.0.0.1 needs no token, and until now it accepted any request that reached it. A page open in your browser could send a plain-text POST to it and start an agent in any folder, and a DNS-rebinding page could read the task list and live agent output. A loopback daemon now refuses requests whose `Host` header is not `127.0.0.1`, `localhost` or `::1`. Every daemon now requires `Content-Type: application/json` on POST, which a browser cannot send cross-site without a preflight that the daemon never approves, and refuses a POST whose `Origin` is not exactly the daemon's own address. Maestro's own CLI, MCP server, remote-agent adapter and web console already send requests that pass these checks.

- **Tokens are kept private.** The `daemon.json` marker and agent registry entries are now written with mode 0600, and an existing world-readable marker is tightened on the next start. `maestro agents list`, `maestro agents add` and the MCP `agents_list` tool show a stored token as `<redacted>`; before, `agents_list` put remote-agent tokens into the supervising model's transcript. Bearer tokens are compared in constant time. The foreground `maestro-daemon` still prints its token at startup, because that is how you give it to another machine.

- **A token file is replaced, never rewritten in place.** Unix checks file permissions only when a file is opened, so a local user who had opened an older world-readable `daemon.json` or registry entry could read a new token through that open handle after the rewrite. Maestro now writes the new content to a fresh file created with mode 0600 in the same directory and moves it over the old one, and removes that temporary file if anything fails. Agent registry entries left world-readable by older versions are set to 0600 whenever the registry is loaded; an entry the current user cannot change is skipped.

- **A reverse proxy can be allowed to POST to the daemon.** The `Origin` check is an exact-origin rule: the origin's host and port must equal the request's `Host` header. A proxy that changes `Host` to the daemon's own address broke browser POSTs through it. List the proxy's public origin with `maestro-daemon --allow-origin https://maestro.example.com` (repeatable) or in `MAESTRO_DAEMON_ALLOWED_ORIGINS` (comma-separated), which also works for `maestro daemon start` and the MCP server's daemon. An invalid entry stops the daemon from starting. Requests without an `Origin` header are not affected.

## [0.12.0] — 2026-09-22

### Fixed

- **Tasks no longer get stuck reporting `working`/IMPLEMENTING after completion or interruption** — three independent holes let a finished (or dead) task keep its old state forever:
  1. *Crashed turn threads* — a turn runs in a bare daemon thread, so any uncaught exception between the start of work and the terminal transition (adapter spawn failure, disk error writing the result file, git/subprocess failure inside verification or the knowledge refresh) killed the thread silently: the task stayed `working` with no thread left to drive it, its workspace slot leaked (later handoffs queued behind a ghost), and CLI output ended on `[state] working`. Turns are now wrapped in a crash guard that ends the task `failed` with a "turn crashed" error and frees the slot; parked (input-required) and already-terminal tasks are never overridden. The terminal knowledge refresh is also exception-isolated so a projection failure can no longer swallow the terminal state event.
  2. *Daemon restarts mid-turn* — in-memory task state was rebuilt from nothing at startup, so a task whose process died while working (SIGKILL/OOM/reboot) reported `working` forever. The daemon now reconciles durable state at startup: tasks left `working` are marked `failed` ("daemon stopped or crashed while the task was running — re-delegate, or continue this task to resume"), and queued-but-never-started tasks (the queue is in-memory) are failed too. Parked input-required tasks are left alone.
  3. *Misleading durable fallbacks* — a task with no live record and no usable status claim defaulted to `working` (and `wait()` blocked on it for the full timeout). Unknown/missing state now resolves to `failed`. The phase claim is also lossy (input-required and completed both map to REVIEWING), so all durable views now prefer the runtime snapshot's own state — written on every transition — over the phase mapping.
  `stop()` additionally marks in-flight turns `failed` before shutting down, so a graceful stop never leaves durable state claiming `working`.

- **Flaky 100% coverage gate on the a2a_remote timeout path (again, for good)** — the SSE wait loop had two equivalent timeout exits (deadline already expired at the top of the loop vs. an empty queue read), and which one fired depended on event timing; branch coverage of the pre-check line therefore rode on whether a localhost HTTP round-trip took more or less than 1 ms, flaking the CI gate (this run: Python 3.11). The wait is now clamped to `max(remaining, 0)` — required anyway, since `queue.get(timeout=<negative>)` raises `ValueError` instead of `Empty` — and the redundant pre-check is gone, leaving a single timeout exit that every timeout test hits deterministically on any machine.

### Changed

- **Implementation prompts now state the run is non-interactive** — Maestro turns are batch runs with nobody available to answer mid-run, but nothing told the agent that; an agent that stopped with "please approve this design" ended its turn there and the task completed without the work (certified `PASSED` on pre-0.11 builds). Implementation prompts now carry an explicit EXECUTION MODE directive: do not stop to ask for approval or confirmation, make reasonable decisions within the request's scope, complete the work in this run, and list open questions in the final report so the supervisor can answer them on a follow-up turn (the existing Q&A channel). Together with the 0.11 zero-work guard, a stalled turn can no longer complete as `verification: PASSED`.

## [0.11.0] — 2026-09-22

### Added

- **Task continuation + context reuse** — a finished task (completed/failed/canceled) can now be continued with a new instruction on the *same* task id, workspace, branch, and routing: CLI `maestro task continue <task-id> --request "…" [--context reuse|fresh] [--no-wait]`, MCP `followup(…, context_mode=…)`, A2A `tasks/followup`. In the default **reuse** mode the daemon projects a compact, versioned **task-knowledge** snapshot (goal, current state, files changed, latest verification result and failures, known issues, bounded tail of the last turn's output) from durable state and injects it as one labeled context entry — raw history stays in the task record and is never replayed. The snapshot budget is configurable (`[continuation] max_tokens`, `MAESTRO_CONTINUATION_MAX_TOKENS`); sections are dropped or truncated deterministically until it fits. Knowledge is persisted as a durable `task_knowledge` claim when a turn reaches a terminal state, so continuation works **after daemon restarts** (the task record is reconstructed from claims). `--context fresh` skips the snapshot for a clean reasoning context; `[continuation] enabled = false` makes all follow-ups fresh. Receipts now report turn count, knowledge schema metadata, and honest per-turn context stats (`mode`, `knowledge_chars`, `context_chars`, `raw_history_bytes`, labeled token estimate, reduction ratio); `maestro config` prints the effective `[continuation]`. Delegation depth still bounds continuation chains (each follow-up decrements `max_depth_remaining`).

### Fixed

- **Zero-work turns no longer certify as `verification: PASSED`** — when no project test runner is detected, the deterministic verifier falls back to `git diff --check`, which passes trivially on an untouched workspace. A turn that left no changes behind (e.g. an agent that stopped to ask for approval a non-interactive batch run can never receive) therefore completed as `PASSED` with nothing built. The fallback verifier now requires evidence of work — working-tree changes or new commits since the turn's baseline HEAD (recorded per turn, durable across restarts) — and fails with an explicit "no changes detected" report that includes the tail of the agent's output. Explicit verification commands and real test runners keep their existing semantics; committed work is recognized as evidence.

- **Dashboard layout on terminals narrower than 100 columns** — `maestro dashboard` rendered every frame for a fixed 100-column width, so on narrower terminals each line wrapped and the whole screen cascaded into a scattered layout. The TUI now detects the real terminal width (`$COLUMNS`, then a TIOCGWINSZ query on the stdout fd, falling back to 80) and re-queries it on `SIGWINCH` with an immediate redraw; the frame renderer also clamps its width input and truncates detail-pane values (title, workspace, branch, error, transcript lines) so no line in the frame can wrap.

- **Agents now inherit login-shell environment variables** — spawned agents
  previously received only the daemon process's environment, so profile exports
  (API keys such as `GROVE_API_KEY`) were missing whenever the daemon was
  started from a GUI, launchd, or an older terminal. `worker_environment` now
  layers a one-per-process snapshot of `$SHELL -lc env` under the daemon's own
  environment (explicit daemon values still win on conflict). Disable with
  `MAESTRO_LOGIN_ENV=0`; tune the snapshot timeout with
  `MAESTRO_LOGIN_ENV_TIMEOUT_S`. Restart the daemon after adding new variables.

- **Flaky 100% coverage gate on the a2a_remote timeout branch** — the SSE wait
  loop has two equivalent timeout exits (deadline already expired at the top of
  the loop vs. an empty queue read), and which one fired depended on event
  timing, so CI occasionally failed the coverage gate. Added a deterministic
  test that exercises the top-of-loop branch (an early event followed by
  silence).

### Changed

- **Flagship demo GIF re-recorded with a normal monospace font** — the previous
  recording rendered with wide letter spacing; the new one uses Menlo with zero
  tracking. The demo's fake implementer now leaves a real working-tree change
  (required by the new evidence-of-work verification rule), and the recording is
  reproducible from `scripts/demo-gif.tape` + `scripts/demo-gif-setup.sh`.

## [0.10.0] — 2026-09-21

### Added

- **Routing defaults + interactive routing question** — when a handoff names no
  target agent, Maestro now resolves routing from the project's
  `.maestro/config.toml` `[defaults]` table (`agent`, `fallback`, `model`,
  `effort`; later config files win per key). If neither the handoff nor
  `[defaults]` names an agent, the task parks in `input-required` with a
  question listing every registered agent (name + version) and resumes via
  `answer_task_question` / `maestro answer` — answers may be a bare agent name,
  `agent=… model=…` pairs, or JSON. `maestro delegate` flag form now accepts
  `--title/--request` without `--target/--mode`, and `maestro config` prints the
  effective `[defaults]`.
- **OpenCode adapter** — first-class support for the OpenCode CLI: spawn-mode
  adapter running `opencode run --format json --auto` (model via `-m
  provider/model`, effort via `--variant`), usage/cost parsed from the terminal
  `step_finish` event, registered in discovery and the agent registry, plus a
  global-rules integration (`~/.config/opencode/AGENTS.md`).
- **Self-contained skill** — the packaged `maestro-driven-development` skill now
  carries the full CLI reference, config schema (`[defaults]`, `[modes]`,
  `[context]`, `[codex]`, `[verification]`, `[storage]`), the MCP tool contract,
  and the routing/answer flow, so a host agent never has to rediscover Maestro's
  interface at runtime.

### Changed

- **Skill updates are delete-then-reinstall** — `maestro skill install` (and
  `install.sh`) now remove the existing installed skill before writing the new
  one, so stale files from a previous Maestro version never survive an update.
- **Demo assets renamed** — `demo-v0.9.{sh,gif}` and `docs/demo-v0.9.md` are now
  `demo-v0.10.*`, matching this release.

- **One-command installation** — `install.sh` (usable via
  `curl -fsSL … | bash`) installs Maestro into `~/.local/share/maestro` with a
  private virtualenv, exposes `maestro` / `maestro-daemon` / `maestro-mcp` in
  `~/.local/bin`, discovers and registers every supported coding-agent CLI on
  the machine (preserving existing registrations), installs the global skill
  into each detected agent, and starts/verifies the background daemon — all
  without root. Rerunning updates in place; `--uninstall` (optionally
  `--purge-state`) removes it while keeping user state by default.
- **Background daemon lifecycle** — `maestro daemon start | stop | status
  [--json] | restart`. `start` detaches the existing daemon executable in its
  own session (survives shell exit), reuses the `~/.maestro/daemon.json`
  marker as the single source of truth, refuses to start a duplicate for the
  same state directory (advisory lock + liveness check), and reports PID/port/URL.
  `stop` is SIGTERM → grace period (`MAESTRO_DAEMON_STOP_GRACE_S`) → SIGKILL,
  idempotent, and cleans stale markers; `status` distinguishes "no marker",
  "marker but dead process", and "live and answering".
- **Global `maestro-driven-development` skill** — a packaged operational skill
  (`maestro/skills/maestro-driven-development/SKILL.md`) that makes Maestro the
  default development execution backend for supported agents: trigger
  classification, daemon health check and auto-start, handoff/delegation via
  MCP tools or CLI, review + follow-up loop, agent selection left to Maestro's
  registry, and a bounded fallback when Maestro is unavailable.
- **Agent integration layer** — `maestro/integrations.py` defines one
  `AgentIntegration` per supported agent (Claude Code global skill, Codex /
  Copilot CLI / Hermes / Pi / Cline managed instruction blocks, Cursor global
  rule, OpenHands `custom_instructions`) with idempotent install/uninstall and
  status, driven by the new `maestro skill list | status | install [--agent
  NAME | --all] | uninstall` commands.
- **`maestro agents register-discovered [--dry-run]`** — converts discovery
  results into registrations programmatically; existing registrations (custom
  names/models/tokens) are preserved untouched.
- **Recursion protection** — every agent process Maestro launches receives
  `MAESTRO_AGENT_CONTEXT=1`, `MAESTRO_TASK_ID=<task>`, and
  `MAESTRO_ROLE=implementation`; the global skill instructs such workers to
  implement directly and never delegate back to Maestro.

### Changed

- `maestro-daemon` remains the foreground executable (development/CI/service
  managers); the preferred user-facing command is now `maestro daemon start`.
- The "no daemon reachable" CLI error now points at `maestro daemon start`.

### Fixed

- **`claude_code` adapter** — emit `--verbose` with `--output-format
  stream-json`, as current Claude Code releases require it in print mode and
  reject the combination without it at argument-validation time (every attempt
  failed identically). If an older CLI rejects `--verbose`, a single bounded
  retry without it self-heals the version skew.
- **`codex` adapter** — the `codex exec --help` probe is now only a first
  guess: when the installed CLI rejects the probed autonomy flag at
  argument-parse time (`error: unexpected argument … found`, e.g. a CLI that
  accepts neither `--full-auto` nor `--approve-for-me`), the adapter retries
  once with the alternate flag set and caches the choice, instead of failing
  every daemon retry with the same bad flag.
- **`hermes` adapter** — honor the usage report's `failed`/`failure` fields.
  The CLI exits 0 on API-level failures (e.g. `HTTP 401: Access denied due to
  missing subscription key`), which previously produced a silent COMPLETED
  receipt with `ok: true`; such runs now fail properly with the reported
  failure reason in the receipt.

## [0.9.0] — 2026-09-19

Productization release: **durable execution for coding agents** becomes the
front door. Maestro is now understandable, installable, diagnosable, and
auditable end to end.

### Added

- **Execution receipts** — `maestro task receipt <id|n>` (and `--json`) renders
  a task's full story from its durable state: final state, per-attempt phase /
  agent / duration / cost, the deterministic verification result and command,
  work-mode gate verdicts and bounce count, and totals. The receipt is a
  projection of existing durable task state (no second database), works for
  running, completed, failed, and canceled tasks, and survives daemon restarts.
- **`/tasks/<id>/receipt` HTTP endpoint** — the daemon serves the same receipt
  over its API; the web console shows it inline on each task's detail pane
  (implementation → verification → review/fix chain with durations, costs, and
  the final result).
- **`maestro doctor`** (and `--json`) — fast, deterministic, strictly read-only
  environment diagnostics: Maestro version, Python, system, state directory
  (existence/writability/config paths/storage backend), daemon reachability
  (env URL or liveness-checked marker, auth status), git, every known agent CLI
  (found/version/status) plus registered agents, the target workspace (git
  root, project type, detected verification command), and budget caps with
  current spend. Missing optional agents and a missing daemon are reported but
  never fail doctor; it exits non-zero only on a genuinely blocking problem
  (unusable state directory or invalid configuration).
- **Packaging** — the project builds a wheel and an sdist (`python -m build`)
  containing the Python package, the prebuilt web console assets
  (`maestro/web_dist/`), and full metadata; `pip install .` works in a clean
  environment without editable mode. `MANIFEST.in` includes LICENSE, README,
  CHANGELOG, and the web assets in the sdist.
- **Apache-2.0 license** — `LICENSE` added; packaging declares it (PEP 639).
- **Examples** — `examples/` now contains ready-to-adapt handoff files:
  `basic.toml`, `fallback.toml`, `work-mode-production.toml`, `context.toml`,
  plus a README explaining each.
- **Smoke script** — `scripts/smoke-fake-agent.sh` proves the full loop
  (delegate → attempt → output → verification → receipt → durable reload) with
  a deterministic fake agent, with no dependency on any real coding-agent CLI.

### Improved

- **Cost capture** — adapter-reported usage under either `cost_usd` or
  `total_cost_usd` now counts toward budgets and receipts (previously only the
  `total_cost_usd` spelling was seen).
- **Robustness without git** — project-root resolution degrades gracefully when
  the `git` binary is unavailable instead of failing task setup.
- **README** — rewritten around the durable-execution positioning: why Maestro,
  a task-flow diagram, an execution-receipt example, and a quickstart that
  runs install → doctor → discover → daemon → delegate → watch → receipt.
- **Documentation** — CLI reference now documents `doctor` and `task receipt`;
  version references updated to 0.9.0; a new concepts page explains how Tasks,
  Attempts, Agents, and Execution Receipts relate.

### Documentation

- New: `docs/usage/explanation/concepts.md` (core object model),
  `examples/README.md`.
- Updated: README (positioning, quickstart, CLI table, license section),
  `docs/usage/reference/cli.md`, `docs/usage/tutorials/first-delegation.md`,
  `docs/usage/reference/configuration.md`.

### Release readiness

- **CI** — GitHub Actions matrix (Python 3.11/3.12/3.13) runs the full suite
  with the 100% coverage gate, builds the wheel + sdist, validates a clean
  install of both artifacts in fresh virtualenvs (`scripts/validate-package.sh`),
  runs the fake-agent smoke test, and rebuilds the web bundle to prove the
  checked-in `maestro/web_dist/` is in sync. A release workflow on `v*` tags
  re-runs all gates, enforces tag == pyproject == `maestro.VERSION`, and
  publishes artifacts (wheel + sdist) to GitHub Releases — never to PyPI (the
  name is taken by an unrelated project).
- **Clean-install verification** — `scripts/validate-package.sh` builds both
  artifacts and installs each into a throwaway venv, checking `maestro
  --version/--help/doctor`, the `maestro-mcp`/`maestro-daemon` entry points,
  and the packaged web assets. Packaging tests in `tests/test_packaging.py`
  pin version, license, metadata, manifest, and asset presence; a marker test
  in `tests/test_console_js.py` catches stale console bundles even without node.
- **Flagship demo** — `scripts/demo-v0.9.sh`: deterministic fake agents drive a
  full work-mode cycle (implement → failing verification gate → auto-fix bounce
  → re-verification) and prove the receipt survives daemon shutdown; the
  90-second storyboard is `docs/demo-v0.9.md`. `examples/overnight.toml`
  documents multi-task batches (separate workspaces, inspect via receipts — no
  scheduler). A 90-second terminal recording of a real demo run
  (`docs/assets/demo-v0.9.gif`, produced with VHS) is embedded in the README.
- **Contributor & security docs** — `CONTRIBUTING.md` (setup, the test bar,
  design principles) and `SECURITY.md` (reporting path + threat model: Maestro
  executes external processes with your privileges; loopback-only daemon by
  default, token required for any non-loopback bind).
- **Real-agent validation** — `docs/release/real-agent-validation.md`: the
  manual, labeled procedure for validating against real agent CLIs (deliberately
  outside CI); screenshot capture instructions in `docs/assets/README.md`.
- **Receipt readability** — the human receipt now shows a single scannable line
  per attempt (phase / agent / duration / cost / ✓-✗) and a `Runs` summary that
  disambiguates total executions from the attempts list
  (`6 (4 agent · 2 verification)`). The JSON shape is unchanged.
- **Coverage gate** — `fail_under = 100` now lives in `.coveragerc` itself, so
  a plain `coverage report` enforces the gate as well as CI's explicit flag.

### Fixed

- **Work-mode presets over the wire** — `explicit_target` (whether the target
  agent was named by the user or is just a default) was dropped when handoffs
  crossed the JSON-RPC boundary, so `maestro delegate --mode <preset>` without
  `--target` ran the default agent instead of the preset's implementer. The flag
  is now serialized with the document; legacy persisted records keep the old
  heuristic.

## [0.8.x] — prior milestones

Highlights from the 0.8 line (see git history for the full record):

- **Context injection** — typed, user-controlled context entries (text / file /
  Agent Skills) composed from config and handoffs, staged into agent turns and
  recorded on the task (`task audit` shows exactly what each turn received).
- **Work modes** — named presets pinning agents to the implement / verify /
  review / fix phases with bounded bounces; deterministic verification always
  runs first and an LLM verdict can only add failures.
- **Cross-machine daemons** — `--bind`, token auth, host-aware P2P discovery,
  remote agent registration over the A2A protocol (`a2a_remote`).
- **MCP server + web console** — `maestro-mcp` for Claude Code supervision;
  React console served by the daemon with live SSE output.
- **Budget caps** — per-agent and daily USD caps enforced at launch time.
- **Durable user-level state** — claim journal + registry under `~/.maestro`;
  tasks survive worktree changes and daemon restarts.
