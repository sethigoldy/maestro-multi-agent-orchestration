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
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
from .agents import AgentRegistry, AgentSpec
from .core import Maestro, maestro_user_dir
from .events import EventBus, TaskEvent, utcnow_iso
from .handoff import HandoffDoc
from .models import Phase
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


def build_prompt(doc: HandoffDoc, task_id: str, workspace: Path, transcript: list[dict[str, str]]) -> str:
    design = doc.design or "(none — use your judgment within the request's scope)"
    context_files = ", ".join(doc.context_files) if doc.context_files else "(none)"
    qa = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in transcript) or "(first turn)"
    return f"""You are the implementation agent for a Maestro multi-agent task.

Task ID: {task_id}
Workspace: {workspace} (work only inside this directory)

TITLE: {doc.title}

REQUEST:
{doc.request}

AUTHORITATIVE DESIGN:
{design}

CONTEXT NOTES:
{doc.context_notes or "(none)"}
CONTEXT FILES: {context_files}

EXPECTATIONS: artifacts: {", ".join(doc.artifacts)}; verification: {doc.verification}; commit policy: {doc.commit_policy}

PREVIOUS Q&A (if any):
{qa}

Report back with: files changed, commands run and their results, deviations from the design, and remaining issues.
"""


class MaestroDaemon:
    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        start_http: bool = True,
        port: int = 0,
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
        self._httpd: ThreadingHTTPServer | None = None
        self._stopped = False
        self.sse_heartbeat_s = 15.0  # keepalive interval for /tasks/<id>/events streams
        if start_http:
            self.start_http(port)

    # ------------------------------------------------------------------ http
    def start_http(self, port: int = 0) -> int:
        if self._httpd is not None:
            return self.port or 0
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.port = self._httpd.server_address[1]
        (self.state_dir / "daemon.json").write_text(
            json.dumps({"pid": os.getpid(), "port": self.port, "started_at": utcnow_iso()}, indent=2), encoding="utf-8"
        )
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        return self.port

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
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

    # ------------------------------------------------------------- delegation
    def default_target(self) -> str:
        return "codex"

    def delegate(self, doc: HandoffDoc, workspace: str | Path) -> dict[str, Any]:
        from .handoff import validate_handoff

        doc = validate_handoff(doc)
        if doc.max_depth_remaining <= 0:
            raise ValueError("Max delegation depth exceeded; refusing to nest further")
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {ws}")
        key = str(ws)
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
        with self.maestro._task_lock():
            number = self.maestro._new_task_number()
            self.maestro._register_task(task_id, doc.title, number)
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
        self.bus.publish(TaskEvent(task_id=task_id, type="state", data={"state": state, **{k: v for k, v in data.items() if v is not None}}))

    def _run_task(self, task_id: str, doc: HandoffDoc, workspace: Path) -> None:
        self._set_state(task_id, STATE_WORKING)
        branch = self._prepare_branch(workspace, task_id, doc.commit_policy)
        if branch:
            record = self._tasks.get(task_id)
            if record is not None:
                record["branch"] = branch
            self.maestro._write_claim(task_id, "task_branch", branch)
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
                prompt = build_prompt(doc, task_id, workspace, record_transcript(self._tasks.get(task_id)))
                cancel_flag = self._cancel_flags.get(task_id)

                def _on_line(line: str, _task_id: str = task_id) -> None:
                    self.bus.publish(TaskEvent(task_id=_task_id, type="output", data={"agent": agent_name, "line": line}))

                result = adapter.run(
                    prompt,
                    workspace,
                    task_id,
                    settings={**{k: v for k, v in spec.to_dict().items() if v is not None and k in {"model", "effort"}}, **doc.agent_settings},
                    timeout=spec.timeout_s,
                    log_dir=self.state_dir / "tasks" / task_id,
                    on_line=_on_line,
                    should_cancel=(lambda: cancel_flag.is_set()) if cancel_flag is not None else None,
                )
                self._record_attempt(task_id, agent_name, result)
                (self.state_dir / "tasks" / task_id).mkdir(parents=True, exist_ok=True)
                (self.state_dir / "tasks" / task_id / f"result-{agent_name}-{attempt}.json").write_text(
                    json.dumps(result.to_dict(), indent=2), encoding="utf-8"
                )
                if result.usage:
                    self.bus.publish(TaskEvent(task_id=task_id, type="usage", data={"agent": agent_name, **result.usage}))
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

    def _record_attempt(self, task_id: str, agent_name: str, result: Any, error: str | None = None) -> None:
        record = self._tasks.get(task_id)
        if record is None:
            return
        entry = {"agent": agent_name, "ok": bool(result.ok) if result else False}
        if result is not None:
            entry["exit_code"] = result.exit_code
            entry["duration_s"] = round(result.duration_s, 3)
        if error:
            entry["error"] = error
        record.setdefault("attempts", []).append(entry)

    def _post_complete(self, task_id: str, doc: HandoffDoc, workspace: Path, agent_name: str, result: Any) -> None:
        self._set_state(task_id, STATE_WORKING, verifying=True)
        verification_ok: bool | None = None
        if doc.verification != "none":
            verification_ok = self._verify(workspace, task_id, doc)
        record = self._tasks.get(task_id)
        if record is not None:
            record["result"] = result.to_dict()
            record["agent"] = agent_name
            record["usage"] = result.usage
        self.maestro._write_claim(task_id, "task_result", str(result.output_path or ""))
        self._set_state(task_id, STATE_COMPLETED, verification="PASSED" if verification_ok else ("FAILED" if verification_ok is False else "skipped"))
        self._release(task_id)

    def _verify(self, workspace: Path, task_id: str, doc: HandoffDoc) -> bool:
        import shlex

        configured = None
        if doc.verification == "command":
            configured = shlex.split(doc.request)  # explicit command mode carries the command in request (M2 simplification)
        test_cmd, note = _verification_command(workspace, configured)
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
        note_text = f"verification note: {note}\n\n" if note else ""
        report = (
            f"workspace: {workspace}\nverification command: {' '.join(test_cmd)}\n\n{note_text}"
            f"git diff --check:\n{diff.stdout}\n{diff.stderr}\n\nverification:\n{tests.stdout}\n{tests.stderr}"
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
        record = self._tasks.get(task_id)
        if record is None:
            raise KeyError(f"Unknown task reference {task_id!r}")
        if record["state"] != STATE_INPUT_REQUIRED:
            raise ValueError(f"Task {task_id} is not awaiting input (state={record['state']})")
        if not str(answer).strip():
            raise ValueError("Answer cannot be empty")
        doc = self._doc_from_record(record)
        workspace = Path(record["workspace"])
        record_transcript_answer(self._tasks.get(task_id), answer)
        thread = threading.Thread(target=self._run_task, args=(task_id, doc, workspace), daemon=True)
        thread.start()
        return {"task_id": task_id, "state": STATE_WORKING}

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
        record = self._tasks.get(task_id)
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
            state = _STATE_BY_PHASE.get(str(claims.get("task_status")), STATE_WORKING)
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

    def status_a2a(self, task_id: str) -> dict[str, Any]:
        record = self._tasks.get(task_id)
        if record is not None:
            state = record["state"]
            workspace = record["workspace"]
            branch = record.get("branch")
            origin = record.get("origin_agent")
            target = record.get("target_agent")
            title = record.get("title")
        else:  # durable fallback for tasks from earlier daemon runs
            claims = self.maestro._claims(task_id)
            state = _STATE_BY_PHASE.get(str(claims.get("task_status")), STATE_WORKING)
            workspace = claims.get("task_workspace")
            branch = claims.get("task_branch")
            origin = claims.get("task_origin_agent")
            target = claims.get("task_target_agent")
            title = claims.get("task_title")
        artifacts: list[dict[str, Any]] = []
        task_dir = self.state_dir / "tasks" / task_id
        if task_dir.is_dir():
            for path in sorted(task_dir.iterdir()):
                if path.is_file() and path.suffix in {".json", ".txt", ".log"}:
                    artifacts.append({"artifactId": f"{task_id}:{path.name}", "name": path.name, "parts": [{"kind": "url", "url": str(path)}]})
        return {
            "kind": "task",
            "id": task_id,
            "status": {"state": state, "timestamp": utcnow_iso()},
            "artifacts": artifacts,
            "metadata": {
                "workspace": workspace,
                "branch": branch,
                "origin_agent": origin,
                "target_agent": target,
                "title": title,
            },
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
        return agent_card(name="maestro-node", url=f"http://127.0.0.1:{self.port or 0}", skills=skills)


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

        def _send_json(self, code: int, obj: dict[str, Any]) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/.well-known/agent.json":
                self._send_json(200, daemon.card())
                return
            if self.path.startswith("/tasks/") and self.path.endswith("/events"):
                task_id = self.path[len("/tasks/") : -len("/events")]
                self._sse(task_id)
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/":
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                return
            response = dispatcher.handle(body)
            self._send_json(200 if "result" in response else 400, response)

        def _sse(self, task_id: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sub = daemon.bus.subscribe()
            try:
                while True:
                    event = sub.get(timeout=daemon.sse_heartbeat_s)
                    if event is None:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if event.task_id != task_id:
                        continue
                    self.wfile.write(sse_encode(event.type, event.to_dict()).encode("utf-8"))
                    self.wfile.flush()
                    if event.type == "state" and event.data.get("state") in TERMINAL_STATES:
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
