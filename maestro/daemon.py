"""The Maestro daemon: local broker with an event-driven core.

Owns the agent registry, the task lifecycle (A2A-aligned states), the per-workspace
queue (one active task per workspace), retry/failover chains, per-task branches,
preflight checks, cancellation, and the A2A HTTP surface (Agent Card + JSON-RPC +
SSE). Everything is event-driven: consumers wait on the bus, nothing polls.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import stat
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .a2a import (
    ERR_INTERNAL,
    A2ADispatcher,
    STATE_CANCELED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_INPUT_REQUIRED,
    STATE_SUBMITTED,
    STATE_WORKING,
    TERMINAL_STATES,
    agent_card,
    sse_encode,
)
from . import daemonctl, worktrees
from .daemon_client import DaemonClient, unanswered_message
from .adapters import AdapterNotAvailable, BaseAdapter, make_adapter
from .agents import BUILTIN_ADAPTERS, AgentRegistry, AgentSpec, _write_private
from .branches import branch_exists, branch_name_clash, find_renamed_branches, rename_task_branch
from .context import RenderedContext, check_skill_entries, compose_context, entry_from_dict, render_context
from .core import Maestro, maestro_user_dir
from .events import EventBus, TaskEvent, utcnow_iso
from .handoff import HandoffDoc
from .knowledge import TaskKnowledge, continuation_budget_chars, estimate_tokens, project_knowledge, render_continuation_block
from .models import Phase
from .modes import DEFAULT_MAX_BOUNCES, expand as expand_mode, resolve_mode
from .worker import _has_python_test_suite, _verification_command

_TASK_ID_RE = re.compile(r"^task-\d{8}-\d{6}-[0-9a-f]{6}$")


def _new_task_id() -> str:
    return f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


_PHASE_BY_STATE = {
    STATE_SUBMITTED: Phase.DESIGNED,
    STATE_WORKING: Phase.IMPLEMENTING,
    STATE_INPUT_REQUIRED: Phase.REVIEWING,
    STATE_COMPLETED: Phase.REVIEWING,  # work done; awaiting cross-review (0.8.x semantics)
    STATE_FAILED: Phase.FAILED,
    STATE_CANCELED: Phase.FAILED,
}

_STATE_BY_PHASE = {
    Phase.DESIGNED.value: STATE_SUBMITTED,
    Phase.IMPLEMENTING.value: STATE_WORKING,
    Phase.VERIFYING.value: STATE_WORKING,
    Phase.REVIEWING.value: STATE_COMPLETED,
    Phase.FAILED.value: STATE_FAILED,
}

_ALL_STATES = (STATE_SUBMITTED, STATE_WORKING, STATE_INPUT_REQUIRED, STATE_COMPLETED, STATE_FAILED, STATE_CANCELED)


def _duration_text(seconds: int) -> str:
    """A time limit in words: "30 minutes", "1 second", "90 seconds"."""
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


class _VerificationRun:
    """What a verification command did: its exit code, output, and whether
    the time limit stopped it."""

    def __init__(self, returncode: int, stdout: str, stderr: str, timed_out: bool = False) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


def _run_verification_command(command: list[str], cwd: Path, timeout_s: int | None) -> _VerificationRun:
    """Run the project's verification command, at most ``timeout_s`` seconds.

    The command runs in its own process group, so when the time limit is
    reached every process it started (test workers, a server it launched) is
    stopped too, not only the first one."""
    from .adapters.base import _kill_group

    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        )
    except (OSError, ValueError) as exc:  # command not launchable: treat as a failed verification
        return _VerificationRun(127, "", f"verification command could not be launched: {exc}")
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        stdout, stderr = process.communicate()
        return _VerificationRun(process.returncode if process.returncode is not None else -9, stdout or "", stderr or "", timed_out=True)
    return _VerificationRun(process.returncode, stdout, stderr)


def _durable_state(claims: dict[str, str], runtime: dict[str, Any]) -> str:
    """Resolve the state of a task that has no live in-memory record.

    The phase claim is lossy (input-required and completed both map to
    REVIEWING), so the runtime snapshot's own state — written on every
    transition by _persist — wins when it names a known A2A state. Only then
    does the phase mapping apply; a missing/unknown status means the task's
    process is gone and it can never finish on its own, so that defaults to
    FAILED rather than a misleading WORKING.
    """
    raw = str(runtime.get("state") or "")
    if raw in _ALL_STATES:
        return raw
    return _STATE_BY_PHASE.get(str(claims.get("task_status")), STATE_FAILED)


def _runtime_from_claims(claims: dict[str, str]) -> dict[str, Any]:
    """Parse the task_runtime claim (the last persisted record snapshot)."""
    raw_runtime = claims.get("task_runtime")
    if isinstance(raw_runtime, str):
        try:
            parsed = json.loads(raw_runtime)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}


def _forwarded_handoff(doc: HandoffDoc) -> dict[str, Any]:
    """The handoff as sent to a remote daemon, without the branch name.

    The branch name belongs to this daemon's workspace, where this daemon
    creates the branch on the first turn and checks it out on later turns. A
    remote daemon gets a fresh ``message/send`` on every attempt and every
    turn, so a branch name in it would make the remote refuse every send after
    the first ("already exists"), and on the same machine even the first. The
    remote daemon puts its work on its own default branch instead.
    """
    data = doc.to_dict()
    data["expectations"]["branch"] = None
    return data


def _commit_rule(doc: HandoffDoc) -> str:
    """The prompt line that says whether the agent may commit.

    Maestro never commits. By default the agent must not either: its changes
    stay uncommitted so the supervisor can review them and commit. A handoff
    can allow commits with [expectations] agent_may_commit = true."""
    if doc.agent_may_commit:
        return ("COMMITS: You may commit your work to the branch that is checked out. "
                "Do not push, and do not create, switch or delete branches.")
    return ("COMMITS: Do not commit, and do not create, switch or delete branches. "
            "Leave your changes uncommitted in the working tree; the supervisor reviews them and commits them.")


def build_prompt(doc: HandoffDoc, task_id: str, workspace: Path, transcript: list[dict[str, str]], context_block: str = "") -> str:
    design = doc.design or "(none — use your judgment within the request's scope)"
    context_files = ", ".join(doc.context_files) if doc.context_files else "(none)"
    qa = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in transcript) or "(first turn)"
    # Rendered [[context]] entries (see maestro/context.py); empty keeps the
    # prompt byte-identical to the pre-feature shape (invariant C6).
    context_section = f"\n\n{context_block}" if context_block else ""
    return f"""You are the implementation agent for a Maestro multi-agent task.

Task ID: {task_id}
Workspace: {workspace} (work only inside this directory)

TITLE: {doc.title}

REQUEST:
{doc.request}

AUTHORITATIVE DESIGN:
{design}{context_section}

CONTEXT NOTES:
{doc.context_notes or "(none)"}
CONTEXT FILES: {context_files}

EXPECTATIONS: artifacts: {", ".join(doc.artifacts)}; verification: {doc.verification}; commit policy: {doc.commit_policy}
{_commit_rule(doc)}

EXECUTION MODE: this is a non-interactive batch run — nobody can answer questions or grant approvals while you work. Do not stop to ask for approval or confirmation; make reasonable decisions within the request's scope, complete the work in this run, and list any open questions in your final report so they can be answered on a follow-up turn.

PREVIOUS Q&A (if any):
{qa}

Report back with: files changed, commands run and their results, deviations from the design, and remaining issues.
"""

# Work-mode gate helpers (see docs/design-work-modes.md). Gate turns are read-only
# LLM passes that end with a machine-readable verdict line; the deterministic
# verification result can never be overridden by an LLM verdict (invariant I1).

_GATE_TEXT_LIMIT = 20_000


def _cap_text(text: str, limit: int = _GATE_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def parse_verdict(text: str) -> dict[str, Any] | None:
    """Parse the trailing verdict protocol from a gate turn's output.

    Finds the last ``VERDICT: PASS|FAIL`` line (case-insensitive). On FAIL it
    collects bullet lines ("-" or "*"): only after an ``ISSUES:`` marker when one
    is present, otherwise anywhere after the verdict line. Returns None when no
    verdict line exists — callers must park on that, never treat it as a pass.
    """
    matches = list(re.finditer(r"^VERDICT:\s*(PASS|FAIL)\b", text, re.IGNORECASE | re.MULTILINE))
    if not matches:
        return None
    match = matches[-1]
    ok = match.group(1).upper() == "PASS"
    issues: list[str] = []
    if not ok:
        tail = text[match.start():].splitlines()
        has_marker = any(re.match(r"^\s*ISSUES\s*:?", line, re.IGNORECASE) for line in tail)
        collecting = not has_marker
        for line in tail[1:]:
            stripped = line.strip()
            if re.match(r"^ISSUES\s*:?", stripped, re.IGNORECASE):
                collecting = True
                continue
            if collecting and stripped.startswith(("-", "*")):
                issues.append(stripped.lstrip("-*").strip())
    return {"ok": ok, "issues": issues}


def _gate_context(doc: HandoffDoc, task_id: str, workspace: Path, context_block: str = "") -> str:
    design = doc.design or "(none — use your judgment within the request's scope)"
    section = f"\n\n{context_block}" if context_block else ""
    return (
        f"Task ID: {task_id}\nWorkspace: {workspace}\n\nTITLE: {doc.title}\n\nREQUEST:\n{doc.request}\n\n"
        f"AUTHORITATIVE DESIGN:\n{design}{section}\n"
    )


def _verification_excerpt(report_path: Path) -> str:
    if report_path.is_file():
        try:
            return _cap_text(report_path.read_text(encoding="utf-8"))
        except OSError:
            return "(verification report unreadable)"
    return "(none — deterministic verification was not run)"


def _diff_excerpt(workspace: Path) -> str:
    diff = subprocess.run(["git", "diff", "HEAD"], cwd=workspace, text=True, capture_output=True)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=workspace, text=True, capture_output=True)
    parts = [f"git diff HEAD:\n{diff.stdout}" if diff.stdout.strip() else "git diff HEAD: (no changes to tracked files)"]
    if untracked.stdout.strip():
        parts.append("Untracked files:\n" + untracked.stdout)
    return _cap_text("\n\n".join(parts))


def build_verify_prompt(doc: HandoffDoc, task_id: str, workspace: Path, verification_ok: bool | None, report_path: Path, context_block: str = "") -> str:
    status = "PASSED" if verification_ok else ("FAILED" if verification_ok is False else "skipped (verification disabled for this task)")
    triage = (
        "The deterministic check FAILED. First determine whether the failure is caused by this change; "
        "pre-existing or unrelated failures must be reported as such, not fixed by you.\n\n"
        if verification_ok is False else ""
    )
    return f"""You are the VERIFIER for a Maestro multi-agent task.

{_gate_context(doc, task_id, workspace, context_block)}DETERMINISTIC VERIFICATION: {status}

{_verification_excerpt(report_path)}

CURRENT DIFF:
{_diff_excerpt(workspace)}

Your job: does the change actually work and meet the request? You may inspect files and re-run targeted tests, but you must not modify any file.
{triage}End your reply with exactly one verdict line:
VERDICT: PASS   or   VERDICT: FAIL
On FAIL, follow it with an ISSUES: section listing one bullet per issue (each line starting with "- ")."""


def build_review_prompt(doc: HandoffDoc, task_id: str, workspace: Path, verification_ok: bool | None, report_path: Path, prior_issues: list[str], context_block: str = "") -> str:
    status = "PASSED" if verification_ok else ("FAILED" if verification_ok is False else "skipped (verification disabled for this task)")
    findings = "\n".join(f"- {issue}" for issue in prior_issues) if prior_issues else "(none)"
    return f"""You are the REVIEWER for a Maestro multi-agent task.

{_gate_context(doc, task_id, workspace, context_block)}DETERMINISTIC VERIFICATION: {status}

{_verification_excerpt(report_path)}

CURRENT DIFF:
{_diff_excerpt(workspace)}

VERIFIER FINDINGS (from an earlier verification pass):
{findings}

Your job: should we accept this code? Assess quality, conformance to the design, and scope creep against the request. You may inspect files, but you must not modify any file.
End your reply with exactly one verdict line:
VERDICT: PASS   or   VERDICT: FAIL
On FAIL, follow it with an ISSUES: section listing one bullet per issue (each line starting with "- ")."""


def build_fix_prompt(doc: HandoffDoc, task_id: str, workspace: Path, issues: list[str], det_failed: bool, context_block: str = "") -> str:
    issue_text = "\n".join(f"- {issue}" for issue in issues) or "(deterministic verification failed; see the verification report)"
    return f"""You are the FIX agent for a Maestro multi-agent task.

{_gate_context(doc, task_id, workspace, context_block)}The previous implementation work for this task is already on the task branch/working tree; build on it rather than redoing it.

UNRESOLVED ISSUES FROM VERIFICATION AND REVIEW:
{issue_text}

