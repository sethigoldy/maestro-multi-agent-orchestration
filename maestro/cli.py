from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents import AgentRegistry, AgentSpec, BUILTIN_ADAPTERS, GENERIC_KIND
from .core import Maestro, maestro_user_dir

VERSION = "0.8.4"


def _workspace(value: str | None) -> Path:
    if value is not None and not str(value).strip():
        return Path.home().resolve()
    env_value = os.environ.get("MAESTRO_WORKSPACE")
    if env_value is not None and not env_value.strip() and value is None:
        return Path.home().resolve()
    candidate = value or env_value or str(Path.cwd())
    return Path(candidate).expanduser().resolve()


def _project(value: str | None) -> Path:
    candidate = value or str(Path.cwd())
    path = Path(candidate).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Project does not exist or is not a directory: {path}")
    return path


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--workspace", default=argparse.SUPPRESS,
                       help="Workspace to scope tasks to. Defaults to MAESTRO_WORKSPACE or the current directory.")
    group.add_argument("--project", default=argparse.SUPPRESS,
                       help="Project root to scope tasks to.")


def _normalize_argv(argv: list[str]) -> list[str]:
    known_task_cmds = {"list", "status", "show", "tail", "audit"}
    if len(argv) >= 2 and argv[0] == "task":
        subcommand = argv[1]
        if not subcommand.startswith("-") and subcommand not in known_task_cmds:  # pragma: no branch
            return ["task", "status", *argv[1:]]
    if len(argv) >= 3 and argv[1] == "task":
        subcommand = argv[2]
        if not subcommand.startswith("-") and subcommand not in known_task_cmds:  # pragma: no branch
            return [argv[0], "task", "status", *argv[2:]]
    return argv


def _scope_for_list(args: argparse.Namespace) -> tuple[Path, str | None]:
    if getattr(args, "project", None):
        project = _project(args.project)
        return project, str(project)
    explicit = getattr(args, "workspace", None)
    env_value = os.environ.get("MAESTRO_WORKSPACE")
    workspace = _workspace(explicit)
    if (explicit is not None and not str(explicit).strip()) or (explicit is None and env_value is not None and not env_value.strip()):
        return workspace, None
    project_root = Maestro._resolve_project_root(workspace)
    if explicit or env_value:
        if (workspace / ".claude" / "worktrees").is_dir() or workspace == project_root:
            return workspace, str(project_root)
        return workspace, str(workspace)
    return workspace, None


def _filter_tasks(tasks: list[dict], project_root: str | None = None, workspace: str | None = None) -> list[dict]:
    out = []
    for task in tasks:
        task_project = str(task.get("project_root") or "")
        task_workspace = str(task.get("workspace") or "")
        if project_root is not None and task_project != project_root:
            continue
        if workspace is not None and task_workspace != workspace:
            continue
        out.append(task)
    return out


def _task_workspace(value: str | None) -> Path:
    if value is not None and not str(value).strip():
        return Path.cwd().resolve()

    env_value = os.environ.get("MAESTRO_WORKSPACE")
    if env_value is not None and env_value.strip():
        return Path(env_value).expanduser().resolve()

    return Path.cwd().resolve()


# ---------------------------------------------------------------- daemon clients

def _daemon_endpoint() -> tuple[str, str | None]:
    """Find the local broker and return ``(base_url, token)``.

    Resolution: ``MAESTRO_DAEMON_URL`` (with an optional ``MAESTRO_DAEMON_TOKEN``),
    else the daemon.json marker written by whichever broker started last (a
    standalone ``maestro-daemon`` or an MCP server's embedded daemon). A stale
    marker from a dead process is rejected so callers get a clear error instead
    of a connection failure mid-stream. Non-loopback daemons record their auth
    token in the marker, so local CLI calls are authorized automatically.
    """
    env = os.environ.get("MAESTRO_DAEMON_URL", "").strip()
    if env:
        return env.rstrip("/"), os.environ.get("MAESTRO_DAEMON_TOKEN") or None
    try:
        info = json.loads((maestro_user_dir() / "daemon.json").read_text(encoding="utf-8"))
        pid = int(info["pid"])
        os.kill(pid, 0)  # liveness check; raises if the broker is gone
        host = str(info.get("host") or "127.0.0.1")
        return f"http://{host}:{int(info['port'])}", info.get("token") or None
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("no daemon reachable — start one with 'maestro-daemon' or set MAESTRO_DAEMON_URL") from None


