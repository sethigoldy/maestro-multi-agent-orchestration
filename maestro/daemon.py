"""The Maestro daemon: local broker with an event-driven core.

Owns the agent registry, the task lifecycle (A2A-aligned states), the per-workspace
queue (one active task per workspace), retry/failover chains, per-task branches,
preflight checks, cancellation, and the A2A HTTP surface (Agent Card + JSON-RPC +
SSE). Everything is event-driven: consumers wait on the bus, nothing polls.
"""

from __future__ import annotations

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
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .a2a import (
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
from .adapters import AdapterNotAvailable, BaseAdapter, make_adapter
from .agents import BUILTIN_ADAPTERS, AgentRegistry, AgentSpec
from .context import RenderedContext, compose_context, entry_from_dict, render_context
from .core import Maestro, maestro_user_dir
from .events import EventBus, TaskEvent, utcnow_iso
from .handoff import HandoffDoc
from .knowledge import TaskKnowledge, continuation_budget_chars, estimate_tokens, project_knowledge, render_continuation_block
from .models import Phase
from .modes import DEFAULT_MAX_BOUNCES, expand as expand_mode, resolve_mode
from .worker import _verification_command

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

Your job: fix exactly these issues with the smallest change that resolves them. Do not commit, and do not restructure beyond what the issues require.
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
    ) -> None:
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
        self.port: int | None = None
        self.bind = bind or "127.0.0.1"
        self.token: str | None = None  # set in _resolve_bind when auth is required
        self.advertised_host: str = "127.0.0.1"  # host peers should use to reach us
        self.local_host: str = "127.0.0.1"  # host the local CLI dials (daemon.json marker)
        self._httpd: ThreadingHTTPServer | None = None
        self._presence: Any | None = None
        self._stopped = False
        self.sse_heartbeat_s = 15.0  # keepalive interval for /tasks/<id>/events streams
        self._reconcile_interrupted_tasks()
        if start_http:
            self.start_http(port)

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
        if self._httpd is not None:
            return self.port or 0
        self._resolve_bind()
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.bind, port), handler)
        self.port = self._httpd.server_address[1]
        marker: dict[str, Any] = {"pid": os.getpid(), "port": self.port, "host": self.local_host, "started_at": utcnow_iso()}
        if self.token is not None:
            marker["token"] = self.token
        (self.state_dir / "daemon.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        # P2P presence: announce over UDP so other Maestro nodes find us.
        from .discovery import PeerTable, PresenceServer, discovery_enabled, discovery_interface_from_env, discovery_ttl_from_env

        if discovery_enabled():
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
        if marker.exists():
            try:
                marker.unlink()
            except OSError:
                pass
        self.maestro.close()

    def _reconcile_interrupted_tasks(self) -> None:
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

    def _apply_work_mode(self, doc: HandoffDoc) -> HandoffDoc:
        """Expand a work-mode preset onto the handoff and validate its routing pins.

        Explicit routing fields win over the preset (see maestro/modes.py). After
        expansion: self-review is rejected (review_agent == implementer), every named
        verifier/reviewer/fixer must be known to this daemon, and a mode-provided
        implementer must be known too. Raises ValueError with an actionable message.
        """
        if doc.mode:
            preset = resolve_mode(self.maestro.config.get("modes") or {}, doc.mode)
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

    def _apply_context(self, doc: HandoffDoc) -> HandoffDoc:
        """Compose standing + handoff context entries and validate skill paths.

        The composed list replaces ``doc.context_entries`` so the stored task record
        is self-contained (invariant C2). Skill entries must exist on disk now (C4):
        a missing directory or SKILL.md fails delegation before any agent runs.
        """
        config_entries = self.maestro.config.get("context") or {}
        composed = compose_context(config_entries, doc.context_entries)
        for entry in composed:
            if entry.kind != "skill":
                continue
            path = Path(os.path.expanduser(entry.path or ""))
            if not path.is_dir():
                raise ValueError(f"Skill context {entry.label!r}: directory not found: {path}")
            if not (path / "SKILL.md").is_file():
                raise ValueError(f"Skill context {entry.label!r}: no SKILL.md in {path}")
        doc.context_entries = [e.to_dict() for e in composed]
        return doc

    # ------------------------------------------------- routing defaults ([defaults])
    def _apply_defaults(self, doc: HandoffDoc) -> bool:
        """Fill missing routing from the config ``[defaults]`` table.

        Returns True when a target agent is now resolved (the handoff named one
        explicitly, or ``[defaults].agent`` supplied it). Returns False when the
        handoff names no target and no default is configured — the caller must
        ask the user which agent/model to use instead of guessing.

        ``[defaults].fallback`` fills an empty fallback chain, and
        ``[defaults].model``/``effort`` are applied to the chosen agent when the
        handoff does not set them (explicit handoff values always win).
        """
        defaults = self.maestro.config.get("defaults") or {}
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
        model = defaults.get("model")
        if model and not doc.agent_settings.get("model"):
            doc.agent_settings["model"] = str(model)
        effort = defaults.get("effort")
        if effort and not doc.agent_settings.get("effort"):
            doc.agent_settings["effort"] = str(effort)
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
        doc = self._apply_work_mode(doc)
        doc = self._apply_context(doc)
        routing_resolved = self._apply_defaults(doc)
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
        key = str(ws)
        self._enforce_budgets(doc.target_agent)
        task_id, record = self._make_record(doc, key)
        queued = False
        with self._lock:
            if key in self._active and self._active[key] is not None:
                queued = True
                self._queue.append(task_id)  # FIFO per workspace; starts when the slot frees
            else:
                self._active[key] = task_id
        record["queued"] = queued
        self._register_and_claims(task_id, doc, key)
        if queued:
            return {"task_id": task_id, "queued": True, "state": STATE_SUBMITTED, "ts": utcnow_iso()}
        self._set_state(task_id, STATE_SUBMITTED)
        if doc.sensitive:
            # Governance: sensitive workspaces pause for approval before any agent runs.
            self._set_state(task_id, STATE_INPUT_REQUIRED, question="Approval required: this task targets a sensitive workspace.")
            return {"task_id": task_id, "queued": False, "state": STATE_INPUT_REQUIRED, "ts": utcnow_iso()}
        if not routing_resolved:
            # No explicit target and no [defaults]: ask the user which agent/model
            # to use instead of silently guessing (resume via answer_question).
            record["awaiting"] = "routing"
            self._persist(task_id, doc)
            self._set_state(task_id, STATE_INPUT_REQUIRED, question=self._routing_question())
            return {"task_id": task_id, "queued": False, "state": STATE_INPUT_REQUIRED, "ts": utcnow_iso()}
        thread = threading.Thread(target=self._run_task, args=(task_id, doc, ws), daemon=True)
        thread.start()
        return {"task_id": task_id, "queued": False, "state": STATE_SUBMITTED, "ts": utcnow_iso()}

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
        """Promote a queued handoff now that its workspace is free."""
        from .handoff import from_dict

        record = self._tasks.get(task_id)
        if record is None:
            return
        with self._lock:
            key = record["workspace"]
            if self._active.get(key) not in (None, task_id):
                return  # the slot was taken first by another handoff
            self._active[key] = task_id
            record["queued"] = False
        doc = from_dict(record["doc"])
        ws = Path(key)
        self._set_state(task_id, STATE_SUBMITTED)
        if doc.sensitive:
            self._set_state(task_id, STATE_INPUT_REQUIRED, question="Approval required: this task targets a sensitive workspace.")
            return
        if not self._apply_defaults(doc):
            record["awaiting"] = "routing"
            self._persist(task_id, doc)
            self._set_state(task_id, STATE_INPUT_REQUIRED, question=self._routing_question())
            return
        thread = threading.Thread(target=self._run_task, args=(task_id, doc, ws), daemon=True)
        thread.start()

    def _persist(self, task_id: str, doc: HandoffDoc | None = None) -> None:
        record = self._tasks.get(task_id)
        if record is None:
            return
        snapshot = {k: v for k, v in record.items() if k != "transcript"}
        self.maestro._write_claim(task_id, "task_runtime", json.dumps(snapshot, ensure_ascii=False))
        if doc is not None:
            self.maestro._write_claim(task_id, "task_origin_agent", doc.origin_agent)
            self.maestro._write_claim(task_id, "task_target_agent", doc.target_agent)

    def _set_state(self, task_id: str, state: str, **data: Any) -> None:
        record = self._tasks.get(task_id)
        if record is not None:
            record["state"] = state
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

    def _run_task(self, task_id: str, doc: HandoffDoc, workspace: Path) -> None:
        """Thread entry point for one turn.

        Crash safety: a turn runs in a bare daemon thread, so any uncaught
        exception (adapter spawn failure, disk error writing the result file,
        git/subprocess failure inside verification or the knowledge refresh)
        used to kill the thread silently — the task then reported "working"
        forever with no thread left to drive it to a terminal state, and every
        later handoff to that workspace queued behind the ghost. Any exception
        now ends the task in FAILED (unless it already parked or terminated on
        its own) and frees the workspace slot.
        """
        try:
            self._set_state(task_id, STATE_WORKING)
            self._run_turn(task_id, doc, workspace)
        except Exception as exc:
            self._handle_turn_crash(task_id, exc)

    def _handle_turn_crash(self, task_id: str, exc: BaseException) -> None:
        """Terminal safety net for a crashed turn. Marks the task FAILED and
        frees its workspace slot — unless it already parked (input-required
        holds its slot by design) or reached a terminal state on its own.
        Never raises: this is the last line of defense in a bare thread."""
        record = self._tasks.get(task_id) or {}
        state = record.get("state")
        if state != STATE_INPUT_REQUIRED and state not in TERMINAL_STATES:
            try:
                self._set_state(task_id, STATE_FAILED, error=f"turn crashed before completion: {exc!r}")
                self.bus.publish(TaskEvent(task_id=task_id, type="state", data={"escalation": True, "error": f"turn crashed: {exc!r}"}))
                self._release(task_id)  # idempotent: frees the slot only if this task still owns it
            except Exception:
                traceback.print_exc(file=sys.stderr)
        print(f"[maestro] task {task_id} turn crashed (state={state}):", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)

    def _run_turn(self, task_id: str, doc: HandoffDoc, workspace: Path) -> None:
        record = self._tasks.get(task_id)
        turn = (record.get("turn") or 0) + 1 if record is not None else 1
        if record is not None:
            record["turn"] = turn  # follow-ups/answers are new turns; result files must not collide
        branch = self._prepare_branch(workspace, task_id, doc.commit_policy)
        if branch:
            record = self._tasks.get(task_id)
            if record is not None:
                record["branch"] = branch
            self.maestro._write_claim(task_id, "task_branch", branch)
        base_head = self._base_head(workspace)
        if base_head:
            record = self._tasks.get(task_id)
            if record is not None:
                record["base_head"] = base_head  # per-turn evidence baseline for verification
            self.maestro._write_claim(task_id, "task_base_head", base_head)
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
                rendered = self._rendered_context(doc, task_id, "implementer", spec.kind, workspace)
                prompt = build_prompt(doc, task_id, workspace, record_transcript(self._tasks.get(task_id)), context_block=rendered.block)
                settings = self._with_context_settings(spec.kind, self._turn_settings(spec, doc), rendered)
                cancel_flag = self._cancel_flags.get(task_id)

                def _on_line(line: str, _task_id: str = task_id) -> None:
                    self.bus.publish(TaskEvent(task_id=_task_id, type="output", data={"agent": agent_name, "line": line}))

                result = adapter.run(
                    prompt,
                    workspace,
                    task_id,
                    settings=settings,
                    timeout=spec.timeout_s,
                    log_dir=self.state_dir / "tasks" / task_id,
                    on_line=_on_line,
                    should_cancel=(lambda: cancel_flag.is_set()) if cancel_flag is not None else None,
                )
                self._record_attempt(
                    task_id, agent_name, result,
                    None if result.ok else (result.error or f"agent {agent_name} failed"),
                )
                self._bookkeep_turn(task_id, agent_name, result, turn, attempt)
                record = self._tasks.get(task_id) or {}
                if record.get("state") == STATE_CANCELED:
                    return
                if result.question:
                    record_transcript_append(self._tasks.get(task_id), result.question)
                    self._set_state(task_id, STATE_INPUT_REQUIRED, question=result.question)
                    return
                if result.ok:
                    self._post_complete(task_id, doc, workspace, agent_name, result)
                    return
                last_error = result.error or f"agent {agent_name} failed"
                if attempt < attempts - 1 and self.backoff_s > 0:
                    import time

                    time.sleep(self.backoff_s * (attempt + 1))
        self._set_state(task_id, STATE_FAILED, error=last_error or "all agents in the chain failed")
        self.bus.publish(TaskEvent(task_id=task_id, type="state", data={"escalation": True, "error": last_error}))
        self._release(task_id)

    def _release(self, task_id: str) -> None:
        """Free the workspace slot and start every queued handoff whose workspace is now free."""
        started: list[str] = []
        with self._lock:
            record = self._tasks.get(task_id)
            key = record["workspace"] if record else None
            if key is not None and self._active.get(key) == task_id:
                del self._active[key]
            changed = True
            while changed:
                changed = False
                for queued_id in list(self._queue):
                    qrec = self._tasks.get(queued_id)
                    if qrec is None:
                        self._queue.remove(queued_id)  # stale entry: drop it
                        changed = True
                        break
                    if qrec["workspace"] not in self._active:
                        self._queue.remove(queued_id)
                        started.append(queued_id)
                        changed = True
                        break  # the slot just filled: re-scan from the front (FIFO fairness)
        for queued_id in started:
            self._start_queued(queued_id)

    @staticmethod
    def _turn_settings(spec: AgentSpec, doc: HandoffDoc) -> dict[str, Any]:
        """Adapter settings for one turn: registry defaults, per-task overrides, handoff."""
        return {
            **{k: v for k, v in spec.to_dict().items() if v is not None and k in {"model", "effort", "token"}},
            **doc.agent_settings,
            # Reserved key for api-mode adapters (e.g. a2a_remote) so
            # the full handoff survives daemon-to-daemon hops.
            "maestro_handoff": doc.to_dict(),
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

    def _post_complete(self, task_id: str, doc: HandoffDoc, workspace: Path, agent_name: str, result: Any) -> None:
        self._set_state(task_id, STATE_WORKING, verifying=True)
        verification_ok: bool | None = None
        if doc.verification != "none":
            verification_ok = self._verify(workspace, task_id, doc, output_tail=self._output_tail(result))
        record = self._tasks.get(task_id)
        if record is not None:
            record["result"] = result.to_dict()
            record["agent"] = agent_name
            # usage was already accumulated per attempt in _run_task
        self.maestro._write_claim(task_id, "task_result", str(result.output_path or ""))
        parked = self._gate_cycle(task_id, doc, workspace, verification_ok)
        record = self._tasks.get(task_id)
        if parked or (record is not None and record.get("state") == STATE_CANCELED):
            return  # a parked task keeps its workspace slot; a canceled one already released it
        self._set_state(task_id, STATE_COMPLETED, verification="PASSED" if verification_ok else ("FAILED" if verification_ok is False else "skipped"))
        self._release(task_id)

    def _canceled(self, task_id: str) -> bool:
        record = self._tasks.get(task_id)
        return record is not None and record.get("state") == STATE_CANCELED

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

    def _gate_cycle(self, task_id: str, doc: HandoffDoc, workspace: Path, verification_ok: bool | None) -> bool:
        """Run work-mode gates (LLM verify/review) plus capped auto-fix bounces.

        Returns True when the task was parked in input-required (the caller must not
        mark it completed and must keep the workspace slot). With no gate agents
        configured it returns False immediately — legacy behavior, where completion
        proceeds regardless of the deterministic outcome. A failed deterministic
        check joins the bounce loop only when at least one gate agent is set; an LLM
        verdict can add failures but never override the deterministic result (I1).
        """
        if not doc.verify_agent and not doc.review_agent:
            return False
        record = self._tasks.get(task_id) or {}
        turn = int(record.get("turn") or 1)
        report_path = self.state_dir / "tasks" / task_id / "verification.txt"
        verdicts: dict[str, Any] = {}
        issues: list[str] = []
        det_failed = verification_ok is False

        if doc.verify_agent and doc.verification != "none":
            v = self._gate_turn(task_id, doc, workspace, agent_name=doc.verify_agent, role="verifier", verification_ok=verification_ok, report_path=report_path)
            verdicts["verify"] = {"agent": doc.verify_agent, "ok": v["ok"], "issues": list(v["issues"])}
            if v["parked"]:
                self._write_gates_claim(task_id, verdicts, 0)
                self._set_state(task_id, STATE_INPUT_REQUIRED, question=v["reason"])
                return True
            issues.extend(v["issues"])

        if doc.review_agent:
            r = self._gate_turn(task_id, doc, workspace, agent_name=doc.review_agent, role="reviewer", verification_ok=verification_ok, report_path=report_path, prior_issues=issues)
            verdicts["review"] = {"agent": doc.review_agent, "ok": r["ok"], "issues": list(r["issues"])}
            if r["parked"]:
                self._write_gates_claim(task_id, verdicts, 0)
                self._set_state(task_id, STATE_INPUT_REQUIRED, question=r["reason"])
                return True
            issues.extend(r["issues"])

        max_bounces = doc.max_bounces if doc.max_bounces is not None else DEFAULT_MAX_BOUNCES
        bounces = 0
        while (det_failed or issues) and bounces < max_bounces:
            bounces += 1
            self._set_state(task_id, STATE_WORKING, fixing=True)
            fix_agent = doc.fix_agent or doc.target_agent
            fix_status, fix_error = self._fix_turn(task_id, doc, workspace, fix_agent, issues, det_failed, turn=turn)
            if fix_status == "canceled":
                return False
            if fix_status == "parked":
                self._write_gates_claim(task_id, verdicts, bounces)
                self._set_state(task_id, STATE_INPUT_REQUIRED, question=fix_error or "fixer could not run")
                return True
            verification_ok = self._verify(workspace, task_id, doc) if doc.verification != "none" else None
            det_failed = verification_ok is False
            issues = []
            if fix_status == "failed":
                issues.append(f"previous fix attempt by {fix_agent!r} failed: {fix_error}")
            if doc.review_agent:
                r = self._gate_turn(task_id, doc, workspace, agent_name=doc.review_agent, role="reviewer", verification_ok=verification_ok, report_path=report_path, prior_issues=issues)
                verdicts["review"] = {"agent": doc.review_agent, "ok": r["ok"], "issues": list(r["issues"])}
                if self._canceled(task_id):
                    return False
                if r["parked"]:
                    self._write_gates_claim(task_id, verdicts, bounces)
                    self._set_state(task_id, STATE_INPUT_REQUIRED, question=r["reason"])
                    return True
                issues.extend(r["issues"])
        self._write_gates_claim(task_id, verdicts, bounces)
        if det_failed or issues:
            self._set_state(task_id, STATE_INPUT_REQUIRED, question=self._park_question(issues, det_failed, bounces))
            return True
        return False

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

    def _gate_turn(self, task_id: str, doc: HandoffDoc, workspace: Path, *, agent_name: str, role: str, verification_ok: bool | None, report_path: Path, prior_issues: list[str] | None = None) -> dict[str, Any]:
        """Run one read-only LLM gate turn (verifier or reviewer) and parse its verdict.

        Returns {"ok", "issues", "parked"} — on park, "reason" carries the question
        text; the caller writes the gates claim first and only then sets the task to
        input-required (agent unavailable, failed run, agent question, or unparseable
        verdict). Cancellation is not a park: it returns ok=True/parked=False and the
        caller checks _canceled() before proceeding.
        """
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
        rendered = self._rendered_context(doc, task_id, role, spec.kind, workspace)
        if role == "verifier":
            prompt = build_verify_prompt(doc, task_id, workspace, verification_ok, report_path, context_block=rendered.block)
        else:
            prompt = build_review_prompt(doc, task_id, workspace, verification_ok, report_path, prior_issues or [], context_block=rendered.block)
        settings = self._with_context_settings(spec.kind, self._turn_settings(spec, doc), rendered)
        cancel_flag = self._cancel_flags.get(task_id)

        def _on_line(line: str, _task_id: str = task_id) -> None:
            self.bus.publish(TaskEvent(task_id=_task_id, type="output", data={"agent": agent_name, "line": line}))

        result = adapter.run(
            prompt, workspace, task_id,
            settings=settings,
            timeout=spec.timeout_s,
            log_dir=turn_log_dir,
            on_line=_on_line,
            should_cancel=(lambda: cancel_flag.is_set()) if cancel_flag is not None else None,
        )
        self._record_attempt(task_id, agent_name, result, None if result.ok else (result.error or f"{role} turn failed"), role=role)
        self._bookkeep_turn(task_id, agent_name, result, turn, 0)
        if self._canceled(task_id):
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

    def _fix_turn(self, task_id: str, doc: HandoffDoc, workspace: Path, fix_agent: str, issues: list[str], det_failed: bool, turn: int) -> tuple[str, str | None]:
        """Run one auto-fix bounce under the fix agent.

        Returns (status, error) where status is "ok", "failed" (error holds the
        failure text), "parked" (error holds the park question — the caller writes
        the gates claim and sets input-required), or "canceled".
        """
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
        rendered = self._rendered_context(doc, task_id, "implementer", spec.kind, workspace)
        prompt = build_fix_prompt(doc, task_id, workspace, issues, det_failed, context_block=rendered.block)
        settings = self._with_context_settings(spec.kind, self._turn_settings(spec, doc), rendered)
        cancel_flag = self._cancel_flags.get(task_id)

        def _on_line(line: str, _task_id: str = task_id) -> None:
            self.bus.publish(TaskEvent(task_id=_task_id, type="output", data={"agent": fix_agent, "line": line}))

        result = adapter.run(
            prompt, workspace, task_id,
            settings=settings,
            timeout=spec.timeout_s,
            log_dir=turn_log_dir,
            on_line=_on_line,
            should_cancel=(lambda: cancel_flag.is_set()) if cancel_flag is not None else None,
        )
        self._record_attempt(task_id, fix_agent, result, None if result.ok else (result.error or "fix turn failed"), role="fix")
        self._bookkeep_turn(task_id, fix_agent, result, turn, 0)
        if self._canceled(task_id):
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

        configured = None
        if doc.verification == "command":
            configured = shlex.split(doc.request)  # explicit command mode carries the command in request (M2 simplification)
        test_cmd, note = _verification_command(workspace, configured)
        # The auto-detected fallback is `git diff --check` plus a note. It passes
        # trivially on an untouched workspace, so it must never certify a turn
        # that left no changes behind — otherwise a zero-work turn (e.g. an agent
        # that stopped to ask for approval a batch run can never receive) would
        # complete as PASSED. Explicit commands and real test runners are
        # authoritative and keep their current semantics.
        fallback_only = note is not None and test_cmd == ["git", "diff", "--check"]
        diff = subprocess.run(["git", "diff", "--check"], cwd=workspace, text=True, capture_output=True)
        try:
            tests = subprocess.run(test_cmd, cwd=workspace, text=True, capture_output=True)
        except (OSError, ValueError) as exc:  # command not launchable: treat as a failed verification
            class _FailedRun:
                returncode = 127
                stdout = ""
                stderr = f"verification command could not be launched: {exc}"

            tests = _FailedRun()
        ok = diff.returncode == 0 and tests.returncode == 0
        no_changes_reason: str | None = None
        if ok and fallback_only:
            has_changes, evidence = self._workspace_has_changes(workspace, task_id)
            if not has_changes:
                ok = False
                no_changes_reason = evidence
        note_text = f"verification note: {note}\n\n" if note else ""
        no_changes_text = ""
        if no_changes_reason is not None:
            tail_text = f"\n\nlast lines of the agent's output:\n{output_tail}" if output_tail else ""
            no_changes_text = (
                "\nRESULT: FAILED — no changes detected. "
                f"{no_changes_reason}. The only available check is `git diff --check` "
                "(no project test runner was detected), which passes trivially on an "
                "untouched workspace, so Maestro will not report PASSED without evidence of work."
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

    def _prepare_branch(self, workspace: Path, task_id: str, commit_policy: str) -> str | None:
        if commit_policy == "no-commit":
            return None
        probe = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"], text=True, capture_output=True)
        if probe.returncode != 0:
            return None
        branch = f"maestro/{task_id}"
        created = subprocess.run(["git", "-C", str(workspace), "checkout", "-b", branch], text=True, capture_output=True)
        if created.returncode != 0:
            existing = subprocess.run(["git", "-C", str(workspace), "checkout", branch], text=True, capture_output=True)
            if existing.returncode != 0:
                return None
        return branch

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
        doc = self._doc_from_record(record)
        if record.get("awaiting") == "routing":
            # The task is parked on the routing question: the answer selects the
            # agent/model, then the task starts. A bad answer raises and leaves
            # the task input-required for another attempt.
            doc = self._parse_routing_answer(record, answer)
            record["doc"] = doc.to_dict()
            record["target_agent"] = doc.target_agent
            record.pop("awaiting", None)
            self._persist(task_id, doc)
        elif not self._apply_defaults(doc):
            # Parked for another reason (e.g. sensitive approval) while routing was
            # never resolved: ask now instead of running under a guessed target.
            record["awaiting"] = "routing"
            self._persist(task_id, doc)
            self._set_state(task_id, STATE_INPUT_REQUIRED, question=self._routing_question())
            return {"task_id": task_id, "state": STATE_INPUT_REQUIRED}
        workspace = Path(record["workspace"])
        record_transcript_answer(self._tasks.get(task_id), answer)
        thread = threading.Thread(target=self._run_task, args=(task_id, doc, workspace), daemon=True)
        thread.start()
        return {"task_id": task_id, "state": STATE_WORKING}

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
        workspace_raw = claims.get("task_workspace") or self._registry_workspace(task_id)
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

    def followup(self, task_id: str, instruction: str, context_mode: str = "reuse") -> dict[str, Any]:
        """Resume a finished task (completed/failed/canceled) with a new instruction.

        The same target agent continues on the same task branch. With
        ``context_mode="reuse"`` (default) the turn receives a compact task-
        knowledge snapshot — goal, current state, verification result, known
        issues — instead of re-discovering everything; raw history stays in the
        durable record. ``context_mode="fresh"`` skips the snapshot for a clean
        reasoning context (same task/workspace/branch). Depth is decremented so
        follow-up chains cannot nest forever. Works after a daemon restart: an
        unknown in-memory task is reconstructed from durable claims.
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
        continuation_config = self.maestro.config.get("continuation") or {}
        if context_mode == "reuse" and continuation_config.get("enabled", True):
            knowledge = self._refresh_knowledge(task_id)
            block = render_continuation_block(knowledge, continuation_budget_chars(self.maestro.config)) if knowledge is not None else ""
            if block:
                followup_doc.context_entries.append({"label": "task-knowledge", "kind": "text", "text": block})
            record["context_stats"] = {
                "mode": "reuse",
                "knowledge_chars": len(knowledge.serialize()) if knowledge is not None else 0,
                "context_chars": len(block),
                "raw_history_bytes": raw_history,
                # chars/4 estimate — labeled as an estimate in receipts/docs.
                "estimated_tokens": estimate_tokens(block),
                "reduction_ratio": round(1 - len(block) / raw_history, 3) if (block and raw_history > 0) else None,
            }
        elif context_mode == "fresh":
            record["context_stats"] = {"mode": "fresh", "raw_history_bytes": raw_history}
        record["doc"] = followup_doc.to_dict()  # the chain accumulates: later follow-ups see reduced depth
        record["target_agent"] = followup_doc.target_agent  # a pinned fixer becomes the task's current target
        self._persist(task_id, followup_doc)
        workspace = Path(record["workspace"])
        self._set_state(task_id, STATE_SUBMITTED)
        thread = threading.Thread(target=self._run_task, args=(task_id, followup_doc, workspace), daemon=True)
        thread.start()
        return {"task_id": task_id, "state": STATE_SUBMITTED, "ts": utcnow_iso()}

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
        if record["state"] in TERMINAL_STATES:
            raise ValueError(f"Task {task_id} already finished (state={record['state']})")
        flag = self._cancel_flags.get(task_id)
        if flag is not None:
            flag.set()
        with self._lock:
            if record.get("queued"):
                try:
                    self._queue.remove(task_id)
                except ValueError:
                    pass
        self._set_state(task_id, STATE_CANCELED, reason=reason or "canceled by user")
        if not record.get("queued"):
            self._release(task_id)
        return {"task_id": task_id, "state": STATE_CANCELED}

    def wait(
        self,
        task_id: str,
        timeout: float | None = None,
        stop_states: tuple[str, ...] = TERMINAL_STATES + (STATE_INPUT_REQUIRED,),
    ) -> dict[str, Any]:
        record = self._tasks.get(task_id)
        if record is not None:
            if record["state"] in stop_states:
                return self.status_a2a(task_id)  # already stopped: no waiting needed
        else:
            claims = self.maestro._claims(task_id)
            if not claims and not (self.state_dir / "tasks" / task_id).is_dir():
                raise KeyError(f"Unknown task reference {task_id!r}")
            # A task without a live record belongs to a dead process and can
            # never emit events, so an unresolved state must not block waiters
            # for the full timeout: _durable_state resolves it (unknown → failed).
            state = _durable_state(claims, _runtime_from_claims(claims))
            if state in stop_states:
                return self.status_a2a(task_id)  # durable terminal from an earlier run
        threshold = max((e.seq for e in self.bus.history(task_id=task_id)), default=0)
        sub = self.bus.subscribe()
        try:
            sub.wait(
                predicate=lambda e: e.task_id == task_id and e.type == "state" and e.data.get("state") in stop_states and e.seq > threshold,
                timeout=timeout,
            )
        finally:
            sub.close()
        return self.status_a2a(task_id)

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
            "branch": branch,
            "origin_agent": origin,
            "target_agent": target,
            "title": title,
            "usage": usage,
            "attempts": attempts,
            "error": error,
        }
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
        return agent_card(name="maestro-node", url=f"http://{self.advertised_host}:{self.port or 0}", skills=skills)


def record_transcript(record: dict[str, Any] | None) -> list[dict[str, str]]:
    if not record:
        return []
    return list(record.get("transcript") or [])


def record_transcript_append(record: dict[str, Any] | None, question: str) -> None:
    if record is not None:
        record.setdefault("transcript", []).append({"question": question, "answer": ""})


def record_transcript_answer(record: dict[str, Any] | None, answer: str) -> None:
    if record is None:
        return
    transcript = record.get("transcript") or []
    for entry in reversed(transcript):
        if not entry.get("answer"):
            entry["answer"] = answer
            break


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
            assets are public; every data/mutation endpoint is gated.
            """
            if daemon.token is None:
                return True
            expected = daemon.token
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer ") and header[len("Bearer "):].strip() == expected:
                return True
            query = parse_qs(urlsplit(self.path).query)
            return bool(query.get("token")) and query["token"][0] == expected

        def _send_json(self, code: int, obj: dict[str, Any]) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
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
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                return
            response = dispatcher.handle(body)
            self._send_json(200 if "result" in response else 400, response)

        def _sse(self, task_id: str | None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sub = daemon.bus.subscribe()
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
                    event = sub.get(timeout=daemon.sse_heartbeat_s)
                    if event is None:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if task_id is not None and event.task_id != task_id:
                        continue
                    self.wfile.write(sse_encode(event.type, event.to_dict()).encode("utf-8"))
                    self.wfile.flush()
                    if task_id is not None and event.type == "state" and event.data.get("state") in TERMINAL_STATES:
                        break
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                sub.close()

    return Handler


_instance: MaestroDaemon | None = None
_instance_lock = threading.Lock()


def get_daemon(**kwargs: Any) -> MaestroDaemon:
    """Process-wide daemon singleton (used by MCP tools and the CLI)."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MaestroDaemon(**kwargs)
        return _instance