Your job: fix exactly these issues with the smallest change that resolves them, and do not restructure beyond what the issues require.
{_commit_rule(doc)}
Report back with: files changed, commands run and their results, and how each issue was resolved."""


class MaestroDaemon:
    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        start_http: bool = True,
        port: int = 0,
        bind: str = "127.0.0.1",
        max_retries: int | None = None,
        backoff_s: float | None = None,
        allowed_origins: list[str] | None = None,
    ) -> None:
        # Browser origins, besides the daemon's own, that may POST to it (a
        # reverse proxy's public address). None reads MAESTRO_DAEMON_ALLOWED_ORIGINS.
        self.allowed_origins = frozenset(_normalize_origin(o) for o in (_allowed_origins_from_env() if allowed_origins is None else allowed_origins))
        self.state_dir = Path(state_dir).expanduser() if state_dir else maestro_user_dir()
        # Pin Maestro's user-level state (claims/registry) to this daemon's state
        # directory so the daemon is the single source of truth for its state.
        os.environ["MAESTRO_HOME"] = str(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.maestro = Maestro(self.state_dir)
        self.registry = AgentRegistry(self.state_dir)
        self.bus = EventBus()
        self.max_retries = int(os.environ.get("MAESTRO_MAX_RETRIES", max_retries if max_retries is not None else 2))
        self.backoff_s = float(os.environ.get("MAESTRO_BACKOFF_S", backoff_s if backoff_s is not None else 1.0))
        self._tasks: dict[str, dict[str, Any]] = {}
        self._active: dict[str, str] = {}  # workspace key -> task_id
        self._queue: list[tuple[HandoffDoc, Path]] = []
        self._cancel_flags: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        # Tasks whose next turn has been started by answer_question but whose
        # thread has not yet set the state to working. rename_branch treats
        # them as running.
        self._turn_starting: set[str] = set()
        self.port: int | None = None
        self.bind = bind or "127.0.0.1"
        self.token: str | None = None  # set in _resolve_bind when auth is required
        self.advertised_host: str = "127.0.0.1"  # host peers should use to reach us
        self.local_host: str = "127.0.0.1"  # host the local CLI dials (daemon.json marker)
        self._httpd: ThreadingHTTPServer | None = None
        self._presence: Any | None = None
        self._stopped = False
        self.sse_heartbeat_s = 15.0  # keepalive interval for /tasks/<id>/events streams
        self.sse_queue_max = 2048  # events one SSE client may fall behind before it is disconnected
        self.sse_write_timeout_s = 30.0  # an SSE client that accepts no data for this long is disconnected
        self._owner_lock_fd: int | None = None  # set while this daemon serves HTTP and owns daemon.json
        # The process that runs this daemon's tasks, written into every task's
        # runtime record. The start time tells this process apart from a later
        # one that reuses its pid, so reconciliation can tell whether the
        # runner of a leftover "working" task is still alive.
        self._runner: dict[str, Any] = {"pid": os.getpid(), "started": daemonctl.process_start_token(os.getpid())}
        # Every daemon process that uses this state directory holds a shared
        # lock on it. Only a process that can briefly take that lock
        # exclusively is alone here; see _reconcile_interrupted_tasks for how
        # that decides which leftover tasks are failed.
        self._users_lock_fd = daemonctl.open_lock(self.state_dir / daemonctl.USERS_LOCK_NAME)
        try:
            alone = daemonctl.try_exclusive(self._users_lock_fd)
            # A daemon from an older version holds no lock, so its answering marker is checked too.
            owner = daemonctl.live_owner(self.state_dir)
            if start_http and owner is not None:
                # This daemon is about to be refused (start_http raises the same
                # error), so it must not fail any task on its way out.
                raise daemonctl.DaemonAlreadyRunning(
                    f"another Maestro daemon (pid {owner.pid}, {owner.url}) already owns the state directory {self.state_dir}"
                )
            self._reconcile_interrupted_tasks(alone=alone and owner is None)
            daemonctl.hold_shared(self._users_lock_fd)
        except BaseException:
            # Closing the descriptor drops the lock, whichever mode it was in,
            # so a failed start never blocks the next daemon.
            os.close(self._users_lock_fd)
            self.maestro.close()
            raise
        if start_http:
            try:
                self.start_http(port)
            except BaseException:
                self._release_state_dir()
                self.maestro.close()
                raise

    # ------------------------------------------------------------------ http
    def _resolve_bind(self) -> None:
        """Compute advertised/local host and auth token for ``self.bind``.

        Pure (no sockets) so it is testable for any bind address:
        - loopback binds need no token and stay reachable at 127.0.0.1;
        - "0.0.0.0"/"::" means all interfaces — advertise the primary LAN IP,
          keep the local marker on loopback;
        - an explicit non-loopback IP is advertised as-is and used locally too.
        A token is required for every non-loopback bind: an explicit
        MAESTRO_DAEMON_TOKEN wins (stable across restarts), otherwise one is
        generated and recorded in daemon.json so the local CLI keeps working.
        """
        import secrets

        if self.bind in ("0.0.0.0", "::"):
            from .discovery import pick_lan_ip

            self.advertised_host = pick_lan_ip()
            self.local_host = "127.0.0.1"
        elif self.bind not in ("127.0.0.1", "::1"):
            self.advertised_host = self.bind
            self.local_host = self.bind
        if self.bind not in ("127.0.0.1", "::1"):
            env_token = os.environ.get("MAESTRO_DAEMON_TOKEN", "").strip()
            self.token = env_token or secrets.token_urlsafe(24)

    def start_http(self, port: int = 0) -> int:
        """Serve HTTP and take ownership of the state directory's daemon.json marker.

        Only one daemon may own a state directory. When a live daemon already
        owns it, this raises :class:`daemonctl.DaemonAlreadyRunning` and leaves
        that daemon's marker alone.
        """
        if self._httpd is not None:
            return self.port or 0
        owner = daemonctl.live_owner(self.state_dir)
        if owner is not None:
            raise daemonctl.DaemonAlreadyRunning(
                f"another Maestro daemon (pid {owner.pid}, {owner.url}) already owns the state directory {self.state_dir}"
            )
        lock_fd = daemonctl.acquire_owner_lock(self.state_dir)
        if lock_fd is None:  # another daemon took ownership after the check above
            raise daemonctl.DaemonAlreadyRunning(
                f"another Maestro daemon (pid {daemonctl.owner_lock_holder(self.state_dir)}) already owns the state directory {self.state_dir}"
            )
        self._resolve_bind()
        handler = _make_handler(self)
        try:
            self._httpd = ThreadingHTTPServer((self.bind, port), handler)
        except BaseException:
            daemonctl.release_owner_lock(lock_fd)
            raise
        self._owner_lock_fd = lock_fd
        self.port = self._httpd.server_address[1]
        marker: dict[str, Any] = {"pid": os.getpid(), "port": self.port, "host": self.local_host, "started_at": utcnow_iso(), "owner_lock": True}
        if self.token is not None:
            marker["token"] = self.token
        _write_private(self.state_dir / "daemon.json", json.dumps(marker, indent=2))
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        # P2P presence: announce over UDP so other Maestro nodes find us. A
        # daemon that listens on loopback only cannot be reached by another
        # machine, so it announces only when MAESTRO_DISCOVERY is set to on.
        from .discovery import PeerTable, PresenceServer, discovery_enabled, discovery_interface_from_env, discovery_ttl_from_env

        if discovery_enabled(loopback_only=self.bind in ("127.0.0.1", "::1")):
            presence = PresenceServer(
                self.port,
                PeerTable(self.state_dir / "peers.json"),
                name=os.environ.get("MAESTRO_NODE_NAME", "maestro-node"),
                multicast_if=discovery_interface_from_env(),
                ttl=discovery_ttl_from_env(),
                http_host=self.advertised_host,
            )
            if presence.start():
                self._presence = presence
        return self.port

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Mark in-flight turns as failed before shutting down: once this process
        # exits, no thread remains to drive them to a terminal state, and their
        # durable status must not keep claiming "working". Parked (input-required)
        # tasks are left for the next daemon; already-terminal ones need nothing.
        for task_id, record in list(self._tasks.items()):
            if record.get("state") in TERMINAL_STATES or record.get("state") == STATE_INPUT_REQUIRED:
                continue
            try:
                self._set_state(task_id, STATE_FAILED, error="daemon stopped while the task was running; re-delegate, or continue this task to resume.")
            except Exception:
                traceback.print_exc(file=sys.stderr)
        if self._presence is not None:
            self._presence.stop()
            self._presence = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        marker = self.state_dir / "daemon.json"
        # Only the daemon that wrote the marker removes it: a daemon running
        # without HTTP beside the owner must leave the owner's marker in place.
        if self._owner_lock_fd is not None and marker.exists():
            try:
                marker.unlink()
            except OSError:
                pass
        self._release_state_dir()
        self.maestro.close()

    def _release_state_dir(self) -> None:
        """Drop this daemon's locks on the state directory (owner lock first)."""
        if self._owner_lock_fd is not None:
            daemonctl.release_owner_lock(self._owner_lock_fd)
            self._owner_lock_fd = None
        os.close(self._users_lock_fd)

    def _reconcile_interrupted_tasks(self, *, alone: bool = True) -> None:
        """Fail tasks that a previous daemon process left mid-flight.

        A turn is an in-process thread. When the daemon dies (SIGKILL, OOM,
        reboot, crash) or stops while a task is working — or while it was still
        queued — no thread remains to drive it to a terminal state, and its
        durable status stays IMPLEMENTING/VERIFYING/FIXING forever: every later
        query would report "working" for work that can never finish on its own.
        At startup we mark exactly those tasks FAILED with an explanatory error
        (they stay continuable via followup/task continue). Parked
        input-required tasks are left alone — they are legitimately waiting for
        an answer, not interrupted.

        When this daemon is ``alone`` (the default), no other daemon process
        uses the state directory, so nothing else can be running a leftover
        task, and every one is failed, whatever its runner record says. (A
        container that restarts runs the new daemon under the old pid again, so
        a live pid in the record proves nothing then.) When other daemon
        processes share the directory, the task's runner decides: the pid and
        start time of the daemon process that ran it, recorded in its runtime
        record. The task is failed only when that process is certainly gone. It
        is left alone when the process still runs, when its start time cannot
        be read now (the process may still run), and when no runner was
        recorded (a record from an older version).
        """
        try:
            records = self.maestro._registry_records()
        except Exception:
            return  # unreadable registry: never block daemon startup on reconciliation
        for item in records:
            task_id = str(item.get("task_id") or "")
            if not task_id or task_id in self._tasks:
                continue
            try:
                claims = self.maestro._claims(task_id)
            except Exception:
                continue
            runtime = _runtime_from_claims(claims)
            state = _durable_state(claims, runtime)
            if state == STATE_WORKING:
                reason = (
                    "daemon stopped or crashed while the task was running; its turn was interrupted. "
                    "Re-delegate, or continue this task to resume."
                )
            elif state == STATE_SUBMITTED or (not claims.get("task_status") and not runtime.get("state")):
                # Registered but never started (queued behind a workspace that
                # was busy when the previous process died — the queue is
                # in-memory) or died before its first state was persisted.
                reason = "daemon restarted before the task started; re-delegate to run it."
            else:
                continue  # completed/failed/canceled or parked input-required: leave as-is
            if not alone:
                runner = runtime.get("runner")
                pid = runner.get("pid") if isinstance(runner, dict) else None
                if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
                    continue  # no usable runner recorded: it may belong to another live daemon
                if daemonctl.runner_state(runner) != "gone":
                    continue  # its daemon process still runs it, or may
            try:
                record = self._durable_record(task_id)
                if record is None:
                    continue
                record["error"] = reason
                self._set_state(task_id, STATE_FAILED, error=reason)
            except Exception:
                traceback.print_exc(file=sys.stderr)

    # ------------------------------------------------------------- delegation
    def _budget_records(self) -> list[dict[str, Any]]:
        """Task records for budget accounting: durable claims, live ones win."""
        from .budgets import _records_from_claims

        records = _records_from_claims(self.maestro.mem.get_all())
        for task_id, record in self._tasks.items():
            snapshot = {k: v for k, v in record.items() if k != "transcript"}
            records[task_id] = snapshot
        return list(records.values())

    def _enforce_budgets(self, target_agent: str) -> None:
        from .budgets import BudgetCaps, check

        caps = BudgetCaps.from_env()
        if not caps.any():
            return
        violation = check(caps, target_agent, self._budget_records())
        if violation is not None:
            raise ValueError(f"Budget cap exceeded — launch blocked ({violation}); running tasks still finish")

    def default_target(self) -> str:
        return "codex"

    def _agent_known(self, name: str) -> bool:
        """True when the name resolves to a registered agent or a builtin adapter kind.

        Names that are not even valid registry names (bad characters) simply
        count as unknown — this is a predicate, not a validator.
        """
        try:
            return self.registry.get(name) is not None or name in BUILTIN_ADAPTERS
        except ValueError:
            return False

    def _config(self, workspace: str | Path | None = None, *, strict: bool = False) -> dict[str, Any]:
        """The config that applies to a workspace, read from disk now.

        That is the user file (<state dir>/config.toml), then the project's
        .maestro/config.toml, then the workspace's own; later files win. Reading
        it on every use means an edited file applies to the next task without a
        daemon restart, and a project's [defaults] apply to that project only.

        With ``strict`` an invalid file raises ValueError (delegation reports
        it). Otherwise the config the daemon started with is used, so a file
        edited while tasks run cannot break scheduling or verification."""
        ws = Path(workspace) if workspace is not None else None
        project_root = Maestro._resolve_project_root(ws) if ws is not None else self.state_dir
        try:
            return Maestro.load_config(self.state_dir, project_root, ws if ws is not None else self.state_dir)
        except ValueError:
            if strict:
                raise
            return self.maestro.config

    def _apply_work_mode(self, doc: HandoffDoc, workspace: str | Path | None = None) -> HandoffDoc:
        """Expand a work-mode preset onto the handoff and validate its routing pins.

        Explicit routing fields win over the preset (see maestro/modes.py). After
        expansion: self-review is rejected (review_agent == implementer), every named
        verifier/reviewer/fixer must be known to this daemon, and a mode-provided
        implementer must be known too. Raises ValueError with an actionable message.
        """
        if doc.mode:
            preset = resolve_mode(self._config(workspace).get("modes") or {}, doc.mode)
            expand_mode(preset, doc)
        if doc.review_agent and doc.review_agent == doc.target_agent:
            raise ValueError(f"review_agent {doc.review_agent!r} cannot equal the implementer (self-review is not a gate)")
        registered = ", ".join(spec.name for spec in self.registry.list()) or "none"
        if doc.mode and not self._agent_known(doc.target_agent):
            raise ValueError(f"Unknown implementer agent {doc.target_agent!r} (from mode {doc.mode!r}). Registered agents: {registered}")
        for role, name in (("verifier", doc.verify_agent), ("reviewer", doc.review_agent), ("fixer", doc.fix_agent)):
            if name and not self._agent_known(name):
                raise ValueError(f"Unknown {role} agent {name!r}. Registered agents: {registered}")
        return doc

    def _apply_context(self, doc: HandoffDoc, workspace: Path) -> HandoffDoc:
        """Compose standing + handoff context entries and validate skill paths.

        The composed list replaces ``doc.context_entries`` so the stored task record
        is self-contained (invariant C2). Skill entries must exist on disk now (C4):
        a missing directory or SKILL.md fails delegation before any agent runs.
        A relative skill path is checked against the task workspace, the same
        directory that ``render_context`` later copies the skill from.
        """
        config_entries = self._config(workspace).get("context") or {}
        composed = compose_context(config_entries, doc.context_entries)
        check_skill_entries(composed, workspace)
        doc.context_entries = [e.to_dict() for e in composed]
        return doc

    # ------------------------------------------------- routing defaults ([defaults])
    def _apply_defaults(self, doc: HandoffDoc, workspace: str | Path | None = None) -> bool:
        """Fill missing routing from the config ``[defaults]`` table.

        Returns True when a target agent is now resolved (the handoff named one
        explicitly, or ``[defaults].agent`` supplied it). Returns False when the
        handoff names no target and no default is configured — the caller must
        ask the user which agent/model to use instead of guessing.

        ``[defaults].fallback`` fills an empty fallback chain, and
        ``[defaults].model``/``effort`` are applied to the chosen agent when
        neither the handoff nor that agent's registry entry sets them. Explicit
        handoff values always win, and a registry value beats the default.
        """
        defaults = self._config(workspace).get("defaults") or {}
        if not doc.explicit_target:
            agent = defaults.get("agent")
            if not agent:
                return False
            registered = ", ".join(spec.name for spec in self.registry.list()) or "none"
            if not self._agent_known(agent):
                raise ValueError(f"Unknown default agent {agent!r} (from [defaults]). Registered agents: {registered}")
            doc.target_agent = str(agent)
            doc.explicit_target = True
        if not doc.fallback and defaults.get("fallback"):
            doc.fallback = list(defaults["fallback"])
        target_spec = self.registry.get(doc.target_agent)
        for key in ("model", "effort"):
            value = defaults.get(key)
            if not value or doc.agent_settings.get(key):
                continue
            if target_spec is not None and getattr(target_spec, key):
                continue  # the agent's own registry value beats the config default
            doc.agent_settings[key] = str(value)
        return True

    def _routing_question(self) -> str:
        """The input-required question asked when no agent/model can be resolved.

        Lists every runnable agent: registered ones (with their configured
        model/version) first, then builtin CLIs discovered on PATH that are not
        registered yet (the daemon runs them via an implicit spec).
        """
        lines = [
            "No default agent/model is configured for this project and the handoff does not name one.",
            "Which agent (and model) should run this task? Available agents:",
        ]
        listed: set[str] = set()
        for spec in self.registry.list():
            status = self.registry.status(spec.name)
            version = status.get("version") if isinstance(status, dict) else None
            detail = f" ({version})" if version else ""
            model_note = f" [model: {spec.model}]" if spec.model else ""
            lines.append(f"  - {spec.name}{detail}{model_note}")
            listed.add(spec.kind)
        for candidate in self.registry.discover():
            if not candidate["found"] or candidate["kind"] in listed:
                continue
            lines.append(f"  - {candidate['name']} (on PATH, not registered)")
            listed.add(candidate["kind"])
        lines.append("Answer with the agent name, optionally a model — e.g. 'codex' or 'agent=codex model=gpt-5.6-luna'.")
        lines.append("To stop being asked, set [defaults] in .maestro/config.toml (keys: agent, fallback, model, effort).")
        return "\n".join(lines)

    def _parse_routing_answer(self, record: dict[str, Any], answer: str) -> HandoffDoc:
        """Resolve a routing question's answer into the task's handoff document.

        Accepted forms: JSON ``{"agent": …, "model": …}``; ``key=value`` pairs
        (``agent=… model=…``); or a bare agent name (first token that matches a
        registered agent). Raises ValueError with guidance when it cannot resolve
        to a known agent — the task stays input-required for another answer.
        """
        from .handoff import from_dict

        doc = from_dict(record["doc"]) if isinstance(record.get("doc"), dict) else self._doc_from_record(record)
        text = str(answer).strip()
        agent: str | None = None
        model: str | None = None
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                if isinstance(payload.get("agent"), str):
                    agent = payload["agent"].strip()
                if isinstance(payload.get("model"), str):
                    model = payload["model"].strip() or None
        except (ValueError, TypeError):
            pass
        if not agent:
            match = re.search(r"(?:^|\s)agent\s*[:=]\s*(\S+)", text)
            if match:
                agent = match.group(1).strip(".,;")
        if not model:
            match = re.search(r"(?:^|\s)model\s*[:=]\s*(\S+)", text)
            if match:
                model = match.group(1).strip(".,;")
        if not agent:
            tokens = [t.strip(".,;") for t in text.split() if t.strip(".,;")]
            for candidate in tokens:
                if self._agent_known(candidate):
                    agent = candidate
                    break
            else:
                # A bare name we do not know: report it as unknown rather than
                # "could not determine", so the user sees exactly what was tried.
                if len(tokens) == 1:
                    agent = tokens[0]
        registered = ", ".join(spec.name for spec in self.registry.list()) or "none"
        if not agent:
            raise ValueError(
                f"Could not determine an agent from answer {answer!r}; "
                f"use e.g. 'codex' or 'agent=codex model=gpt-5.6-luna'. Registered agents: {registered}"
            )
        if not self._agent_known(agent):
            raise ValueError(f"Unknown agent {agent!r}. Registered agents: {registered}")
        doc.target_agent = agent
        doc.explicit_target = True
        if model:
            doc.agent_settings["model"] = model
        return doc

    def delegate(self, doc: HandoffDoc, workspace: str | Path) -> dict[str, Any]:
        from .handoff import validate_handoff

        doc = validate_handoff(doc)
        resolved_ws = Path(workspace).expanduser().resolve()
        self._config(resolved_ws, strict=True)  # an invalid config refuses the delegation, with its reason
        doc = self._apply_work_mode(doc, resolved_ws)
        doc = self._apply_context(doc, resolved_ws)
        routing_resolved = self._apply_defaults(doc, resolved_ws)
        if routing_resolved and doc.target_agent == doc.origin_agent:
            raise ValueError(
                f"Agent {doc.target_agent!r} cannot delegate to itself (no self-review/self-delegation); "
                "pick a different target or add a fallback agent"
            )
        if doc.max_depth_remaining <= 0:
            raise ValueError("Max delegation depth exceeded; refusing to nest further")
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {ws}")
        if doc.commit_policy != "no-commit":
            probe = subprocess.run(["git", "-C", str(ws), "rev-parse", "--show-toplevel"], text=True, capture_output=True)
            if probe.returncode != 0:
                raise ValueError(
                    f"Workspace {ws} is not a git repository but commit_policy={doc.commit_policy!r} requires one; "
                    "run 'git init' there or set commit_policy='no-commit'"
                )
            clash = branch_name_clash(ws, doc.branch) if doc.branch else None
            if clash:
                raise ValueError(
                    f"Cannot use branch {doc.branch!r} in {ws}: {clash}; pick a new name for this task's branch"
                )
        key = str(ws)
        self._enforce_budgets(doc.target_agent)
        task_id, record = self._make_record(doc, key)
        turn_flag = self._cancel_flags[task_id]
        # Register first: once the task is queued, _release may start it at any
        # moment from another thread, and it must already have its claims.
        self._register_and_claims(task_id, doc, key)
        with self._lock:
            # Where the first turn runs: the workspace when it is free, else a
            # worktree of its own, else the queue (docs/design-parallel-tasks.md).
            queued = self._claim_turn(task_id) is None
            if queued:
                self._queue.append(task_id)  # FIFO; starts when a place frees
            # Set under the lock: _release clears it under the same lock.
            record["queued"] = queued
            reason = self._queue_reason(task_id) if queued else None
            run_dir = record.get("run_dir") or key
        self._persist(task_id)
        if queued:
            return {"task_id": task_id, "queued": True, "reason": reason, "run_dir": run_dir, "state": STATE_SUBMITTED, "ts": utcnow_iso()}
        state = self._launch(task_id, doc, ws, turn_flag, routing_resolved)
        return {"task_id": task_id, "queued": False, "run_dir": run_dir, "state": state, "ts": utcnow_iso()}

    def _max_parallel(self, workspace: str | Path | None = None) -> int:
        """How many tasks may run turns at the same time for one workspace."""
        return int((self._config(workspace).get("defaults") or {}).get("max_parallel", 4))

    def _is_running(self, task_id: str) -> bool:
        """True while the task runs a turn or is about to. Parked, finished
        and queued tasks are not running."""
        record = self._tasks[task_id]
        if record.get("queued"):
            return False
        return record.get("state") in (STATE_SUBMITTED, STATE_WORKING) or task_id in self._turn_starting

    def _running_count(self, key: str, *, excluding: str | None = None) -> int:
        """Tasks of one workspace that are running a turn."""
        return sum(
            1 for tid, rec in self._tasks.items()
            if tid != excluding and rec.get("workspace") == key and self._is_running(tid)
        )

    def _worktree_allowed(self, record: dict[str, Any]) -> bool:
        """A task may run in a worktree unless it works in place (no-commit),
        runs on a remote daemon (which never uses the local checkout), or the
        user set max_parallel = 1, which keeps the behaviour from before
        worktrees: a task for a busy workspace waits."""
        if self._max_parallel(record["workspace"]) == 1:
            return False
        doc = record.get("doc") or {}
        if ((doc.get("expectations") or {}).get("commit_policy") or "branch") == "no-commit":
            return False
        target = (doc.get("routing") or {}).get("target_agent")
        spec = self.registry.get(target) if target else None
        return not (spec is not None and spec.kind == "a2a_remote")

    def _claim_turn(self, task_id: str) -> str | None:
        """Decide where the task's next turn runs. The caller holds the lock.

        Returns "workspace" or "worktree", or None when the turn must wait in
        the queue. A task stays in the directory where it first ran: its
        uncommitted work is there. A new task takes the workspace when it is
        free, and otherwise gets a worktree of its own. No turn starts while
        the workspace already has max_parallel running tasks.
        See docs/design-parallel-tasks.md, section 3.
        """
        record = self._tasks[task_id]
        key = record["workspace"]
        if self._running_count(key, excluding=task_id) >= self._max_parallel(key):
            return None
        kind = self._run_dir_kind(record)
        if kind == "worktree":
            return "worktree"
        workspace_free = self._active.get(key) in (None, task_id)
        if kind == "workspace" or workspace_free or not self._worktree_allowed(record):
            if not workspace_free:
                return None
            self._active[key] = task_id
            record["run_dir"], record["run_dir_kind"] = key, "workspace"
            return "workspace"
        # The worktree is a checkout of the whole repository; a workspace in a
        # subdirectory of it runs in the same subdirectory of the worktree.
        root = worktrees.worktree_path(self.state_dir, task_id)
        prefix = worktrees.repo_prefix(Path(key))
        record["run_dir"] = str(root / prefix) if prefix else str(root)
        record["run_dir_kind"] = "worktree"
        return "worktree"

    @staticmethod
    def _run_dir_kind(record: dict[str, Any]) -> str | None:
        """Where the task's turns run, or None before its first turn is
        scheduled. A task from before run directories existed has no kind
        recorded; if it has run, it ran in its workspace."""
        kind = record.get("run_dir_kind")
        if kind is None and (record.get("turn") or record.get("attempts")):
            return "workspace"
        return kind

    def _queue_reason(self, task_id: str) -> str:
        """Why a task waits in the queue, in words a user can act on."""
        record = self._tasks[task_id]
        limit = self._max_parallel(record["workspace"])
        if self._running_count(record["workspace"], excluding=task_id) >= limit:
            return (
                f"the workspace is at its limit of {limit} running tasks; this task starts when one of them "
                "finishes or stops to ask a question"
            )
        if limit == 1:
            return "max_parallel is 1, so tasks for this workspace run one at a time; this task starts when the task using the workspace finishes"
        if not self._worktree_allowed(record):
            return "this task works in place (commit_policy no-commit), and another task is using the workspace; it starts when that task finishes"
        return "this task's work is in the workspace, and another task is using the workspace; it starts when that task finishes"

    def _launch(self, task_id: str, doc: HandoffDoc, ws: Path, turn_flag: threading.Event | None, routing_resolved: bool) -> str:
        """Start a task's first turn once it holds its workspace slot.

        A sensitive workspace parks the task for approval, and a task with no
        resolved agent parks on the routing question; otherwise the turn's
        thread starts. Returns the state the task is now in. Every transition
        is tied to ``turn_flag``, so a task that was canceled in the meantime
        stays canceled and nothing starts (the result is then "canceled").
        """
        question = None if routing_resolved else self._routing_question()
        with self._lock:
            if not self._set_state(task_id, STATE_SUBMITTED, turn_flag=turn_flag):
                return STATE_CANCELED
            if doc.sensitive:
                # Governance: sensitive workspaces pause for approval before any agent runs.
                self._set_state(task_id, STATE_INPUT_REQUIRED, question="Approval required: this task targets a sensitive workspace.", awaiting="approval", turn_flag=turn_flag)
                return STATE_INPUT_REQUIRED
            if question is not None:
                # No explicit target and no [defaults]: ask the user which agent/model
                # to use instead of silently guessing (resume via answer_question).
                self._persist(task_id, doc)
                self._set_state(task_id, STATE_INPUT_REQUIRED, question=question, awaiting="routing", turn_flag=turn_flag)
                return STATE_INPUT_REQUIRED
            thread = threading.Thread(target=self._run_task, args=(task_id, doc, ws, turn_flag), daemon=True)
            thread.start()
            return STATE_SUBMITTED

    def _make_record(self, doc: HandoffDoc, key: str) -> tuple[str, dict[str, Any]]:
        task_id = _new_task_id()
        record = {
            "task_id": task_id,
            "state": STATE_SUBMITTED,
            "origin_agent": doc.origin_agent,
            "target_agent": doc.target_agent,
            "agent": None,
            "workspace": key,
            "branch": None,
            # Where the task's turns run: the workspace itself, or a worktree
            # of its own. Decided when its first turn is scheduled.
            "run_dir": None,
            "run_dir_kind": None,
            "title": doc.title,
            "doc": doc.to_dict(),
            "attempts": [],
            "result": None,
            "usage": None,
            "error": None,
            "started_at": utcnow_iso(),
            "transcript": [],
        }
        with self._lock:
            self._tasks[task_id] = record
            self._cancel_flags[task_id] = threading.Event()
        return task_id, record

    def _register_and_claims(self, task_id: str, doc: HandoffDoc, key: str) -> None:
        # The registry record's project_root is the TASK workspace's project
        # root (not the daemon's own cwd), so scoped listings
        # (`task list --workspace/--project`) match tasks by their real project.
        with self.maestro._task_lock():
            number = self.maestro._new_task_number()
            self.maestro._register_task(task_id, doc.title, number, project_root=str(Maestro._resolve_project_root(Path(key))))
        self.maestro._write_claim(task_id, "task_title", doc.title)
        self.maestro._write_claim(task_id, "task_workspace", key)
        self.maestro._write_claim(task_id, "task_number", str(number))
        self.maestro._write_claim(task_id, "task_request", doc.request)
        self._persist(task_id, doc)

    def _start_queued(self, task_id: str) -> None:
        """Start a queued handoff whose turn _pump_queue has just claimed."""
        from .handoff import from_dict

        record = self._tasks.get(task_id)
        if record is None:
            return
        # The whole promotion runs under the lock, so a cancel lands either
        # before it (and is seen here) or after the turn has its flag (and the
        # turn stops at its first step). cancel() takes the same lock.
        with self._lock:
            key = record["workspace"]
            turn_flag = self._cancel_flags.get(task_id)
            if record.get("state") == STATE_CANCELED or (turn_flag is not None and turn_flag.is_set()):
                # Canceled after _release took it off the queue. cancel() saw
                # queued=False, so it already freed the slot and started the
                # next queued task; this task must not start.
                record.pop("continuation", None)
                return
            if record.get("run_dir_kind") != "worktree" and self._active.get(key) != task_id:
                return  # its claim on the workspace was lost: leave it untouched
            record["queued"] = False
            doc = from_dict(record["doc"])
            ws = Path(key)
            if record.pop("continuation", False):
                # A follow-up or answer that waited for the workspace: its approval
                # and routing were settled before it queued, so it just runs.
                thread = threading.Thread(target=self._run_task, args=(task_id, doc, ws, turn_flag), daemon=True)
                thread.start()
                return
            self._launch(task_id, doc, ws, turn_flag, self._apply_defaults(doc, ws))

    def _persist(self, task_id: str, doc: HandoffDoc | None = None) -> None:
        record = self._tasks.get(task_id)
        if record is None:
            return
        snapshot = {k: v for k, v in record.items() if k != "transcript"}
        snapshot["runner"] = self._runner
        self.maestro._write_claim(task_id, "task_runtime", json.dumps(snapshot, ensure_ascii=False))
        if doc is not None:
            self.maestro._write_claim(task_id, "task_origin_agent", doc.origin_agent)
            self.maestro._write_claim(task_id, "task_target_agent", doc.target_agent)

    def _set_state(self, task_id: str, state: str, *, turn_flag: threading.Event | None = None, awaiting: str | None = None, **data: Any) -> bool:
        """Move the task to ``state`` and publish the change.

        Transitions happen under the daemon lock, so a cancel (which checks
        and sets the state under the same lock) and a turn ending can never
        both win. ``turn_flag`` is the cancel flag of the turn making the
        change: when it is given and is no longer the task's current flag, or
        has been set by a cancel, the change is refused. ``awaiting`` records
        what an input-required task waits for ("routing", "approval",
        "question" or "gate"); leaving input-required clears it and the
        question. Returns False when the transition was refused.
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if turn_flag is not None and (self._cancel_flags.get(task_id) is not turn_flag or turn_flag.is_set()):
                # The turn was canceled or a newer turn replaced it.
                return False
            if record is not None and record.get("state") == STATE_CANCELED and state not in (STATE_CANCELED, STATE_SUBMITTED):
                # A turn that was canceled may still be finishing in its thread.
                # Nothing it reports may bring the task back; only a new turn
                # (follow-up), which starts at SUBMITTED, moves it on.
                return False
            if record is not None:
                record["state"] = state
                if state != STATE_INPUT_REQUIRED:
                    # What the task was waiting for, and the question it was
                    # asked, belong to the park that just ended.
                    record.pop("awaiting", None)
                    record.pop("question", None)
                elif awaiting is not None:
                    record["awaiting"] = awaiting
                for key, value in data.items():
                    if value is not None:
                        record[key] = value
            phase = _PHASE_BY_STATE.get(state)
            if phase is not None:
                self.maestro._write_claim(task_id, "task_status", phase.value)
            self._persist(task_id)
            if state in TERMINAL_STATES:
                # Every terminal transition refreshes the durable task-knowledge
                # projection (deterministic; re-derived from claims + git state).
                # The projection is derived data: a failure here must never abort
                # the transition itself, or the task would be durably terminal in
                # the record/claims but its terminal event would never be published.
                try:
                    self._refresh_knowledge(task_id)
                except Exception:
                    print(f"[maestro] task {task_id}: knowledge refresh failed after {state}:", file=sys.stderr, flush=True)
                    traceback.print_exc(file=sys.stderr)
            self.bus.publish(TaskEvent(task_id=task_id, type="state", data={"state": state, **{k: v for k, v in data.items() if v is not None}}))
            if state == STATE_INPUT_REQUIRED:
                # A parked task no longer counts as running, so a queued task
                # may be able to start now.
                self._pump_queue()
            return True

    def _flag_for(self, task_id: str, turn_flag: threading.Event | None) -> threading.Event | None:
        """The turn's cancel flag: the one passed in, else the task's current one."""
        return turn_flag if turn_flag is not None else self._cancel_flags.get(task_id)

    def _turn_live(self, task_id: str, turn_flag: threading.Event | None) -> bool:
        """True while the turn owning ``turn_flag`` may still act for the task.

        Each turn has its own cancel flag. A cancel sets it, and a follow-up
        or answer installs a new one, so a turn that was canceled or replaced
        sees that here and must not change state, free the workspace or start
        another agent. A task without a flag (a defensive case) is live until
        it is canceled.
        """
        record = self._tasks.get(task_id)
        if record is not None and record.get("state") == STATE_CANCELED:
            return False
        return turn_flag is None or (self._cancel_flags.get(task_id) is turn_flag and not turn_flag.is_set())

    def _new_turn(self, task_id: str) -> threading.Event:
        """Give the task a new turn with its own clear cancel flag.

        Called by a follow-up or an answer, under the lock. The previous
        turn's flag stops being current, so if that turn is still finishing in
        its thread it can no longer act for the task.
        """
        turn_flag = threading.Event()
        with self._lock:
            self._cancel_flags[task_id] = turn_flag
            record = self._tasks[task_id]
            record.pop("awaiting", None)
            record.pop("question", None)
        return turn_flag

    def _finish_turn(self, task_id: str, turn_flag: threading.Event | None, state: str, *, escalate: str | None = None, **data: Any) -> None:
        """End a turn in a terminal state and free its workspace slot, in one step.

        Both happen under the lock and only while the turn is still current.
        A follow-up that starts right after this cannot lose its new slot to
        the old turn, and a turn that was canceled or replaced changes nothing.
        ``escalate`` publishes an escalation event with that error text.
        """
        with self._lock:
            if not self._set_state(task_id, state, turn_flag=turn_flag, **data):
                return
            if escalate is not None:
                self.bus.publish(TaskEvent(task_id=task_id, type="state", data={"escalation": True, "error": escalate}))
            self._release(task_id)  # frees the slot only if this task still owns it

    def _run_task(self, task_id: str, doc: HandoffDoc, workspace: Path, turn_flag: threading.Event | None = None) -> None:
        """Thread entry point for one turn.

        ``turn_flag`` is the turn's own cancel flag, taken when the turn was
        started; without one, the task's current flag is used. The turn stops
        without running anything when it was canceled before its thread ran.

        Crash safety: a turn runs in a bare daemon thread, so any uncaught
        exception (adapter spawn failure, disk error writing the result file,
        git/subprocess failure inside verification or the knowledge refresh)
        used to kill the thread silently — the task then reported "working"
        forever with no thread left to drive it to a terminal state, and every
        later handoff to that workspace queued behind the ghost. Any exception
        now ends the task in FAILED (unless it already parked or terminated on
        its own) and frees the workspace slot.
        """
        turn_flag = self._flag_for(task_id, turn_flag)
        try:
            try:
                started = self._set_state(task_id, STATE_WORKING, turn_flag=turn_flag)
            finally:
                self._turn_starting.discard(task_id)
            if not started:
                return  # canceled before the turn began: run nothing
            self._run_turn(task_id, doc, workspace, turn_flag)
        except Exception as exc:
            self._handle_turn_crash(task_id, exc, turn_flag)

    def _handle_turn_crash(self, task_id: str, exc: BaseException, turn_flag: threading.Event | None = None) -> None:
        """Terminal safety net for a crashed turn. Marks the task FAILED and
        frees its workspace slot — unless it already parked (input-required
        holds its slot by design), reached a terminal state on its own, or the
        crashed turn was canceled or replaced by a newer one.
        Never raises: this is the last line of defense in a bare thread."""
        record = self._tasks.get(task_id) or {}
        state = record.get("state")
        if state != STATE_INPUT_REQUIRED and state not in TERMINAL_STATES:
            try:
                self._finish_turn(task_id, turn_flag, STATE_FAILED, escalate=f"turn crashed: {exc!r}", error=f"turn crashed before completion: {exc!r}")
            except Exception:
                traceback.print_exc(file=sys.stderr)
        print(f"[maestro] task {task_id} turn crashed (state={state}):", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)

    def _run_turn(self, task_id: str, doc: HandoffDoc, workspace: Path, turn_flag: threading.Event | None) -> None:
        record = self._tasks.get(task_id)
        turn = (record.get("turn") or 0) + 1 if record is not None else 1
        if record is not None:
            record["turn"] = turn  # follow-ups/answers are new turns; result files must not collide
        recorded = (record or {}).get("branch")
        run_dir, branch = self._prepare_run_dir(task_id, doc, workspace, recorded)
        if branch:
            record = self._tasks.get(task_id)
            if record is not None:
                record["branch"] = branch
            self.maestro._write_claim(task_id, "task_branch", branch)
            if recorded and branch != recorded:
                # The branch was renamed by hand since the last turn and this
                # turn adopted the new name: tell every view.
                self.bus.publish(TaskEvent(task_id=task_id, type="branch", data={"old_branch": recorded, "branch": branch}))
        self._record_turn_baseline(task_id, run_dir, doc)
        chain = [doc.target_agent] + [a for a in doc.fallback if a != doc.target_agent]
        last_error: str | None = None
        for agent_name in chain:
            spec = self.registry.get(agent_name) or AgentSpec(name=agent_name, kind=agent_name)
            try:
                adapter = make_adapter(spec)
            except AdapterNotAvailable as exc:
                last_error = str(exc)
                self._record_attempt(task_id, agent_name, None, str(exc))
                continue
            preflight = adapter.preflight()
            if not preflight.ok:
                last_error = preflight.error or "preflight failed"
                self._record_attempt(task_id, agent_name, None, last_error)
                continue
            attempts = 1 + max(0, self.max_retries)
            for attempt in range(attempts):
                if not self._turn_live(task_id, turn_flag):
                    return  # canceled or replaced by a newer turn: start no agent
                rendered = self._rendered_context(doc, task_id, "implementer", spec.kind, self._context_root(task_id, workspace))
                prompt = build_prompt(doc, task_id, run_dir, record_transcript(self._tasks.get(task_id)), context_block=rendered.block)
                settings = self._with_context_settings(spec.kind, self._turn_settings(spec, doc), rendered)

                def _on_line(line: str, _task_id: str = task_id) -> None:
                    self.bus.publish(TaskEvent(task_id=_task_id, type="output", data={"agent": agent_name, "line": line}))

                result = adapter.run(
                    prompt,
                    run_dir,
                    task_id,
                    settings=settings,
                    timeout=spec.timeout_s,
                    log_dir=self.state_dir / "tasks" / task_id,
                    on_line=_on_line,
                    should_cancel=self._should_cancel(task_id, turn_flag),
                )
                self._record_attempt(
                    task_id, agent_name, result,
                    None if result.ok else (result.error or f"agent {agent_name} failed"),
                )
                self._bookkeep_turn(task_id, agent_name, result, turn, attempt)
                with self._lock:
                    if not self._turn_live(task_id, turn_flag):
                        return
                    if result.question:
                        record_transcript_append(self._tasks.get(task_id), result.question)
                        self._set_state(task_id, STATE_INPUT_REQUIRED, question=result.question, awaiting="question", turn_flag=turn_flag)
                        return
                if result.ok:
                    self._post_complete(task_id, doc, run_dir, agent_name, result, turn_flag)
                    return
                last_error = result.error or f"agent {agent_name} failed"
                if attempt < attempts - 1 and self.backoff_s > 0:
                    import time

                    time.sleep(self.backoff_s * (attempt + 1))
        last_error = last_error or "all agents in the chain failed"
        self._finish_turn(task_id, turn_flag, STATE_FAILED, escalate=last_error, error=last_error)

    def _should_cancel(self, task_id: str, turn_flag: threading.Event | None) -> Callable[[], bool] | None:
        """The adapter's cancel check for one turn: true once the turn is canceled or replaced."""
        if turn_flag is None:
            return None
        return lambda: not self._turn_live(task_id, turn_flag)

    def _release(self, task_id: str) -> None:
        """Free the workspace if this task holds it, then start every queued
        handoff that can run now."""
        with self._lock:
            record = self._tasks.get(task_id)
            key = record["workspace"] if record else None
            if key is not None and self._active.get(key) == task_id:
                del self._active[key]
        self._pump_queue()

    def _pump_queue(self) -> None:
        """Start every queued handoff that can now claim a turn, oldest first.

        It runs when a task finishes, is canceled, or stops to ask a question,
        because each of those can free the workspace or a place under the
        limit. Starting a task can park it at once (routing or approval), which
        runs this again; by then this run has finished going through the queue."""
        started: list[str] = []
        with self._lock:
            for queued_id in list(self._queue):
                qrec = self._tasks.get(queued_id)
                if qrec is None:
                    self._queue.remove(queued_id)  # stale entry: drop it
                    continue
                if self._claim_turn(queued_id) is None:
                    continue
                self._queue.remove(queued_id)
                # It is no longer queued, so a cancel from now on frees what it holds.
                qrec["queued"] = False
                started.append(queued_id)
            for queued_id in started:
                self._start_queued(queued_id)

    @staticmethod
    def _turn_settings(spec: AgentSpec, doc: HandoffDoc) -> dict[str, Any]:
        """Adapter settings for one turn: registry defaults, per-task overrides, handoff.

        The handoff's ``model`` and ``effort`` were chosen for the task's target
        agent, so only that agent gets them. A fallback agent, a gate agent or a
        fixer that is a different agent runs with its own registry model and
        effort, because a model name for one CLI means nothing to another.
        Other ``agent_settings`` keys still reach every agent.
        """
        overrides = dict(doc.agent_settings)
        if spec.name != doc.target_agent:
            overrides.pop("model", None)
            overrides.pop("effort", None)
        return {
            **{k: v for k, v in spec.to_dict().items() if v is not None and k in {"model", "effort", "token"}},
            **overrides,
            # Reserved key for api-mode adapters (e.g. a2a_remote) so
            # the full handoff survives daemon-to-daemon hops.
            "maestro_handoff": _forwarded_handoff(doc),
        }

    def _rendered_context(self, doc: HandoffDoc, task_id: str, phase: str, adapter_kind: str, workspace: Path) -> RenderedContext:
        """Render this turn's context block and channel artifacts from the composed entries."""
        entries = [entry_from_dict(e) for e in doc.context_entries]
        return render_context(entries, phase, workspace, self.state_dir / "tasks" / task_id, adapter_kind)

    @staticmethod
    def _with_context_settings(kind: str, settings: dict[str, Any], rendered: RenderedContext) -> dict[str, Any]:
        """Attach the reserved ``maestro_context`` key for claude_code (D1); other adapters ignore it."""
        if kind == "claude_code" and (rendered.system_file is not None or rendered.skills_root is not None):
            settings["maestro_context"] = {
                "system_file": str(rendered.system_file) if rendered.system_file is not None else None,
                "skills_root": str(rendered.skills_root) if rendered.skills_root is not None else None,
            }
        return settings

    def _bookkeep_turn(self, task_id: str, agent_name: str, result: Any, turn: int, attempt: int) -> None:
        """Persist one turn's result file and fold its usage into the task record."""
        (self.state_dir / "tasks" / task_id).mkdir(parents=True, exist_ok=True)
        result_file = f"result-{agent_name}-t{turn}-{attempt}.json"
        (self.state_dir / "tasks" / task_id / result_file).write_text(
            json.dumps(result.to_dict(), indent=2), encoding="utf-8"
        )
        # _record_attempt always immediately precedes this call: stamp the log
        # reference onto that attempt so receipts can point at the turn's output.
        record = self._tasks.get(task_id) or {}
        if record.get("attempts") and record["attempts"][-1].get("agent") == agent_name:
            record["attempts"][-1]["result_file"] = result_file
        if result.usage:
            self.bus.publish(TaskEvent(task_id=task_id, type="usage", data={"agent": agent_name, **result.usage}))
            record = self._tasks.get(task_id) or {}
            # Accumulate cost across attempts: failed work still costs money.
            merged = dict(record.get("usage") or {})
            merged.update(result.usage)
            record["usage"] = merged

    def _record_attempt(self, task_id: str, agent_name: str, result: Any, error: str | None = None, role: str = "implement") -> None:
        record = self._tasks.get(task_id)
        if record is None:
            return
        entry = {"agent": agent_name, "ok": bool(result.ok) if result else False, "role": role}
        if result is not None:
            entry["exit_code"] = result.exit_code
            entry["duration_s"] = round(result.duration_s, 3)
            # per-attempt usage keeps budget attribution exact (failed work still costs)
            if isinstance(result.usage, dict):
                entry["usage"] = result.usage
        if error:
            entry["error"] = error
        entry["finished_at"] = utcnow_iso()
        record.setdefault("attempts", []).append(entry)

    def _post_complete(self, task_id: str, doc: HandoffDoc, workspace: Path, agent_name: str, result: Any, turn_flag: threading.Event | None = None) -> None:
        turn_flag = self._flag_for(task_id, turn_flag)
        self._set_state(task_id, STATE_WORKING, verifying=True, turn_flag=turn_flag)
        verification_ok: bool | None = None
        if doc.verification != "none":
            verification_ok = self._verify(workspace, task_id, doc, output_tail=self._output_tail(result))
        if not self._turn_live(task_id, turn_flag):
            return  # canceled (or replaced by a follow-up) during verification
        record = self._tasks.get(task_id)
        if record is not None:
            record["result"] = result.to_dict()
            record["agent"] = agent_name
            # usage was already accumulated per attempt in _run_task
        self.maestro._write_claim(task_id, "task_result", str(result.output_path or ""))
        parked, verification_ok = self._gate_cycle(task_id, doc, workspace, verification_ok, turn_flag)
        if parked:
            return  # a parked task keeps its workspace slot
        # Refused, and the slot left alone, when the turn was canceled (cancel
        # already freed the slot) or replaced by a follow-up (which now owns it).
        self._finish_turn(task_id, turn_flag, STATE_COMPLETED, verification="PASSED" if verification_ok else ("FAILED" if verification_ok is False else "skipped"))

    def _write_gates_claim(self, task_id: str, verdicts: dict[str, Any], bounces: int) -> None:
        """Record gate verdicts durably (claim) and on the live task record."""
        self.maestro._write_claim(task_id, "task_gates", json.dumps({"verdicts": verdicts, "bounces": bounces}, ensure_ascii=False))
        record = self._tasks.get(task_id)
        if record is not None:
            record["gates"] = dict(verdicts)
            record["bounces"] = bounces

    def _park_question(self, issues: list[str], det_failed: bool, bounces: int) -> str:
        lines = [f"Work-mode gates parked this task after {bounces} auto-fix bounce(s)."]
        if det_failed:
            lines.append("Deterministic verification: FAILED (report in the task artifacts).")
        if issues:
            lines.append("Unresolved issues:")
            lines.extend(f"- {issue}" for issue in issues)
        lines.append("Answer with fix instructions to resume on the task branch, or cancel.")
        return "\n".join(lines)

    def _gate_cycle(self, task_id: str, doc: HandoffDoc, workspace: Path, verification_ok: bool | None, turn_flag: threading.Event | None = None) -> tuple[bool, bool | None]:
        """Run work-mode gates (LLM verify/review) plus capped auto-fix bounces.

        Returns ``(parked, verification_ok)``. ``parked`` is True when the task was
        parked in input-required (the caller must not mark it completed and must
        keep the workspace slot). ``verification_ok`` is the latest deterministic
        result, which changes when a fix bounce re-runs verification. With no gate agents
        configured it returns False immediately — legacy behavior, where completion
        proceeds regardless of the deterministic outcome. A failed deterministic
        check joins the bounce loop only when at least one gate agent is set; an LLM
        verdict can add failures but never override the deterministic result (I1).
        Every gate and fix step belongs to ``turn_flag``'s turn and stops once that
        turn is canceled or replaced.
        """
        turn_flag = self._flag_for(task_id, turn_flag)
        if not doc.verify_agent and not doc.review_agent:
            return False, verification_ok
        record = self._tasks.get(task_id) or {}
        turn = int(record.get("turn") or 1)
        report_path = self.state_dir / "tasks" / task_id / "verification.txt"
        verdicts: dict[str, Any] = {}
        issues: list[str] = []
        det_failed = verification_ok is False

        if doc.verify_agent and doc.verification != "none":
            v = self._gate_turn(task_id, doc, workspace, agent_name=doc.verify_agent, role="verifier", verification_ok=verification_ok, report_path=report_path, turn_flag=turn_flag)
            verdicts["verify"] = {"agent": doc.verify_agent, "ok": v["ok"], "issues": list(v["issues"])}
            if not self._turn_live(task_id, turn_flag):
                return False, verification_ok
            if v["parked"]:
                self._write_gates_claim(task_id, verdicts, 0)
                self._set_state(task_id, STATE_INPUT_REQUIRED, question=v["reason"], awaiting="gate", turn_flag=turn_flag)
                return True, verification_ok
            issues.extend(v["issues"])

        if doc.review_agent:
            r = self._gate_turn(task_id, doc, workspace, agent_name=doc.review_agent, role="reviewer", verification_ok=verification_ok, report_path=report_path, prior_issues=issues, turn_flag=turn_flag)
            verdicts["review"] = {"agent": doc.review_agent, "ok": r["ok"], "issues": list(r["issues"])}
            if not self._turn_live(task_id, turn_flag):
                return False, verification_ok
            if r["parked"]:
                self._write_gates_claim(task_id, verdicts, 0)
                self._set_state(task_id, STATE_INPUT_REQUIRED, question=r["reason"], awaiting="gate", turn_flag=turn_flag)
                return True, verification_ok
            issues.extend(r["issues"])

        max_bounces = doc.max_bounces if doc.max_bounces is not None else DEFAULT_MAX_BOUNCES
        bounces = 0
        while (det_failed or issues) and bounces < max_bounces:
            bounces += 1
            self._set_state(task_id, STATE_WORKING, fixing=True, turn_flag=turn_flag)
            fix_agent = doc.fix_agent or doc.target_agent
            fix_status, fix_error = self._fix_turn(task_id, doc, workspace, fix_agent, issues, det_failed, turn=turn, turn_flag=turn_flag)
            if fix_status == "canceled":
                return False, verification_ok
            if fix_status == "parked":
                self._write_gates_claim(task_id, verdicts, bounces)
                self._set_state(task_id, STATE_INPUT_REQUIRED, question=fix_error or "fixer could not run", awaiting="gate", turn_flag=turn_flag)
                return True, verification_ok
            verification_ok = self._verify(workspace, task_id, doc) if doc.verification != "none" else None
            det_failed = verification_ok is False
            issues = []
            if fix_status == "failed":
                issues.append(f"previous fix attempt by {fix_agent!r} failed: {fix_error}")
            if doc.review_agent:
                r = self._gate_turn(task_id, doc, workspace, agent_name=doc.review_agent, role="reviewer", verification_ok=verification_ok, report_path=report_path, prior_issues=issues, turn_flag=turn_flag)
                verdicts["review"] = {"agent": doc.review_agent, "ok": r["ok"], "issues": list(r["issues"])}
                if not self._turn_live(task_id, turn_flag):
                    return False, verification_ok
                if r["parked"]:
                    self._write_gates_claim(task_id, verdicts, bounces)
                    self._set_state(task_id, STATE_INPUT_REQUIRED, question=r["reason"], awaiting="gate", turn_flag=turn_flag)
                    return True, verification_ok
                issues.extend(r["issues"])
        self._write_gates_claim(task_id, verdicts, bounces)
        if det_failed or issues:
            self._set_state(task_id, STATE_INPUT_REQUIRED, question=self._park_question(issues, det_failed, bounces), awaiting="gate", turn_flag=turn_flag)
            return True, verification_ok
        return False, verification_ok

    def _gate_park(self, agent_name: str, role: str, reason: str) -> dict[str, Any]:
        """Build a park result because a gate turn produced no verdict.

        The caller writes the gates claim first and only then sets the task to
        input-required, so waiters never observe a parked state without verdicts.
        """
        return {
            "ok": False,
            "issues": [],
            "parked": True,
            "reason": (
                f"Work-mode {role} gate for agent {agent_name!r} could not be completed:\n{reason}\n\n"
                "Answer with instructions to resume on the task branch, or cancel."
            ),
        }

    def _gate_turn(self, task_id: str, doc: HandoffDoc, workspace: Path, *, agent_name: str, role: str, verification_ok: bool | None, report_path: Path, prior_issues: list[str] | None = None, turn_flag: threading.Event | None = None) -> dict[str, Any]:
        """Run one read-only LLM gate turn (verifier or reviewer) and parse its verdict.

        Returns {"ok", "issues", "parked"} — on park, "reason" carries the question
        text; the caller writes the gates claim first and only then sets the task to
        input-required (agent unavailable, failed run, agent question, or unparseable
        verdict). Cancellation is not a park: it returns ok=True/parked=False and the
        caller checks _turn_live() before proceeding. A turn that was canceled or
        replaced before the agent starts returns the same way without starting it.
        """
        turn_flag = self._flag_for(task_id, turn_flag)
        record = self._tasks.get(task_id) or {}
        turn = int(record.get("turn") or 1)
        # Per-turn log dir: spawn adapters share one log file per (kind, task), so
        # gate turns get their own directory to keep output_path unambiguous.
        seq = int(record.get("gate_seq") or 0) + 1
        record["gate_seq"] = seq
        turn_log_dir = self.state_dir / "tasks" / task_id / f"{role}-{seq}"
        spec = self.registry.get(agent_name) or AgentSpec(name=agent_name, kind=agent_name)
        try:
            adapter = make_adapter(spec)
        except AdapterNotAvailable as exc:
            return self._gate_park(agent_name, role, f"agent {agent_name!r} unavailable: {exc}")
        preflight = adapter.preflight()
        if not preflight.ok:
            return self._gate_park(agent_name, role, f"agent {agent_name!r} failed preflight: {preflight.error or 'unknown error'}")
        rendered = self._rendered_context(doc, task_id, role, spec.kind, self._context_root(task_id, workspace))
        if role == "verifier":
            prompt = build_verify_prompt(doc, task_id, workspace, verification_ok, report_path, context_block=rendered.block)
        else:
            prompt = build_review_prompt(doc, task_id, workspace, verification_ok, report_path, prior_issues or [], context_block=rendered.block)
        settings = self._with_context_settings(spec.kind, self._turn_settings(spec, doc), rendered)
        if not self._turn_live(task_id, turn_flag):
            return {"ok": True, "issues": [], "parked": False}  # start no agent for a stale turn

        def _on_line(line: str, _task_id: str = task_id) -> None:
            self.bus.publish(TaskEvent(task_id=_task_id, type="output", data={"agent": agent_name, "line": line}))

        result = adapter.run(
            prompt, workspace, task_id,
            settings=settings,
            timeout=spec.timeout_s,
            log_dir=turn_log_dir,
            on_line=_on_line,
            should_cancel=self._should_cancel(task_id, turn_flag),
        )
        self._record_attempt(task_id, agent_name, result, None if result.ok else (result.error or f"{role} turn failed"), role=role)
        self._bookkeep_turn(task_id, agent_name, result, turn, 0)
        if not self._turn_live(task_id, turn_flag):
            return {"ok": True, "issues": [], "parked": False}
        if result.question:
            return self._gate_park(agent_name, role, f"{role} agent asked a question: {result.question}")
        if not result.ok:
            return self._gate_park(agent_name, role, f"{role} turn failed: {result.error or 'unknown error'}")
        output_text = ""
        if result.output_path:
            try:
                output_text = Path(result.output_path).read_text(encoding="utf-8")
            except OSError:
                output_text = ""
        verdict = parse_verdict(output_text)
        if verdict is None:
            snippet = _cap_text(output_text.strip(), 2000)
            return self._gate_park(agent_name, role, f"{role} output contained no parsable VERDICT line (raw output kept in result files)\n\n{snippet}")
        return {"ok": verdict["ok"], "issues": list(verdict["issues"]), "parked": False}

    def _fix_turn(self, task_id: str, doc: HandoffDoc, workspace: Path, fix_agent: str, issues: list[str], det_failed: bool, turn: int, turn_flag: threading.Event | None = None) -> tuple[str, str | None]:
        """Run one auto-fix bounce under the fix agent.

        Returns (status, error) where status is "ok", "failed" (error holds the
        failure text), "parked" (error holds the park question — the caller writes
        the gates claim and sets input-required), or "canceled". A turn that was
        canceled or replaced before the fixer starts returns "canceled" without
        starting it.
        """
        turn_flag = self._flag_for(task_id, turn_flag)
        record = self._tasks.get(task_id) or {}
        seq = int(record.get("gate_seq") or 0) + 1
        record["gate_seq"] = seq
        turn_log_dir = self.state_dir / "tasks" / task_id / f"fix-{seq}"
        spec = self.registry.get(fix_agent) or AgentSpec(name=fix_agent, kind=fix_agent)
        try:
            adapter = make_adapter(spec)
        except AdapterNotAvailable as exc:
            return "parked", f"Work-mode fixer agent {fix_agent!r} is unavailable:\n{exc}\n\nAnswer with instructions to resume on the task branch, or cancel."
        preflight = adapter.preflight()
        if not preflight.ok:
            return "parked", f"Work-mode fixer agent {fix_agent!r} failed preflight:\n{preflight.error or 'unknown error'}\n\nAnswer with instructions to resume on the task branch, or cancel."
        rendered = self._rendered_context(doc, task_id, "implementer", spec.kind, self._context_root(task_id, workspace))
        prompt = build_fix_prompt(doc, task_id, workspace, issues, det_failed, context_block=rendered.block)
        settings = self._with_context_settings(spec.kind, self._turn_settings(spec, doc), rendered)
        if not self._turn_live(task_id, turn_flag):
            return "canceled", None  # start no fixer for a stale turn

        def _on_line(line: str, _task_id: str = task_id) -> None:
            self.bus.publish(TaskEvent(task_id=_task_id, type="output", data={"agent": fix_agent, "line": line}))

        result = adapter.run(
            prompt, workspace, task_id,
            settings=settings,
            timeout=spec.timeout_s,
            log_dir=turn_log_dir,
            on_line=_on_line,
            should_cancel=self._should_cancel(task_id, turn_flag),
        )
        self._record_attempt(task_id, fix_agent, result, None if result.ok else (result.error or "fix turn failed"), role="fix")
        self._bookkeep_turn(task_id, fix_agent, result, turn, 0)
        if not self._turn_live(task_id, turn_flag):
            return "canceled", None
        if result.question:
            return "parked", f"Fix agent {fix_agent!r} asked a question:\n{result.question}\n\nAnswer to resume on the task branch."
        if not result.ok:
            return "failed", result.error or "fix turn failed"
        return "ok", None

    @staticmethod
    def _base_head(workspace: Path) -> str | None:
        """The HEAD commit before this turn's work (the evidence baseline), or None."""
        probe = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True, capture_output=True)
        if probe.returncode != 0:
            return None
        head = probe.stdout.strip()
        return head or None

    def _record_turn_baseline(self, task_id: str, workspace: Path, doc: HandoffDoc) -> None:
        """Record what verification compares this turn against, before the agent runs.

        Two values are kept, each in the task record and in a claim so that a
        restarted daemon still has them:

        - ``base_head``: the HEAD commit, so new commits count as work.
        - ``python_test_suite``: whether the project had a Python test suite.
          Verification accepts pytest's "no tests collected" exit code only
          when it did not. When it did, the project is verified with pytest
          even if pytest is missing or the tests are gone, so an agent that
          deletes or hides every test does not pass. Only auto-detected
          verification uses this value, so the project is scanned only in
          that mode.
        """
        base_head = self._base_head(workspace)
        if base_head:
            record = self._tasks.get(task_id)
            if record is not None:
                record["base_head"] = base_head  # per-turn evidence baseline for verification
            self.maestro._write_claim(task_id, "task_base_head", base_head)
        if doc.verification == "auto":
            suite = _has_python_test_suite(workspace)
            record = self._tasks.get(task_id)
            if record is not None:
                record["python_test_suite"] = suite
            self.maestro._write_claim(task_id, "task_python_test_suite", "true" if suite else "false")

    def _recorded_python_test_suite(self, task_id: str) -> bool | None:
        """Whether the project had a Python test suite when the turn started.

        The task record is read first, then the claim. None means nothing was
        recorded, for example for a task started by an older Maestro.
        """
        record = self._tasks.get(task_id) or {}
        recorded = record.get("python_test_suite")
        if recorded is not None:
            return bool(recorded)
        claim = self.maestro._claims(task_id).get("task_python_test_suite")
        if claim is None:
            return None
        return claim == "true"

    def _workspace_has_changes(self, workspace: Path, task_id: str) -> tuple[bool, str]:
        """Evidence that the turn produced work: working-tree changes or new commits."""
        status = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain"], text=True, capture_output=True)
        if status.returncode == 0 and status.stdout.strip():
            return True, "working-tree changes are present"
        record = self._tasks.get(task_id) or {}
        base = record.get("base_head") or self.maestro._claims(task_id).get("task_base_head")
        if base:
            ahead = subprocess.run(
                ["git", "-C", str(workspace), "rev-list", "--count", f"{base}..HEAD"], text=True, capture_output=True
            )
            count = ahead.stdout.strip()
            if ahead.returncode == 0 and count not in ("", "0"):
                return True, f"{count} new commit(s) since the turn started"
        return False, "no working-tree changes and no new commits since the turn started"

    @staticmethod
    def _output_tail(result: Any, limit: int = 500) -> str | None:
        """The tail of a turn's output (for reports), or None when unavailable."""
        path = getattr(result, "output_path", None)
        if not path:
            return None
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text[-limit:] or None

    def _verify(self, workspace: Path, task_id: str, doc: HandoffDoc, output_tail: str | None = None) -> bool:
        import shlex

        from .worker import _pytest_found_no_tests

        configured = None
        if doc.verification == "command":
            # The command is verification_command when set; otherwise the
            # original handoff carried it in request (M2 simplification).
            configured = shlex.split(doc.verification_command or doc.request)
        # Whether the project had a Python test suite when the turn started.
        # It is used only for auto-detection, and None means it is not known.
        recorded_suite = self._recorded_python_test_suite(task_id) if configured is None else None
        test_cmd, note = _verification_command(workspace, configured, had_test_suite=recorded_suite)
        # The auto-detected fallback is `git diff --check` plus a note. It passes
        # trivially on an untouched workspace, so it must never certify a turn
        # that left no changes behind — otherwise a zero-work turn (e.g. an agent
        # that stopped to ask for approval a batch run can never receive) would
        # complete as PASSED. Explicit commands and real test runners are
        # authoritative and keep their current semantics.
        fallback_only = note is not None and test_cmd == ["git", "diff", "--check"]
        # Whether the turn left work behind is recorded before the test command
        # runs, so that files the test command itself creates are not mistaken
        # for the agent's work.
        has_changes, evidence = self._workspace_has_changes(workspace, task_id)
        diff = subprocess.run(["git", "diff", "--check"], cwd=workspace, text=True, capture_output=True)
        timeout_s = int(self._config(self._context_root(task_id, workspace)).get("verification_timeout_s", 1800)) or None
        self._set_verifying(task_id, True)
        limit_text = f"time limit {_duration_text(timeout_s)}" if timeout_s else "no time limit"
        self.bus.publish(TaskEvent(task_id=task_id, type="output", data={
            "agent": "maestro", "line": f"[verify] running: {' '.join(test_cmd)} ({limit_text})",
        }))
        tests = _run_verification_command(test_cmd, workspace, timeout_s)
        self.bus.publish(TaskEvent(task_id=task_id, type="output", data={
            "agent": "maestro",
            "line": "[verify] stopped: the time limit was reached" if tests.timed_out else f"[verify] finished with exit code {tests.returncode}",
        }))
        self._set_verifying(task_id, False)
        # An auto-detected pytest run that found no tests (exit code 5) is not
        # a test failure when the project had no Python test suite when the
        # turn started. It proves nothing about the turn either, so it is
        # treated like the `git diff --check` fallback: it passes only when the
        # turn left changes behind. When the project did have a test suite,
        # exit code 5 means the tests were removed or hidden, and it is a
        # failure. When nothing was recorded, exit code 5 is a failure too,
        # because there is no evidence that the project had no tests. An
        # explicit command always keeps its exit code.
        pytest_no_tests = configured is None and _pytest_found_no_tests(test_cmd, tests.returncode)
        suite_vanished = pytest_no_tests and recorded_suite is not False
        no_tests = pytest_no_tests and not suite_vanished
        ok = diff.returncode == 0 and (tests.returncode == 0 or no_tests)
        no_changes_reason: str | None = None
        if ok and (fallback_only or no_tests) and not has_changes:
            ok = False
            no_changes_reason = evidence
        if tests.timed_out:
            ok = False
        note_text = f"verification note: {note}\n\n" if note else ""
        if tests.timed_out:
            note_text += (
                f"RESULT: FAILED — the verification command did not finish within {_duration_text(timeout_s)} "
                "(the [verification] timeout_s setting), so Maestro stopped it and every process it started. "
                "Raise timeout_s, or set it to 0 for no limit, if the tests need longer.\n\n"
            )
        if suite_vanished:
            note_text += (
                "verification note: pytest collected no tests although the project had a Python test suite "
                f"when the turn started (exit code {tests.returncode}). This counts as a failure, because "
                "the tests may have been deleted, renamed or hidden.\n\n"
            )
        if no_tests:
            note_text += (
                f"verification note: pytest found no tests to run (exit code {tests.returncode}). "
                "This is not counted as a test failure, but it is not evidence of work either, "
                "so the task passes only if the turn changed the workspace.\n\n"
            )
        no_changes_text = ""
        if no_changes_reason is not None:
            tail_text = f"\n\nlast lines of the agent's output:\n{output_tail}" if output_tail else ""
            if no_tests:
                weak_check = (
                    "The test command found no tests to run, and the only other check is "
                    "`git diff --check`, which passes trivially on an untouched workspace"
                )
            else:
                weak_check = (
                    "The only available check is `git diff --check` (no project test runner "
                    "was detected), which passes trivially on an untouched workspace"
                )
            no_changes_text = (
                "\nRESULT: FAILED — no changes detected. "
                f"{no_changes_reason}. {weak_check}, so Maestro will not report PASSED without evidence of work."
                + tail_text
            )
        report = (
            f"workspace: {workspace}\nverification command: {' '.join(test_cmd)}\n\n{note_text}"
            f"git diff --check:\n{diff.stdout}\n{diff.stderr}\n\nverification:\n{tests.stdout}\n{tests.stderr}{no_changes_text}"
        )
        task_dir = self.state_dir / "tasks" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        report_path = task_dir / "verification.txt"
        report_path.write_text(report, encoding="utf-8")
        self.maestro._write_claim(task_id, "task_verification", f"{'PASSED' if ok else 'FAILED'}: {report_path}")
        self.bus.publish(TaskEvent(task_id=task_id, type="verify", data={"ok": ok, "command": " ".join(test_cmd), "report": str(report_path)}))
        return ok

    def _set_verifying(self, task_id: str, verifying: bool) -> None:
        """Write the VERIFYING phase while the tests run, and IMPLEMENTING after.

        Only for a task that is still working: a task canceled in the meantime
        keeps the phase its cancel wrote."""
        if (self._tasks.get(task_id) or {}).get("state") != STATE_WORKING:
            return
        phase = Phase.VERIFYING if verifying else Phase.IMPLEMENTING
        self.maestro._write_claim(task_id, "task_status", phase.value)

    def _prepare_run_dir(self, task_id: str, doc: HandoffDoc, workspace: Path, recorded: str | None) -> tuple[Path, str | None]:
        """Get the task's run directory ready for this turn; return it and the branch.

        A task that runs in the workspace checks out its branch there, as
        before. A task that runs in a worktree gets the worktree created on its
        first turn, from the commit checked out in the workspace, and created
        again from its branch if it was removed (only committed work comes
        back). See docs/design-parallel-tasks.md, section 3."""
        record = self._tasks.get(task_id) or {}
        if record.get("run_dir_kind") != "worktree":
            branch = self._prepare_branch(workspace, task_id, doc.commit_policy, requested=doc.branch, recorded=recorded)
            self._record_run_dir(task_id, workspace, "workspace")
            return workspace, branch
        run_dir = Path(record["run_dir"])
        root = worktrees.worktree_path(self.state_dir, task_id)  # run_dir is root or a subdirectory of it
        if recorded:
            branch = recorded if branch_exists(workspace, recorded) else self._renamed_task_branch(workspace, task_id, recorded)
            if worktrees.ensure_worktree(workspace, root, branch):
                self.bus.publish(TaskEvent(task_id=task_id, type="output", data={
                    "agent": "maestro",
                    "line": f"[maestro] the worktree {root} was missing, so it was created again from branch {branch}; only committed work is in it",
                }))
            else:
                checked = subprocess.run(["git", "-C", str(root), "checkout", branch], text=True, capture_output=True)
                if checked.returncode != 0:
                    raise RuntimeError(
                        f"could not check out the task branch {branch!r} in {root}: {(checked.stderr or checked.stdout).strip()}"
                    )
        else:
            branch = doc.branch or f"maestro/{task_id}"
            worktrees.add_worktree(workspace, root, branch, new_branch=True, start=worktrees.head_commit(workspace))
        self._record_run_dir(task_id, run_dir, "worktree")
        return run_dir, branch

    def _record_run_dir(self, task_id: str, run_dir: Path, kind: str) -> None:
        """Store where the task's turns run, for status, cleanup and later turns."""
        record = self._tasks.get(task_id)
        if record is not None:
            record["run_dir"], record["run_dir_kind"] = str(run_dir), kind
        self.maestro._write_claim(task_id, "task_run_dir", str(run_dir))
        self.maestro._write_claim(task_id, "task_run_dir_kind", kind)
        self.maestro._write_claim(task_id, "task_run_dir_removed", "false")

    def _context_root(self, task_id: str, fallback: Path) -> Path:
        """The directory context files and skills resolve against: the
        caller's workspace, because files that are not in git exist only there."""
        record = self._tasks.get(task_id) or {}
        return Path(record["workspace"]) if record.get("workspace") else fallback

    def _prepare_branch(
        self, workspace: Path, task_id: str, commit_policy: str,
        requested: str | None = None, recorded: str | None = None,
    ) -> str | None:
        """Check out the task branch, creating it on the task's first turn.

        On the first turn (no branch recorded yet) the branch is created with
        the name the handoff asked for, or the default ``maestro/<task_id>``.
        A branch the handoff asked for must be created fresh; if git cannot
        create it, the turn fails and the message names the command that picks
        another name. A default branch that already exists is checked out.

        On a later turn the recorded branch is checked out. If it no longer
        exists, git's reflog is searched for a hand rename (``git branch -m``):
        when exactly one branch was renamed from it, that branch is adopted and
        returned. Otherwise the turn fails; a fresh branch is never created in
        place of the task's branch, because the agent would then work without
        the task's earlier commits.

        If git cannot check out the task branch, the turn fails rather than
        letting the agent work on whatever branch is checked out while the
        record names the task branch.
        """
        if commit_policy == "no-commit":
            return None
        probe = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"], text=True, capture_output=True)
        if probe.returncode != 0:
            return None
        if recorded:
            branch = recorded if branch_exists(workspace, recorded) else self._renamed_task_branch(workspace, task_id, recorded)
            checked = subprocess.run(["git", "-C", str(workspace), "checkout", branch], text=True, capture_output=True)
            if checked.returncode != 0:
                raise RuntimeError(
                    f"could not check out the task branch {branch!r}: {(checked.stderr or checked.stdout).strip()}"
                )
            return branch
        branch = requested or f"maestro/{task_id}"
        created = subprocess.run(["git", "-C", str(workspace), "checkout", "-b", branch], text=True, capture_output=True)
        if created.returncode != 0:
            if requested:
                ref = self._task_ref(task_id)
                raise RuntimeError(
                    f"could not create the requested branch {branch!r}: {(created.stderr or created.stdout).strip()}. "
                    f"Pick another name with 'maestro task rename-branch {ref} <new-name>' (MCP tool: rename_task_branch), "
                    f"then send the follow-up again; or do both at once with "
                    f"'maestro task continue {ref} --request <instruction> --branch <new-name>' (MCP tool: followup with branch)"
                )
            existing = subprocess.run(["git", "-C", str(workspace), "checkout", branch], text=True, capture_output=True)
            if existing.returncode != 0:
                raise RuntimeError(
                    f"could not check out the task branch {branch!r}: {(existing.stderr or existing.stdout).strip()}"
                )
        return branch

    def _renamed_task_branch(self, workspace: Path, task_id: str, recorded: str) -> str:
        """Return the branch that ``recorded`` was renamed to by hand, or fail the turn."""
        candidates = find_renamed_branches(workspace, recorded)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise RuntimeError(
                f"the task branch {recorded!r} no longer exists, and git has no record that it was renamed, "
                "so this turn will not create a new, empty branch in its place. Recreate it under that name "
                f"(for example 'git branch {recorded} <commit>') and send the follow-up again"
            )
        raise RuntimeError(
            f"the task branch {recorded!r} no longer exists, and git records more than one branch that came from "
            f"it ({', '.join(candidates)}). Record the right one with "
            f"'maestro task rename-branch {self._task_ref(task_id)} <branch>'"
        )

    def _task_ref(self, task_id: str) -> str:
        """The short task number when there is one (what users type), else the task id."""
        return str(self.maestro._claims(task_id).get("task_number") or task_id)

    # ------------------------------------------------------------ interactions
    def answer_question(self, task_id: str, answer: str) -> dict[str, Any]:
        # Durable-aware: a task parked in an earlier daemon run can be answered
        # after a restart (its record is reconstructed from claims).
        record = self._tasks.get(task_id) or self._durable_record(task_id)
        if record is None:
            raise KeyError(f"Unknown task reference {task_id!r}")
        if record["state"] != STATE_INPUT_REQUIRED:
            raise ValueError(f"Task {task_id} is not awaiting input (state={record['state']})")
        if not str(answer).strip():
            raise ValueError("Answer cannot be empty")
        # The park this answer is for. Starting the next turn replaces the
        # task's cancel flag, so a second answer that arrives at the same time
        # sees a different flag and is refused below.
        parked_flag = self._cancel_flags.get(task_id)
        doc = self._doc_from_record(record)
        routing_answer = record.get("awaiting") == "routing"
        needs_routing = False
        if routing_answer:
            # The task is parked on the routing question: the answer selects the
            # agent/model, then the task starts. A bad answer raises and leaves
            # the task input-required for another attempt.
            doc = self._parse_routing_answer(record, answer)
        else:
            # Parked for another reason (e.g. sensitive approval) while routing was
            # never resolved: ask now instead of running under a guessed target.
            needs_routing = not self._apply_defaults(doc, record["workspace"])
        routing_question = self._routing_question() if needs_routing else None
        workspace = Path(record["workspace"])
        # The state check and the start of the turn happen together under the
        # lock, so of two answers given at the same moment only one starts a turn.
        with self._lock:
            if record["state"] != STATE_INPUT_REQUIRED or self._cancel_flags.get(task_id) is not parked_flag:
                raise ValueError(
                    f"Task {task_id} is no longer awaiting this answer (state={record['state']}); "
                    "another answer or a cancel reached it first"
                )
            if routing_question is not None:
                self._persist(task_id, doc)
                self._set_state(task_id, STATE_INPUT_REQUIRED, question=routing_question, awaiting="routing")
                return {"task_id": task_id, "state": STATE_INPUT_REQUIRED}
            # A branch rename may have changed the requested branch since doc
            # was read above; the rename runs under this lock, so read it again.
            doc.branch = _requested_branch(record)
            record["doc"] = doc.to_dict()
            if routing_answer:
                record["target_agent"] = doc.target_agent
                self._persist(task_id, doc)
            else:
                # The answer must reach the agent's next prompt. An agent question
                # already has an open transcript entry; a park by a gate or an
                # approval does not, so the question is recorded with the answer.
                record_transcript_answer(record, answer, question=record.get("question"))
            turn_flag = self._new_turn(task_id)
            # The answer starts a new turn, which starts at SUBMITTED like a
            # follow-up. Leaving input-required here, not in the turn's thread,
            # means a wait() right after this call waits for the new turn
            # instead of returning at once on the old park.
            self._set_state(task_id, STATE_SUBMITTED, turn_flag=turn_flag)
            if not self._acquire_or_queue(task_id):
                # Parked tasks keep their slot, but not across a daemon restart;
                # another task may hold the workspace now.
                return {"task_id": task_id, "state": STATE_SUBMITTED, "queued": True, "reason": self._queue_reason(task_id)}
            self._turn_starting.add(task_id)
            thread = threading.Thread(target=self._run_task, args=(task_id, doc, workspace, turn_flag), daemon=True)
            thread.start()
        return {"task_id": task_id, "state": STATE_WORKING}

    def _acquire_or_queue(self, task_id: str) -> bool:
        """Claim a turn for a follow-up or an answer (see _claim_turn).

        The caller holds the lock and has just checked, under it, that the
        task may start a turn; that is what stops two concurrent follow-ups
        or answers from both getting here. A task that ran in the workspace
        keeps it while it is parked, so its own claim succeeds.

        Returns True when the turn may start now. Otherwise the continuation
        is queued (FIFO, like a queued delegation), False is returned, and it
        starts when the workspace or a place under the limit frees.
        """
        record = self._tasks[task_id]
        with self._lock:
            if self._claim_turn(task_id) is not None:
                record["queued"] = False
                return True
            self._queue.append(task_id)
            record["queued"] = True
            record["continuation"] = True
            return False

    # ------------------------------------------------------------ task knowledge
    def _refresh_knowledge(self, task_id: str) -> TaskKnowledge | None:
        """Re-project durable state into the task's knowledge snapshot.

        Writes the ``task_knowledge`` claim (the single durable home of the
        snapshot — works on both storage backends, survives restarts). Returns
        the projected knowledge, or None when no claims exist yet for the task.
        """
        claims = self.maestro._claims(task_id)
        if not claims:
            return None
        runtime: dict[str, Any] = {}
        raw_runtime = claims.get("task_runtime")
        if isinstance(raw_runtime, str):
            try:
                parsed = json.loads(raw_runtime)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                runtime = parsed
        # Changed files are read where the task's work is: its run directory.
        workspace_raw = claims.get("task_run_dir") or claims.get("task_workspace") or self._registry_workspace(task_id)
        knowledge = project_knowledge(task_id, claims, runtime, workspace=Path(workspace_raw) if workspace_raw else None)
        self.maestro._write_claim(task_id, "task_knowledge", knowledge.serialize())
        return knowledge

    def _durable_record(self, task_id: str) -> dict[str, Any] | None:
        """Rebuild the in-memory record from durable claims after a restart.

        The workspace comes from the task_workspace claim or, for a task
        migrated from the legacy journal without that claim, from the task's
        registry record. Returns None when neither names a workspace (unknown
        task). The Q&A transcript is intentionally not reconstructed — it was
        never persisted (pre-existing behavior); continuations rely on task
        knowledge instead of the transcript.
        """
        claims = self.maestro._claims(task_id)
        workspace = claims.get("task_workspace") or self._registry_workspace(task_id)
        if not workspace:
            return None
        runtime = _runtime_from_claims(claims)
        doc = runtime.get("doc") if isinstance(runtime.get("doc"), dict) else None
        record = {
            "task_id": task_id,
            # Prefer the runtime snapshot's own state (the phase claim is lossy:
            # parked and completed both map to REVIEWING); unknown → failed.
            "state": _durable_state(claims, runtime),
            "origin_agent": claims.get("task_origin_agent"),
            "target_agent": claims.get("task_target_agent") or (doc or {}).get("routing", {}).get("target_agent"),
            "agent": runtime.get("agent"),
            "workspace": workspace,
            "branch": claims.get("task_branch") or runtime.get("branch"),
            "title": claims.get("task_title"),
            "doc": doc,
            "attempts": runtime.get("attempts") or [],
            "result": runtime.get("result"),
            "usage": runtime.get("usage"),
            "error": runtime.get("error"),
            "started_at": runtime.get("started_at"),
            "turn": runtime.get("turn") or 0,
            "transcript": [],
        }
        # What a parked task was waiting for (routing vs. a question), and the
        # gate and verification details the status views show.
        for key in ("awaiting", "question", "gates", "bounces", "base_head", "python_test_suite", "verification", "run_dir", "run_dir_kind"):
            if runtime.get(key) is not None:
                record[key] = runtime[key]
        with self._lock:
            self._tasks[task_id] = record
            self._cancel_flags.setdefault(task_id, threading.Event())
        return record

    def _raw_history_bytes(self, task_id: str) -> int:
        """Total bytes of the task's durable artifact trail (honest raw size).

        One ``stat`` per entry (no separate is_file probe): an entry that
        vanishes or is unreadable between enumeration and stat is skipped."""
        task_dir = self.state_dir / "tasks" / task_id
        if not task_dir.is_dir():
            return 0
        total = 0
        for path in sorted(task_dir.rglob("*")):
            try:
                st = path.stat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
        return total

    def followup(
        self, task_id: str, instruction: str, context_mode: str = "reuse", branch: str | None = None,
    ) -> dict[str, Any]:
        """Resume a finished task (completed/failed/canceled) with a new instruction.

        The same target agent continues on the same task branch. With
        ``context_mode="reuse"`` (default) the turn receives a compact task-
        knowledge snapshot — goal, current state, verification result, known
        issues — instead of re-discovering everything; raw history stays in the
        durable record. ``context_mode="fresh"`` skips the snapshot for a clean
        reasoning context (same task/workspace/branch). Depth is decremented so
        follow-up chains cannot nest forever. Works after a daemon restart: an
        unknown in-memory task is reconstructed from durable claims.

        ``branch`` renames the task's branch first, exactly as
        :meth:`rename_branch` does; when the task has no branch yet it changes
        the name this turn creates. It is the way out when the first turn
        could not create the branch it asked for.
        """
        if context_mode not in ("reuse", "fresh"):
            raise ValueError(f"context_mode must be 'reuse' or 'fresh': {context_mode!r}")
        record = self._tasks.get(task_id) or self._durable_record(task_id)
        if record is None:
            raise KeyError(f"Unknown task reference {task_id!r}")
        instruction = str(instruction).strip()
        if not instruction:
            raise ValueError("Follow-up instruction cannot be empty")
        state = record["state"]
        if state not in TERMINAL_STATES:
            raise ValueError(
                f"Task {task_id} is still active (state={state}); cancel it or answer its question before following up"
            )
        doc = self._doc_from_record(record)
        followup_doc = HandoffDoc(
            title=f"{record.get('title') or task_id} — follow-up",
            request=instruction,
            design=doc.design,
            context_files=list(doc.context_files),
            context_notes=(
                (doc.context_notes + "\n" if doc.context_notes else "")
                + "Follow-up turn: the previous work for this task is already on the task branch/working tree; build on it rather than redoing it."
            ),
            # A pinned fixer owns follow-up turns (a follow-up is a fix); otherwise
            # the original target continues as before. Work-mode slots carry over so
            # later turns keep the same profile.
            target_agent=doc.fix_agent or record.get("target_agent") or doc.target_agent,
            explicit_target=True,
            fallback=list(doc.fallback),
            origin_agent=record.get("origin_agent") or doc.origin_agent,
            parent_task_id=task_id,
            mode=doc.mode,
            review_agent=doc.review_agent,
            verify_agent=doc.verify_agent,
            fix_agent=doc.fix_agent,
            max_bounces=doc.max_bounces,
            artifacts=list(doc.artifacts),
            verification=doc.verification,
            commit_policy=doc.commit_policy,
            branch=doc.branch,
            # In command mode the original request was the verification
            # command; the follow-up's request is an instruction and must
            # never be run as a command.
            verification_command=doc.verification_command or (doc.request if doc.verification == "command" else None),
            budget_hint=doc.budget_hint,
            sensitive=doc.sensitive,
            max_depth_remaining=max(0, doc.max_depth_remaining - 1),
        )
        if followup_doc.max_depth_remaining <= 0:
            raise ValueError("Max delegation depth exceeded; refusing to nest further")
        # Continuation context: carry the task's composed [[context]] entries
        # forward (follow-up turns must see what earlier turns saw), dropping
        # any stale knowledge entry from an earlier turn; in reuse mode a fresh
        # compact knowledge snapshot is then injected as its own entry, rendered
        # through the standard context pipeline.
        followup_doc.context_entries = [e for e in doc.context_entries if e.get("label") != "task-knowledge"]
        raw_history = self._raw_history_bytes(task_id)
        task_config = self._config((self._tasks.get(task_id) or {}).get("workspace"))
        continuation_config = task_config.get("continuation") or {}
        context_stats: dict[str, Any] | None = None
        if context_mode == "reuse" and continuation_config.get("enabled", True):
            knowledge = self._refresh_knowledge(task_id)
            block = render_continuation_block(knowledge, continuation_budget_chars(task_config)) if knowledge is not None else ""
            if block:
                followup_doc.context_entries.append({"label": "task-knowledge", "kind": "text", "text": block})
            context_stats = {
                "mode": "reuse",
                "knowledge_chars": len(knowledge.serialize()) if knowledge is not None else 0,
                "context_chars": len(block),
                "raw_history_bytes": raw_history,
                # chars/4 estimate — labeled as an estimate in receipts/docs.
                "estimated_tokens": estimate_tokens(block),
                "reduction_ratio": round(1 - len(block) / raw_history, 3) if (block and raw_history > 0) else None,
            }
        elif context_mode == "fresh":
            context_stats = {"mode": "fresh", "raw_history_bytes": raw_history}
        workspace = Path(record["workspace"])
        # Check the state again and start the turn in one step under the lock:
        # of two follow-ups sent at the same moment, only one starts a turn and
        # the other gets the "still active" error. Nothing is written to the
        # record before this check, so the refused call changes nothing.
        with self._lock:
            state = record["state"]
            if state not in TERMINAL_STATES:
                raise ValueError(
                    f"Task {task_id} is still active (state={state}); cancel it or answer its question before following up"
                )
            # The rename runs here, under the lock and after the state check,
            # so a refused name changes nothing, and the turn uses the name it
            # sets, including a new requested name for a task with no branch yet.
            if branch is not None:
                self.rename_branch(task_id, branch)
            followup_doc.branch = _requested_branch(record)
            if context_stats is not None:
                record["context_stats"] = context_stats
            record["doc"] = followup_doc.to_dict()  # the chain accumulates: later follow-ups see reduced depth
            record["target_agent"] = followup_doc.target_agent  # a pinned fixer becomes the task's current target
            self._persist(task_id, followup_doc)
            # A new turn with its own clear cancel flag. A canceled earlier turn
            # keeps its set flag, so it can never act for this turn.
            turn_flag = self._new_turn(task_id)
            self._set_state(task_id, STATE_SUBMITTED, turn_flag=turn_flag)
            if not self._acquire_or_queue(task_id):
                self._persist(task_id)
                return {"task_id": task_id, "state": STATE_SUBMITTED, "queued": True, "reason": self._queue_reason(task_id), "ts": utcnow_iso()}
            thread = threading.Thread(target=self._run_task, args=(task_id, followup_doc, workspace, turn_flag), daemon=True)
            thread.start()
        return {"task_id": task_id, "state": STATE_SUBMITTED, "ts": utcnow_iso()}

    def cleanup_worktree(self, task_id: str, force: bool = False) -> dict[str, Any]:
        """Remove a task's worktree (docs/design-parallel-tasks.md, section 5).

        Never touches the workspace and never removes the branch. Refuses while
        the task is running, and refuses to drop uncommitted work unless
        ``force`` is given; the message lists the changed files."""
        with self._lock:
            record = self._tasks.get(task_id) or self._durable_record(task_id)
            if record is None:
                raise KeyError(f"Unknown task reference {task_id!r}")
            if record.get("state") in (STATE_SUBMITTED, STATE_WORKING) or task_id in self._turn_starting:
                raise ValueError(f"Task {task_id} is running; wait for it to finish or cancel it first")
            claims = self.maestro._claims(task_id)
            kind = record.get("run_dir_kind") or claims.get("task_run_dir_kind") or "workspace"
            run_dir = Path(record.get("run_dir") or claims.get("task_run_dir") or record["workspace"])
            if kind != "worktree":
                return {"task_id": task_id, "run_dir": str(run_dir), "removed": False,
                        "reason": "this task ran in the workspace; Maestro never removes or changes your checkout"}
            root = worktrees.worktree_path(self.state_dir, task_id)  # the whole worktree, even when run_dir is a subdirectory
            if not root.is_dir():
                return {"task_id": task_id, "run_dir": str(run_dir), "removed": False, "reason": "the worktree is already gone"}
            dirty = worktrees.dirty_files(root)
            if dirty and not force:
                raise ValueError(
                    f"The worktree {root} has uncommitted changes: {', '.join(dirty)}. "
                    "Commit them, or pass --force to remove the worktree anyway"
                )
            worktrees.remove_worktree(Path(record["workspace"]), root, force=force)
            self.maestro._write_claim(task_id, "task_run_dir_removed", "true")
            return {"task_id": task_id, "run_dir": str(run_dir), "removed": True, "reason": "removed; the branch is kept"}

    def rename_branch(self, task_id: str, new_branch: str) -> dict[str, Any]:
        """Rename a task's branch in git and in the task's record.

        Refused while an agent is working on the task, because the agent's
        checkout would change under it. The check and the rename run under the
        daemon lock, and ``followup`` and ``answer_question`` start a turn
        under the same lock, so a turn cannot start in the middle of a rename
        and check out (or recreate) the old name. See
        :func:`maestro.branches.rename_task_branch` for the git side, including
        the case where the branch was already renamed by hand and the case
        where the task has no branch yet (the result then has ``pending``).
        """
        with self._lock:
            record = self._tasks.get(task_id) or self._durable_record(task_id)
            if record is None:
                raise KeyError(f"Unknown task reference {task_id!r}")
            state = STATE_WORKING if task_id in self._turn_starting else record["state"]
            if state in (STATE_SUBMITTED, STATE_WORKING):
                raise ValueError(f"Task {task_id} is running (state={state}); rename its branch after it finishes")
            result = rename_task_branch(self.maestro, task_id, new_branch)
            if result.get("pending"):
                # No branch exists yet: the new name goes into the handoff
                # record, which the next turn reads. rename_task_branch refuses
                # a task without a recorded handoff, so record["doc"] is a dict.
                record["doc"]["expectations"]["branch"] = result["branch"]
                self._persist(task_id)
                return result
            record["branch"] = result["branch"]
            self._persist(task_id)
            self.bus.publish(TaskEvent(task_id=task_id, type="branch", data={"old_branch": result["old_branch"], "branch": result["branch"]}))
            return result

    def _doc_from_record(self, record: dict[str, Any]) -> HandoffDoc:
        from .handoff import from_dict

        if isinstance(record.get("doc"), dict):
            return from_dict(record["doc"])
        claims = self.maestro._claims(record["task_id"])
        return HandoffDoc(
            title=str(record.get("title") or record["task_id"]),
            request=str(claims.get("task_request") or record.get("title") or ""),
            target_agent=str(record.get("target_agent") or "codex"),
            origin_agent=str(record.get("origin_agent") or "human"),
            commit_policy="branch",
        )

    def cancel(self, task_id: str, reason: str = "") -> dict[str, Any]:
        # Durable-aware: a parked task from an earlier daemon run can be
        # canceled after a restart (its record is reconstructed from claims).
        record = self._tasks.get(task_id) or self._durable_record(task_id)
        if record is None:
            raise KeyError(f"Unknown task reference {task_id!r}")
        # Check and cancel in one step under the lock. A turn ends under the
        # same lock (_finish_turn), so a task that completes at this moment is
        # either completed (and this raises) or canceled, never both.
        with self._lock:
            if record["state"] in TERMINAL_STATES:
                raise ValueError(f"Task {task_id} already finished (state={record['state']})")
            flag = self._cancel_flags.get(task_id)
            if flag is not None:
                flag.set()
            queued = bool(record.get("queued"))
            if queued:
                # Still waiting in the queue: it never held the slot.
                try:
                    self._queue.remove(task_id)
                except ValueError:
                    pass
                record["queued"] = False
                record.pop("continuation", None)
            self._set_state(task_id, STATE_CANCELED, reason=reason or "canceled by user")
            if not queued:
                # Freed under the lock, so a follow-up sent right after this
                # cancel cannot have its new slot freed by it.
                self._release(task_id)
        return {"task_id": task_id, "state": STATE_CANCELED}

    def wait(
        self,
        task_id: str,
        timeout: float | None = None,
        stop_states: tuple[str, ...] = TERMINAL_STATES + (STATE_INPUT_REQUIRED,),
    ) -> dict[str, Any]:
        # Subscribe before looking at the state. A transition after the check
        # is then always delivered (its seq is above start_seq), and one before
        # it is seen by the check; no transition can fall between the two.
        sub = self.bus.subscribe()
        try:
            record = self._tasks.get(task_id)
            if record is not None:
                if record["state"] in stop_states:
                    return self.status_a2a(task_id)  # already stopped: no waiting needed
            else:
                claims = self.maestro._claims(task_id)
                if not claims and not (self.state_dir / "tasks" / task_id).is_dir():
                    raise KeyError(f"Unknown task reference {task_id!r}")
                # A task without a live record is not run by this daemon, so no
                # event for it may ever come here: _durable_state resolves an
                # unresolved state (unknown → failed), and the durable record is
                # polled below in case another process is still running it.
                state = _durable_state(claims, _runtime_from_claims(claims))
                if state in stop_states:
                    return self.status_a2a(task_id)  # durable terminal from an earlier run
            threshold = sub.start_seq
            predicate = lambda e: e.task_id == task_id and e.type == "state" and e.data.get("state") in stop_states and e.seq > threshold  # noqa: E731
            if record is not None:
                sub.wait(predicate=predicate, timeout=timeout)
            else:
                self._wait_durable(task_id, sub, predicate, timeout, stop_states)
        finally:
            sub.close()
        return self.status_a2a(task_id)

    def _wait_durable(self, task_id: str, sub: Any, predicate: Callable[[TaskEvent], bool], timeout: float | None, stop_states: tuple[str, ...]) -> None:
        """Wait for a task this daemon does not run, until it stops or ``timeout`` passes.

        Another process may be running it and writes its state to the durable
        record, so the record is read again every DURABLE_POLL_S seconds. An
        event from this daemon (when it takes the task over, for example by an
        answer) ends the wait at once.
        """
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            slice_s = DURABLE_POLL_S if deadline is None else min(DURABLE_POLL_S, max(0.0, deadline - time.monotonic()))
            if sub.wait(predicate=predicate, timeout=slice_s) is not None:
                return
            record = self._tasks.get(task_id)
            if record is not None:
                state = record["state"]
            else:
                claims = self.maestro._claims(task_id)
                state = _durable_state(claims, _runtime_from_claims(claims))
            if state in stop_states or (deadline is not None and time.monotonic() >= deadline):
                return

    # ------------------------------------------------------------------ views
    def resolve(self, ref: str) -> str:
        ref = str(ref).strip()
        if _TASK_ID_RE.match(ref):
            return ref
        return self.maestro.resolve_task(ref)

    def _registry_workspace(self, task_id: str) -> str | None:
        """Return the workspace in the task's registry record, or None when there is no record."""
        record = next((item for item in self.maestro._registry_records() if str(item.get("task_id")) == task_id), None)
        return (record or {}).get("workspace")

    def final_state_event(self, task_id: str) -> TaskEvent | None:
        """Return a state event for a task that has already finished, or None.

        A task-scoped event stream only carries events that this daemon
        process published and still holds in its replay buffer. A task that
        finished in an earlier daemon run, or whose events have left the
        buffer, would never send its final state, so the stream would wait
        forever. The stream sends this event instead and ends. Returns None
        for a task that is still running or parked, and for an unknown task.
        """
        record = self._tasks.get(task_id)
        if record is not None:
            state, error = record["state"], record.get("error")
        else:
            claims = self.maestro._claims(task_id)
            if not claims:
                return None
            runtime = _runtime_from_claims(claims)
            state, error = _durable_state(claims, runtime), runtime.get("error")
        if state not in TERMINAL_STATES:
            return None
        data: dict[str, Any] = {"state": state}
        if error:
            data["error"] = error
        return TaskEvent(task_id=task_id, type="state", data=data)

    def status_a2a(self, task_id: str) -> dict[str, Any]:
        record = self._tasks.get(task_id)
        if record is not None:
            state = record["state"]
            workspace = record["workspace"]
            branch = record.get("branch")
            origin = record.get("origin_agent")
            target = record.get("target_agent")
            run_dir = record.get("run_dir") or workspace
            title = record.get("title")
            usage = record.get("usage")
            attempts = record.get("attempts") or []
            error = record.get("error")
            gates = record.get("gates")
            bounces = record.get("bounces")
        else:  # durable fallback for tasks from earlier daemon runs
            claims = self.maestro._claims(task_id)
            runtime = _runtime_from_claims(claims)
            # The runtime snapshot's own state wins over the lossy phase claim;
            # a missing/unknown status means the task's process is gone and it
            # can never finish on its own — report FAILED, not WORKING.
            state = _durable_state(claims, runtime)
            # A task migrated from the legacy journal can lack the workspace
            # claim; its registry record still names the workspace. This is
            # the same fallback Maestro.status uses.
            workspace = claims.get("task_workspace") or self._registry_workspace(task_id)
            branch = claims.get("task_branch")
            origin = claims.get("task_origin_agent")
            target = claims.get("task_target_agent")
            run_dir = claims.get("task_run_dir") or workspace
            title = claims.get("task_title")
            usage = runtime.get("usage")
            attempts = runtime.get("attempts") or []
            error = runtime.get("error")
            gates = runtime.get("gates")
            bounces = runtime.get("bounces")
        artifacts: list[dict[str, Any]] = []
        task_dir = self.state_dir / "tasks" / task_id
        if task_dir.is_dir():
            for path in sorted(task_dir.iterdir()):
                if path.is_file() and path.suffix in {".json", ".txt", ".log"}:
                    artifacts.append({"artifactId": f"{task_id}:{path.name}", "name": path.name, "parts": [{"kind": "url", "url": str(path)}]})
        metadata = {
            "workspace": workspace,
            # Where the task's work is: the workspace, or the task's worktree.
            "run_dir": run_dir,
            "branch": branch,
            "origin_agent": origin,
            "target_agent": target,
            "title": title,
            "usage": usage,
            "attempts": attempts,
            "error": error,
        }
        run_dir_kind = record.get("run_dir_kind") if record is not None else claims.get("task_run_dir_kind")
        if run_dir_kind == "worktree" and run_dir and not Path(run_dir).is_dir():
            # Removed by task cleanup or deleted by hand; the next turn creates it again.
            metadata["run_dir_missing"] = True
        if gates is not None:
            metadata["gates"] = gates
        if bounces is not None:
            metadata["bounces"] = bounces
        return {
            "kind": "task",
            "id": task_id,
            "status": {"state": state, "timestamp": utcnow_iso()},
            "artifacts": artifacts,
            "metadata": metadata,
        }

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            live = [self.status_a2a(tid) for tid in self._tasks]
        durable_ids = {str(item.get("task_id")) for item in self.maestro._registry_records()} - set(self._tasks)
        for tid in sorted(durable_ids):
            try:
                live.append(self.status_a2a(tid))
            except Exception:
                continue
        return live

    # ------------------------------------------------------------- agent card
    def card(self) -> dict[str, Any]:
        skills = [
            {"id": spec.name, "name": spec.display_name or spec.name, "description": f"Delegable agent ({spec.kind})", "tags": list(spec.skills)}
            for spec in self.registry.list()
        ]
        card = agent_card(name="maestro-node", url=f"http://{self.advertised_host}:{self.port or 0}", skills=skills)
        # Which process serves this card and for which state directory. A
        # daemon.json marker from an older version has no owner lock, so
        # ``maestro daemon status/stop`` confirm its pid through these fields.
        card["maestro"] = {"pid": os.getpid(), "state_dir": str(self.state_dir)}
        return card

    def agents(self) -> list[dict[str, Any]]:
        """Every registered agent (tokens redacted) with its live availability."""
        return [{**spec.to_dict(redact=True), "status": self.registry.status(spec.name)} for spec in self.registry.list()]

    def current_state(self, task_id: str) -> str | None:
        """The task's state without building its full status, or None for an unknown task."""
        record = self._tasks.get(task_id)
        if record is not None:
            return str(record["state"])
        claims = self.maestro._claims(task_id)
        return _durable_state(claims, _runtime_from_claims(claims)) if claims else None