def _daemon_url() -> str:
    return _daemon_endpoint()[0]


def _daemon_token() -> str | None:
    return _daemon_endpoint()[1]


def _post_jsonrpc(url: str, method: str, params: dict[str, Any], token: str | None = None) -> Any:
    from .a2a_client import post_jsonrpc

    return post_jsonrpc(url, method, params, token=token)


def _sse_events(url: str, path: str, token: str | None = None):
    """Yield ``(event_name, data_dict)`` from an SSE endpoint (see a2a_client)."""
    from .a2a_client import sse_events

    yield from sse_events(url, path, token=token)


def _stream_task(url: str, task_id: str | None, token: str | None = None) -> int:
    """Follow one task (or the global stream when task_id is None) live."""
    path = f"/tasks/{task_id}/events" if task_id else "/events"
    final_state: str | None = None
    try:
        for event, envelope in _sse_events(url, path, token=token):
            data = envelope.get("data") or {}  # TaskEvent.to_dict nests the payload under "data"
            if event == "output":
                print(data.get("line", ""), flush=True)
            elif event == "state":
                state = data.get("state") or "?"
                note = f" — {data['error']}" if data.get("error") else ""
                note += f" (question: {data['question']})" if data.get("question") else ""
                print(f"[state] {state}{note}", flush=True)
                final_state = state
            elif event == "usage":
                print(f"[usage] {json.dumps(data, ensure_ascii=False)}", flush=True)
    except KeyboardInterrupt:
        print("\n[tail stopped]", flush=True)
        return 130
    if task_id is not None and final_state is not None:
        return 0 if final_state == "completed" else 1
    return 0


def _cmd_delegate(args: argparse.Namespace) -> int:
    from .handoff import HandoffDoc, load_handoff_file

    workspace = str(_workspace(getattr(args, "workspace", None)))
    if getattr(args, "project", None):
        workspace = str(_project(args.project))
    if args.file:
        doc = load_handoff_file(args.file)
    else:
        if not (args.title and args.request and args.target):
            raise ValueError("provide --file or all of --title/--request/--target")
        design = ""
        if args.design_file:
            try:
                design = Path(args.design_file).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"Unable to read design file: {exc}") from exc
        doc = HandoffDoc(
            title=args.title, request=args.request, design=design,
            target_agent=args.target, fallback=list(args.fallback),
        )
    url, token = _daemon_endpoint()
    result = _post_jsonrpc(url, "message/send", {"message": {
        "kind": "message", "role": "user",
        "parts": [{"kind": "data", "data": doc.to_dict()}],
        "metadata": {"maestro": {"workspace": workspace}},
    }}, token=token)
    task = (result or {}).get("task") or {}
    task_id = task.get("id")
    if not task_id:  # queued behind an active task in this workspace
        print(json.dumps({"queued": True, "reason": "workspace already has an active task; this handoff is next in line"}, indent=2))
        return 0
    if args.no_wait:
        print(json.dumps(result, indent=2))
        return 0
    print(f"[task] {task_id} — target={doc.target_agent} workspace={workspace}", flush=True)
    return _stream_task(url, task_id, token=token)