def record_transcript(record: dict[str, Any] | None) -> list[dict[str, str]]:
    if not record:
        return []
    return list(record.get("transcript") or [])


def record_transcript_append(record: dict[str, Any] | None, question: str) -> None:
    if record is not None:
        record.setdefault("transcript", []).append({"question": question, "answer": ""})


def _requested_branch(record: dict[str, Any]) -> str | None:
    """The branch name the task's recorded handoff asks for, or None.

    Read straight from the record, because a branch rename for a task with no
    branch yet changes only ``record["doc"]["expectations"]["branch"]``.
    """
    expectations = (record.get("doc") or {}).get("expectations") or {}
    return expectations.get("branch") or None


def record_transcript_answer(record: dict[str, Any] | None, answer: str, question: str | None = None) -> None:
    """Record ``answer`` against the open question in the transcript.

    When no entry is waiting for an answer (the task was parked by a gate or
    for approval, or its transcript was lost in a restart), a new entry holding
    ``question`` and the answer is added, so the answer still reaches the
    agent's next prompt.
    """
    if record is None:
        return
    transcript = record.setdefault("transcript", [])
    for entry in reversed(transcript):
        if not entry.get("answer"):
            entry["answer"] = answer
            return
    transcript.append({"question": question or "", "answer": answer})


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _host_name(value: str) -> str:
    """The host part of a Host header or an Origin's netloc, without the port."""
    value = value.strip().lower()
    if value.startswith("["):  # [::1]:8790
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _normalize_origin(value: str) -> str:
    """An allowed origin as a browser sends it: ``scheme://host[:port]``, lower case.

    Raises ValueError for anything else, so a typo fails at daemon start
    instead of silently refusing the proxy's requests.
    """
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"Invalid allowed origin {value!r}: expected scheme://host[:port], for example https://maestro.example.com")
    return f"{parts.scheme}://{parts.netloc}".lower()


def _allowed_origins_from_env() -> list[str]:
    """``MAESTRO_DAEMON_ALLOWED_ORIGINS``: a comma-separated list of origins."""
    raw = os.environ.get("MAESTRO_DAEMON_ALLOWED_ORIGINS", "")
    return [item for item in (part.strip() for part in raw.split(",")) if item]


# How often a wait for a task this daemon does not run reads the task's durable record.
DURABLE_POLL_S = 0.5

MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024  # a JSON-RPC request larger than 8 MiB is refused unparsed
# After refusing a body, the handler reads and discards up to this much of it
# before closing. Closing a socket with unread data makes the kernel send a
# reset, and the client then sees "connection reset" instead of the refusal.
MAX_DRAIN_BYTES = 64 * 1024 * 1024
DRAIN_TIMEOUT_S = 5.0  # the drain stops when the client sends nothing for this long


def _make_handler(daemon: MaestroDaemon) -> type[BaseHTTPRequestHandler]:
    dispatcher = A2ADispatcher(daemon)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep test output clean
            pass

        def _authorized(self) -> bool:
            """Bearer-token check for non-loopback binds (``daemon.token``).

            Accepts the ``Authorization: Bearer <token>`` header or a
            ``?token=`` query parameter — EventSource (used by the web console
            and terminal dashboards) cannot set custom headers. Static console
            assets are public; every data/mutation endpoint is gated. Tokens
            are compared in constant time.
            """
            if daemon.token is None:
                return True
            expected = daemon.token.encode("utf-8")
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer ") and hmac.compare_digest(header[len("Bearer "):].strip().encode("utf-8"), expected):
                return True
            query = parse_qs(urlsplit(self.path).query)
            return bool(query.get("token")) and hmac.compare_digest(query["token"][0].encode("utf-8"), expected)

        def _host_allowed(self) -> bool:
            """Refuse requests addressed to a name other than this machine.

            A loopback daemon has no token, so the Host header is what stops a
            DNS-rebinding web page: the page's own domain, re-pointed at
            127.0.0.1, would otherwise read tasks and live agent output. Only
            loopback names are accepted there. A daemon with a token is reached
            by LAN addresses and host names that cannot be listed in advance,
            and the token already protects it, so any Host is accepted.
            """
            if daemon.token is not None:
                return True
            host = self.headers.get("Host")
            return host is None or _host_name(host) in _LOOPBACK_HOSTS

        def _post_allowed(self) -> str | None:
            """Why a POST must be refused, or None when it may proceed.

            A web page can send a cross-site POST without asking first only
            when its Content-Type is a plain form or text type, so requiring
            application/json forces the browser to ask (a CORS preflight),
            which this server never approves. A request that carries an Origin
            header must also come from exactly this server: the origin's host
            and port must equal the Host header. Behind a reverse proxy that
            rewrites Host, the proxy's public origin must be listed in
            ``daemon.allowed_origins``. A request with no Origin header comes
            from a program, not a web page, and is not checked.
            """
            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return "Content-Type must be application/json"
            origin = self.headers.get("Origin")
            if origin is None:
                return None
            if urlsplit(origin).netloc.lower() == (self.headers.get("Host") or "").strip().lower():
                return None
            if origin.strip().lower() in daemon.allowed_origins:
                return None
            return "cross-origin requests are not allowed"

        def _send_json(self, code: int, obj: dict[str, Any]) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _refuse_body(self, code: int, obj: dict[str, Any], declared: int | None) -> None:
            """Send a refusal for a request body that will not be read, then close cleanly.

            The reply goes out first and the write side is shut down, so the
            client can read it at once. The body is then read and thrown away,
            ``declared`` bytes of it (or, when the length is unknown, until the
            client stops sending), never more than MAX_DRAIN_BYTES and never
            waiting more than DRAIN_TIMEOUT_S for the next chunk. Without this,
            closing the socket with the body unread makes the kernel reset the
            connection, and a client still sending sees a broken pipe or a
            connection reset instead of this reply.
            """
            import socket

            self.close_connection = True
            self._send_json(code, obj)
            try:
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(DRAIN_TIMEOUT_S)
                remaining = MAX_DRAIN_BYTES if declared is None else min(declared, MAX_DRAIN_BYTES)
                while remaining > 0:
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        break  # the client closed its side
                    remaining -= len(chunk)
            except OSError:
                pass  # timeout or reset: stop draining, the reply is already out

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if not self._host_allowed():
                self._send_json(403, {"error": "forbidden host"})
                return
            if path in ("/", "/index.html", "/console.js"):
                from .dashboard import console_asset

                asset = console_asset(path)
                if asset is None:
                    self._send_json(404, {"error": "not found"})
                    return
                content_type, body = asset
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            if path == "/.well-known/agent.json":
                self._send_json(200, daemon.card())
                return
            if path == "/tasks":
                self._send_json(200, {"tasks": daemon.list_tasks()})
                return
            if path.startswith("/tasks/") and path.endswith("/receipt"):
                from .receipt import build_receipt

                task_id = path[len("/tasks/") : -len("/receipt")]
                try:
                    resolved = daemon.resolve(task_id)
                except KeyError as exc:
                    self._send_json(404, {"error": str(exc)})
                    return
                known = (
                    resolved in daemon._tasks
                    or any(str(x.get("task_id")) == resolved for x in daemon.maestro._registry_records())
                    or bool(daemon.maestro.mem.history(f"maestro:task:{resolved}", "task_status"))
                )
                if not known:
                    self._send_json(404, {"error": f"Unknown task reference {resolved!r}"})
                    return
                self._send_json(200, build_receipt(resolved, daemon.maestro, daemon.state_dir))
                return
            if path == "/events":
                self._sse(None)  # global stream: every task, never terminates on its own
                return
            if path.startswith("/tasks/") and path.endswith("/events"):
                task_id = path[len("/tasks/") : -len("/events")]
                self._sse(task_id)
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != "/":
                self._send_json(404, {"error": "not found"})
                return
            if not self._host_allowed():
                self._send_json(403, {"error": "forbidden host"})
                return
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            refusal = self._post_allowed()
            if refusal is not None:
                self._send_json(403, {"error": refusal})
                return
            # Check the declared size before parsing anything: a negative length
            # would make the read wait until the client hangs up, and a huge one
            # would be read into memory. The refused body is read in chunks and
            # thrown away (see _refuse_body), never parsed as a request, and the
            # connection is closed after the reply.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._refuse_body(400, {"error": "Content-Length must be a non-negative integer"}, None)
                return
            if length > MAX_REQUEST_BODY_BYTES:
                self._refuse_body(413, {"error": f"request body is larger than the {MAX_REQUEST_BODY_BYTES}-byte limit"}, length)
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                return
            response = dispatcher.handle(body)
            if "result" in response:
                code = 200
            else:
                code = 500 if response["error"]["code"] == ERR_INTERNAL else 400
            self._send_json(code, response)

        def _sse(self, task_id: str | None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            # A client that stops reading must not hold this thread or grow a
            # queue forever: writes give up after sse_write_timeout_s, and a
            # client more than sse_queue_max events behind is disconnected.
            # Before that it is told why (see _overflowed below), because the
            # events it missed are not all in the bus's replay buffer, which
            # keeps only the newest 1000 events.
            self.connection.settimeout(daemon.sse_write_timeout_s)
            sub = daemon.bus.subscribe(maxsize=daemon.sse_queue_max)
            # A task stream starts at the task's latest turn (its most recent
            # "submitted" event). Replaying earlier turns would end the stream
            # at the previous turn's terminal event while the new turn runs.
            turn_start = 0
            if task_id is not None:
                submitted = [
                    e.seq for e in daemon.bus.history(task_id=task_id, types=("state",))
                    if e.seq <= sub.start_seq and e.data.get("state") == STATE_SUBMITTED
                ]
                turn_start = submitted[-1] if submitted else 0
            try:
                if task_id is not None:
                    # The subscription replays the buffer, so a final state
                    # event still in it ends the loop below. When it is not
                    # there, send the task's final state now and end, or the
                    # stream would wait for an event that never comes.
                    replayed = daemon.bus.history(task_id, ("state",))
                    if not (replayed and replayed[-1].data.get("state") in TERMINAL_STATES):
                        final = daemon.final_state_event(task_id)
                        if final is not None:
                            self.wfile.write(sse_encode(final.type, final.to_dict()).encode("utf-8"))
                            self.wfile.flush()
                            return
                while True:
                    if sub.overflowed:
                        self._overflowed(task_id)
                        break
                    event = sub.get(timeout=daemon.sse_heartbeat_s)
                    if event is None:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if task_id is not None and (event.task_id != task_id or event.seq < turn_start):
                        continue
                    self.wfile.write(sse_encode(event.type, event.to_dict()).encode("utf-8"))
                    self.wfile.flush()
                    if task_id is not None and event.type == "state" and event.data.get("state") in TERMINAL_STATES:
                        break
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                sub.close()

        def _overflowed(self, task_id: str | None) -> None:
            """Tell a subscriber that fell too far behind why its stream ends.

            A task stream whose task has finished meanwhile gets the final
            state, which ends it normally. Otherwise the stream sends an
            ``overflow`` event: the subscriber has missed events and must
            re-subscribe, or ask for the task with ``tasks/get``, to learn how
            the task ends. Maestro's own clients (``maestro task tail`` and the
            a2a_remote adapter) re-subscribe.
            """
            if task_id is not None:
                final = daemon.final_state_event(task_id)
                if final is not None:
                    self.wfile.write(sse_encode(final.type, final.to_dict()).encode("utf-8"))
                    self.wfile.flush()
                    return
            data: dict[str, Any] = {"reason": "subscriber fell too far behind; events were dropped. Re-subscribe to keep following."}
            if task_id is not None:
                data["state"] = daemon.current_state(task_id)
            event = TaskEvent(task_id=task_id or "", type="overflow", data=data)
            self.wfile.write(sse_encode(event.type, event.to_dict()).encode("utf-8"))
            self.wfile.flush()

    return Handler


_instance: "MaestroDaemon | DaemonClient | None" = None
_instance_kwargs: dict[str, Any] = {}
_instance_lock = threading.Lock()
SHUTDOWN_LOCK_WAIT_S = 5.0  # shutdown_daemon may run in a signal handler; it never waits longer
# How long a call waits for a daemon that owns the state directory but does
# not answer, before it reports an error. Nothing runs locally meanwhile.
OWNER_RETRY_S = 20.0
# Set by the MCP server's main(): when no daemon owns the state directory,
# start a detached background daemon (as 'maestro daemon start' does) and
# forward to it, instead of running the daemon inside this process.
BACKGROUND_OWNER = False


def get_daemon(**kwargs: Any) -> "MaestroDaemon | DaemonClient":
    """Process-wide daemon for the MCP tools (and anything else in this process).

    Only one daemon runs tasks for a state directory, and this process never
    runs tasks beside it. When a daemon already owns the directory and answers
    HTTP (for example one that ``maestro daemon start`` launched), this returns
    a :class:`~maestro.daemon_client.DaemonClient` that sends every call to
    that owner. When no daemon owns it, a daemon is started: with
    ``BACKGROUND_OWNER`` (the MCP server) a detached background daemon, which
    outlives this process, and otherwise one inside this process. When a
    background daemon cannot be started, the daemon runs inside this process
    as a fallback, and a note on stderr says so.

    Each call checks that the owner still answers. When the owner still holds
    the directory but does not answer, the call waits and checks again, with
    growing pauses, for up to OWNER_RETRY_S seconds, and then raises
    :class:`~maestro.daemon_client.DaemonUnavailable`. Only when the owner is
    gone (no process holds the directory) is a new daemon started.
    """
    global _instance, _instance_kwargs
    with _instance_lock:
        if _instance is not None and not isinstance(_instance, DaemonClient):
            return _instance  # this process runs the daemon that owns the directory
        if _instance is not None:
            if _instance.alive():
                return _instance
            print(f"maestro: the Maestro daemon at {_instance.url} stopped answering; looking for its replacement.", file=sys.stderr, flush=True)
            _instance = None
        else:
            _instance_kwargs = dict(kwargs)
        _instance = _connect(_instance_kwargs)
        return _instance


def _client_for(state_dir: Path, info: daemonctl.DaemonInfo, kwargs: dict[str, Any]) -> "DaemonClient":
    client = DaemonClient(state_dir, str(info.url), info.token, info.pid, fallback=lambda give_up_at=None: None)
    client._fallback = lambda give_up_at=None: _replace_client(client, give_up_at)
    client._legacy = lambda: _use_legacy_embedded(state_dir, info, kwargs)
    return client


def _is_legacy_owner(info: daemonctl.DaemonInfo) -> bool:
    """True when the owner's agent card has no ``maestro`` block: it is from 0.12.0 or earlier.

    Such a daemon lacks the JSON-RPC methods an MCP server forwards its tools
    with (tasks/delegate, tasks/wait, tasks/resolve, tasks/answer,
    agents/list). A card that cannot be read is not taken as proof of age;
    a forwarded call that gets "Method not found" catches that case.
    """
    import urllib.request

    request = urllib.request.Request(f"{info.url}/.well-known/agent.json")
    if info.token:
        request.add_header("Authorization", f"Bearer {info.token}")
    try:
        with urllib.request.urlopen(request, timeout=3) as resp:
            card = json.loads(resp.read())
    except daemonctl._PORT_ERRORS:
        return False
    return isinstance(card, dict) and "maestro" not in card


def _legacy_embedded(state_dir: Path, info: daemonctl.DaemonInfo, kwargs: dict[str, Any]) -> "MaestroDaemon":
    """A daemon in this process, beside an older owner, without taking the directory over."""
    print(
        f"maestro: an older Maestro daemon (pid {info.pid}, {info.url}) owns the state directory {state_dir} and "
        "cannot take calls from this MCP server. This MCP server runs its own tasks without an HTTP endpoint, as "
        "older versions did, so that daemon cannot see or cancel them. Restart that daemon with "
        "'maestro daemon restart', or close the sessions that still use the older version, to share one daemon again.",
        file=sys.stderr,
        flush=True,
    )
    return MaestroDaemon(**{**kwargs, "start_http": False})


def _use_legacy_embedded(state_dir: Path, info: daemonctl.DaemonInfo, kwargs: dict[str, Any]) -> "MaestroDaemon | DaemonClient":
    """Switch this process to a daemon of its own after the owner answered "Method not found"."""
    global _instance
    with _instance_lock:
        if _instance is None or isinstance(_instance, DaemonClient):
            _instance = _legacy_embedded(state_dir, info, kwargs)
        return _instance


def _connect(kwargs: dict[str, Any], give_up_at: float | None = None) -> "MaestroDaemon | DaemonClient":
    """The daemon to use for ``kwargs['state_dir']``: the answering owner, or a newly started one.

    An owner from an older version gets a daemon in this process beside it
    instead (see _legacy_embedded). Raises DaemonUnavailable when, for
    OWNER_RETRY_S seconds (or until ``give_up_at``, if that is sooner), the
    directory is held by a daemon that does not answer, or no daemon could be
    started or reached.
    """
    import time

    from .daemon_client import DaemonUnavailable

    raw = kwargs.get("state_dir")
    state_dir = Path(raw).expanduser() if raw else maestro_user_dir()
    deadline = time.monotonic() + OWNER_RETRY_S
    if give_up_at is not None:
        deadline = min(deadline, give_up_at)
    pause = 0.1
    while True:
        info = daemonctl.live_owner(state_dir)
        if info is not None and info.running and info.url:
            if _is_legacy_owner(info):
                return _legacy_embedded(state_dir, info, kwargs)
            return _client_for(state_dir, info, kwargs)
        if info is None:
            started = _start_owner(state_dir, kwargs)
            if started is not None:
                return started
        if time.monotonic() >= deadline:
            if info is not None:
                raise DaemonUnavailable(unanswered_message(info.pid, state_dir, info.url))
            raise DaemonUnavailable(f"could not start or reach a Maestro daemon for the state directory {state_dir}; see {state_dir / 'daemon.log'}")
        time.sleep(pause)
        pause = min(pause * 2, 2.0)


def _start_owner(state_dir: Path, kwargs: dict[str, Any]) -> "MaestroDaemon | DaemonClient | None":
    """Start the daemon that will own ``state_dir``; None when another daemon took it first.

    ``kwargs`` are the MaestroDaemon arguments for a daemon inside this process.
    """
    if BACKGROUND_OWNER:
        try:
            info = daemonctl.start(state_dir)
        except (RuntimeError, TimeoutError, OSError) as exc:
            print(
                f"maestro: could not start a background daemon ({exc}). This MCP server runs the daemon in its own "
                "process instead; its tasks stop when this MCP server exits.",
                file=sys.stderr,
                flush=True,
            )
        else:
            return _client_for(state_dir, info, kwargs)
    try:
        return MaestroDaemon(**kwargs)
    except daemonctl.DaemonAlreadyRunning:
        return None  # another daemon took the directory first; the caller connects to it


def _replace_client(client: "DaemonClient", give_up_at: float | None = None) -> "MaestroDaemon | DaemonClient":
    """The daemon to use after ``client``'s owner stopped answering (see DaemonClient.wait).

    ``give_up_at`` is the waiting caller's deadline; the search stops there.
    """
    global _instance
    with _instance_lock:
        if _instance is client or _instance is None:
            _instance = None
            _instance = _connect(_instance_kwargs, give_up_at)
        return _instance


def shutdown_daemon() -> None:
    """Stop this process's daemon, if it runs one, releasing its marker and locks.

    The MCP server calls this when it exits (normally, or on SIGTERM). A
    daemon left running would keep "working" tasks that no process drives
    any more. A client of another process's daemon has nothing to stop.
    """
    global _instance
    locked = _instance_lock.acquire(timeout=SHUTDOWN_LOCK_WAIT_S)
    try:
        instance, _instance = _instance, None
    finally:
        if locked:
            _instance_lock.release()
    if isinstance(instance, MaestroDaemon):
        instance.stop()