def _cmd_task_audit(args: argparse.Namespace) -> int:
    state_dir = maestro_user_dir()
    m = Maestro(state_dir)
    try:
        claims = m._claims(args.task_id)
        runtime: dict[str, Any] = {}
        if claims.get("task_runtime"):
            try:
                parsed = json.loads(claims["task_runtime"])
                if isinstance(parsed, dict):
                    runtime = parsed
            except ValueError:
                pass
        results: list[dict[str, Any]] = []
        task_dir = state_dir / "tasks" / args.task_id
        if task_dir.is_dir():
            for path in sorted(task_dir.glob("result-*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        results.append(value)
                except (OSError, ValueError):
                    continue
        print(json.dumps({
            "task_id": args.task_id,
            "title": claims.get("task_title"),
            "state": runtime.get("state") or claims.get("task_status"),
            "workspace": claims.get("task_workspace"),
            "branch": claims.get("task_branch"),
            "origin_agent": claims.get("task_origin_agent"),
            "target_agent": claims.get("task_target_agent"),
            "attempts": runtime.get("attempts") or [],
            "usage": runtime.get("usage"),
            "error": runtime.get("error"),
            "results": results,
        }, indent=2))
        return 0
    finally:
        m.close()


def _cmd_budgets() -> int:
    from .budgets import BudgetCaps, daily_spend, spent_by_agent
    from .core import Maestro

    caps = BudgetCaps.from_env()
    if not caps.any():
        print("no budget caps configured (set MAESTRO_BUDGET_PER_AGENT_USD and/or MAESTRO_BUDGET_DAILY_USD)")
        return 0
    records = _budget_records_from_state()
    per_agent = spent_by_agent(records)
    daily = daily_spend(records)
    if caps.per_agent_usd is not None:
        print(f"per-agent cap: ${caps.per_agent_usd:.4f}")
        for agent in sorted(per_agent):
            spent = per_agent[agent]
            flag = "  (EXHAUSTED)" if spent >= caps.per_agent_usd else ""
            print(f"  {agent}: ${spent:.4f}{flag}")
    if caps.daily_usd is not None:
        flag = "  (EXHAUSTED)" if daily >= caps.daily_usd else ""
        print(f"daily cap: ${caps.daily_usd:.4f} — spent today (UTC): ${daily:.4f}{flag}")
    return 0


def _budget_records_from_state() -> list[dict[str, Any]]:
    from .budgets import _records_from_claims

    m = Maestro(maestro_user_dir())
    try:
        return list(_records_from_claims(m.mem.get_all()).values())
    finally:
        m.close()


def _cmd_peers(args: argparse.Namespace) -> int:
    from .discovery import PeerTable, STALE_AFTER_S

    table = PeerTable(Path(maestro_user_dir()) / "peers.json")
    if args.peers_cmd == "add":
        url = args.url.rstrip("/")
        if not url.startswith(("http://", "https://")):
            print(f"peer URL must be http(s): {args.url}", file=sys.stderr)
            return 2
        table.add_static(args.name, url)
        print(f"added peer {args.name!r} -> {url}")
        return 0
    if args.peers_cmd == "remove":
        if table.remove(args.peer):
            print(f"removed peer {args.peer!r}")
            return 0
        print(f"no such peer: {args.peer}", file=sys.stderr)
        return 1
    # list
    import time as _time

    now = _time.time()
    peers = table.load()
    if not peers:
        print("no peers discovered yet (peers.json is empty)")
        return 0
    for key in sorted(peers):
        peer = peers[key]
        age = now - float(peer.get("last_seen") or 0)
        status = "manual" if peer.get("manual") else ("live" if age <= STALE_AFTER_S else f"stale {int(age)}s")
        url = peer.get("url") or f"http://{peer.get('address')}:{peer.get('port')}"
        print(f"{key}\t{status}\t{peer.get('name') or '?'}\t{url}")
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    state_dir = maestro_user_dir()
    m = Maestro(state_dir)
    try:
        now = datetime.now(timezone.utc)

        def age_days_for(task_id: str, record: dict[str, Any]) -> float | None:
            task_dir = state_dir / "tasks" / task_id
            if task_dir.is_dir():
                return (now - datetime.fromtimestamp(task_dir.stat().st_mtime, tz=timezone.utc)).total_seconds() / 86400.0
            created_at = record.get("created_at")
            if isinstance(created_at, str):
                try:
                    created = datetime.fromisoformat(created_at)
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    return (now - created).total_seconds() / 86400.0
                except ValueError:
                    return None
            return None

        removed: list[dict[str, Any]] = []
        kept = 0
        for record in m._registry_records():
            task_id = str(record.get("task_id") or "")
            if not task_id:
                continue
            phase = str(m._claims(task_id).get("task_status") or "")
            terminal = phase in {"COMPLETE", "FAILED"}  # canceled tasks land in FAILED
            age = age_days_for(task_id, record)
            if not terminal or age is None or age < args.days:
                kept += 1
                continue
            if args.dry_run:
                removed.append({"task_id": task_id, "title": record.get("title"), "age_days": round(age, 1), "dry_run": True})
                continue
            shutil.rmtree(state_dir / "tasks" / task_id, ignore_errors=True)
            m._save_index([x for x in m._load_index() if str(x.get("task_id")) != task_id])
            dropped = m.mem.forget(m._subject(task_id))
            removed.append({"task_id": task_id, "title": record.get("title"), "age_days": round(age, 1), "claims_dropped": dropped})
        print(json.dumps({"removed": removed, "kept": kept}, indent=2))
        return 0
    finally:
        m.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="maestro", description="Multi-agent orchestration broker with durable user-level task state")
    p.add_argument("--version", action="version", version=VERSION)
    _add_target_args(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    task = sub.add_parser("task", help="Inspect and manage tasks")
    task_sub = task.add_subparsers(dest="task_cmd")
    task_list = task_sub.add_parser("list", help="List tasks")
    _add_target_args(task_list)
    task_status = task_sub.add_parser("status", help="Show task status")
    task_status.add_argument("task_id")
    _add_target_args(task_status)
    task_show = task_sub.add_parser("show", help="Alias for task status")
    task_show.add_argument("task_id")
    _add_target_args(task_show)
    task_tail = task_sub.add_parser("tail", help="Live-tail a task's event stream (SSE, no polling)")
    task_tail.add_argument("task_id")
    task_tail.add_argument("--all", action="store_true", help="Follow the global stream instead of one task")
    task_audit = task_sub.add_parser("audit", help="Show the durable audit record (attempts, usage, errors)")
    task_audit.add_argument("task_id")

    d = sub.add_parser("delegate", help="Delegate a handoff to any registered agent via the local daemon")
    d.add_argument("--file", default=None, help="Handoff document file (TOML or JSON)")
    d.add_argument("--title", default=None)
    d.add_argument("--request", default=None)
    d.add_argument("--target", default=None)
    d.add_argument("--fallback", action="append", default=[], help="Fallback agent (repeatable)")
    d.add_argument("--design-file", default=None, help="Design text file to attach")
    d.add_argument("--no-wait", action="store_true", help="Return immediately after enqueueing")
    _add_target_args(d)

    gc = sub.add_parser("gc", help="Delete terminal tasks older than the TTL (manual; never runs automatically)")
    gc.add_argument("--days", type=int, default=90, help="TTL in days (default 90)")
    gc.add_argument("--dry-run", action="store_true", help="List what would be deleted")

    dash = sub.add_parser("dashboard", help="Terminal dashboard for the local daemon (SSE-driven, no polling)")

    peers = sub.add_parser("peers", help="Manage discovered/registered Maestro peers")
    peers_sub = peers.add_subparsers(dest="peers_cmd", required=True)
    peers_list = peers_sub.add_parser("list", help="List live and stale peers (peers.json)")
    peers_add = peers_sub.add_parser("add", help="Manually register a peer (for networks without broadcast)")
    peers_add.add_argument("--name", required=True)
    peers_add.add_argument("--url", required=True, help="Peer base URL, e.g. http://10.0.0.5:8790")
    peers_remove = peers_sub.add_parser("remove", help="Remove a peer by key or name")
    peers_remove.add_argument("peer")

    sub.add_parser("budgets", help="Show budget caps and current spend (MAESTRO_BUDGET_*_USD)")

    s = sub.add_parser("status", help="Show task status")
    s.add_argument("task_id")
    _add_target_args(s)
    top = sub.add_parser("list", help="List tasks")
    _add_target_args(top)
    cfg = sub.add_parser("config", help="Show effective Codex defaults")
    _add_target_args(cfg)
    storage = sub.add_parser("storage", help="Manage storage backends")
    storage_sub = storage.add_subparsers(dest="storage_cmd", required=True)
    migrate = storage_sub.add_parser("migrate-memvara", help="Import legacy state")
    _add_target_args(migrate)

    agents = sub.add_parser("agents", help="Manage registered agents (user-level)")
    agents_sub = agents.add_subparsers(dest="agents_cmd", required=True)
    agents_list = agents_sub.add_parser("list", help="List registered agents")
    agents_add = agents_sub.add_parser("add", help="Register an agent")
    agents_add.add_argument("--name", required=True)
    agents_add.add_argument("--kind", required=True, choices=[*BUILTIN_ADAPTERS, GENERIC_KIND])
    agents_add.add_argument("--display-name", default="")
    agents_add.add_argument("--skill", action="append", default=[])
    agents_add.add_argument("--command", default=None, help="Launch command for generic agents (supports {workspace} and {prompt})")
    agents_add.add_argument("--input-mode", choices=["arg", "stdin"], default="arg")
    agents_add.add_argument("--output-format", choices=["text", "jsonl", "rpc"], default="text")
    agents_add.add_argument("--workspace-policy", choices=["cwd", "flag"], default="cwd")
    agents_add.add_argument("--token", default=None, help="Bearer token for remote daemons (a2a_remote / api agents on non-loopback binds)")
    agents_remove = agents_sub.add_parser("remove", help="Unregister an agent")
    agents_remove.add_argument("name")
    agents_discover = agents_sub.add_parser("discover", help="Scan PATH for known agent CLIs")
    agents_status = agents_sub.add_parser("status", help="Show registration/availability status for one agent")
    agents_status.add_argument("name")

    args = p.parse_args(_normalize_argv(argv if argv is not None else sys.argv[1:]))
    if args.cmd == "task" and args.task_cmd is None:
        task.print_help()
        return 2
    try:
        if args.cmd == "delegate":
            return _cmd_delegate(args)
        if args.cmd == "gc":
            return _cmd_gc(args)
        if args.cmd == "dashboard":
            from . import tui

            try:
                url = _daemon_url()
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            return tui.run(url, token=_daemon_token())
        if args.cmd == "peers":
            return _cmd_peers(args)
        if args.cmd == "budgets":
            return _cmd_budgets()
        if args.cmd == "task" and args.task_cmd == "tail":
            url = _daemon_url()
            target_id = None if args.all else args.task_id
            return _stream_task(url, target_id, token=_daemon_token())
        if args.cmd == "task" and args.task_cmd == "audit":
            return _cmd_task_audit(args)

        if args.cmd == "storage" and args.storage_cmd == "migrate-memvara":
            m = Maestro(_workspace(getattr(args, "workspace", None)))
            try:
                print(json.dumps(m.migrate_legacy_memvara(), indent=2)); return 0
            finally: m.close()

        if (args.cmd == "task" and args.task_cmd == "list") or args.cmd == "list":
            base, scope = _scope_for_list(args)
            m = Maestro(base)
            try:
                if getattr(args, "project", None):
                    tasks = m.list_tasks(project_filter=scope)
                elif getattr(args, "workspace", None) or (os.environ.get("MAESTRO_WORKSPACE") or "").strip():
                    project_root = Maestro._resolve_project_root(base)
                    if (base / ".claude" / "worktrees").is_dir() or base == project_root:
                        tasks = m.list_tasks(project_filter=str(project_root))
                    else:
                        tasks = m.list_tasks(workspace_filter=str(base))
                else:
                    tasks = m.list_tasks()
                print(json.dumps(tasks, indent=2)); return 0
            finally: m.close()

        if args.cmd == "agents":
            registry = AgentRegistry(maestro_user_dir())
            if args.agents_cmd == "list":
                print(json.dumps([s.to_dict() for s in registry.list()], indent=2)); return 0
            if args.agents_cmd == "add":
                spec = AgentSpec(
                    name=args.name, kind=args.kind, display_name=args.display_name,
                    skills=list(args.skill), command=args.command,
                    input_mode=args.input_mode, output_format=args.output_format,
                    workspace_policy=args.workspace_policy, token=args.token,
                )
                registry.save(spec)
                print(json.dumps(registry.get(args.name).to_dict(), indent=2)); return 0
            if args.agents_cmd == "remove":
                if not registry.remove(args.name):
                    raise ValueError(f"Agent not registered: {args.name}")
                print(json.dumps({"name": args.name, "removed": True}, indent=2)); return 0
            if args.agents_cmd == "discover":
                print(json.dumps(registry.discover(), indent=2)); return 0
            if args.agents_cmd == "status":
                print(json.dumps(registry.status(args.name), indent=2)); return 0
            raise AssertionError("unhandled agents command")  # pragma: no cover

        target = _workspace(getattr(args, "workspace", None))
        if getattr(args, "project", None):
            target = _project(args.project)
        m = Maestro(target)
        try:
            if args.cmd == "task" and args.task_cmd in {"status", "show"}:
                print(json.dumps(m.status(args.task_id), indent=2)); return 0
            if args.cmd == "status":
                print(json.dumps(m.status(args.task_id), indent=2)); return 0
            if args.cmd == "config":
                print(json.dumps(m.codex_defaults(), indent=2)); return 0
            raise AssertionError("unhandled command")  # pragma: no cover
        finally:
            m.close()
    except KeyError as exc:
        print(f"maestro: {exc.args[0]}", file=sys.stderr); return 2
    except ValueError as exc:
        print(f"maestro: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())  # pragma: no cover
