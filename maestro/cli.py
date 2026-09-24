from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import VERSION
from .agents import AgentRegistry, AgentSpec, BUILTIN_ADAPTERS, GENERIC_KIND
from .core import Maestro, RegistryUnreadableError, maestro_user_dir
from . import worktrees


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


# Top-level options that take a separate value (``--workspace DIR``). They must
# match the options that ``main`` adds to the top-level parser.
_TOP_LEVEL_VALUE_OPTIONS = ("--workspace", "--project")


def _takes_separate_value(token: str) -> bool:
    """Return True when ``token`` is a top-level option whose value is the next token.

    argparse also accepts unambiguous prefixes such as ``--work``, so a prefix
    of a value option counts too. The ``--workspace=DIR`` form carries its
    value in the same token, so it does not take the next one.
    """
    if not token.startswith("--") or len(token) < 3 or "=" in token:
        return False
    return any(option.startswith(token) for option in _TOP_LEVEL_VALUE_OPTIONS)


def _normalize_argv(argv: list[str]) -> list[str]:
    """Rewrite the shorthand ``maestro task <ref>`` to ``maestro task status <ref>``.

    ``argv`` does not include the program name. The shorthand applies only
    when the command itself is ``task``. To find the command, this skips the
    top-level options and the values that belong to them, so
    ``maestro --workspace /repo task 1`` works the same as ``maestro task 1``.
    A ``task`` token that appears later, as an argument of another command or
    as an option value, is left alone.
    """
    known_task_cmds = {"list", "status", "show", "tail", "audit", "receipt", "continue", "answer", "cancel", "cleanup", "rename-branch"}
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        index += 2 if _takes_separate_value(argv[index]) else 1
    if index + 1 >= len(argv) or argv[index] != "task":
        return argv
    subcommand = argv[index + 1]
    if subcommand.startswith("-") or subcommand in known_task_cmds:
        return argv
    return [*argv[: index + 1], "status", *argv[index + 1 :]]


def _add_skill_target_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--agent", default=None, metavar="NAME",
                       help="Target one agent (adapter kind, binary name, or display name)")
    group.add_argument("--all", action="store_true", dest="all_agents",
                       help="Act on every supported agent, even ones not detected on PATH")


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
    else the daemon.json marker written by the daemon that owns the state
    directory (a standalone ``maestro-daemon`` or an MCP server's daemon). The
    marker is checked the same way ``maestro daemon status`` checks it: its
    process must be alive, must be confirmed as the daemon that wrote the
    marker, and must answer HTTP. A stale marker is rejected so callers get a
    clear error instead of a connection failure mid-stream. Non-loopback
    daemons record their auth token in the marker, so local CLI calls are
    authorized automatically.
    """
    from . import daemonctl

    env = os.environ.get("MAESTRO_DAEMON_URL", "").strip()
    if env:
        return env.rstrip("/"), os.environ.get("MAESTRO_DAEMON_TOKEN") or None
    info = daemonctl.status(maestro_user_dir())
    if not info.running or not info.url:
        raise ValueError("no daemon reachable — start one with 'maestro daemon start' (or run 'maestro-daemon' in the foreground) or set MAESTRO_DAEMON_URL")
    return info.url, info.token or None


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
    """Follow one task (or the global stream when task_id is None) live.

    When the daemon drops this reader for falling too far behind, the stream
    is followed again from a new subscription, and a note says that some
    output lines may be missing. A task stream that ends without a final
    state asks the daemon for the task's state (see
    :func:`maestro.a2a_client.follow_task_events`).
    """
    from .a2a_client import follow_events, follow_task_events

    if task_id:
        events = follow_task_events(url, task_id, token=token, stream=_sse_events)
    else:
        events = follow_events(url, token=token, stream=_sse_events)
    final_state: str | None = None
    try:
        for event, envelope in events:
            data = envelope.get("data") or {}  # TaskEvent.to_dict nests the payload under "data"
            if event == "overflow":
                print("[tail] fell behind the daemon's event stream; reconnecting (some output lines may be missing)", flush=True)
            elif event == "output":
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


def _resolve_task_on_daemon(url: str, ref: str, token: str | None) -> str:
    """Ask the daemon which task ``ref`` names and return that task's id.

    ``ref`` is a task number such as ``1`` or a full task id. The daemon's
    ``tasks/get`` method resolves numbers the same way ``maestro task status``
    does, and it refuses an unknown number with an error, which reaches the
    caller as a ValueError. The daemon accepts any well-formed task id without
    checking it, so a task that has no recorded workspace is treated as
    unknown here. Every real task has a workspace: the daemon reports the
    task's workspace claim, or for a task migrated from the legacy journal
    without that claim, the workspace in its registry record.
    """
    result = _post_jsonrpc(url, "tasks/get", {"id": ref}, token=token)
    task = (result or {}).get("task") or {}
    task_id = task.get("id")
    if not task_id or not (task.get("metadata") or {}).get("workspace"):
        raise ValueError(f"Unknown task reference {ref!r}. Run `maestro list` to see available tasks.")
    return str(task_id)


def _cmd_task_tail(args: argparse.Namespace) -> int:
    """Follow one task's event stream, or every task's events with ``--all``."""
    if args.all and args.task_id is not None:
        raise ValueError("task tail: give a task number or id, or --all, not both")
    if not args.all and args.task_id is None:
        raise ValueError("task tail: give a task number or id, or --all to follow every task")
    url, token = _daemon_endpoint()
    if args.all:
        return _stream_task(url, None, token=token)
    # The event stream only matches full task ids, so a number such as "1"
    # has to be resolved first. Without this the stream waits forever for
    # events that carry the task id "1".
    task_id = _resolve_task_on_daemon(url, args.task_id, token)
    return _stream_task(url, task_id, token=token)


def _cmd_delegate(args: argparse.Namespace) -> int:
    from .handoff import HandoffDoc, load_handoff_file

    workspace = str(_workspace(getattr(args, "workspace", None)))
    if getattr(args, "project", None):
        workspace = str(_project(args.project))
    if args.file:
        doc = load_handoff_file(args.file)
        if args.mode:
            doc.mode = args.mode  # --mode overrides any mode named in the file
    else:
        if not (args.title and args.request):
            raise ValueError("provide --file or --title/--request (plus --target or --mode unless [defaults] in .maestro/config.toml names the agent)")
        design = ""
        if args.design_file:
            try:
                design = Path(args.design_file).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"Unable to read design file: {exc}") from exc
        # Without --target/--mode the daemon resolves routing from config
        # [defaults]; if none is configured it parks the task and asks.
        doc = HandoffDoc(
            title=args.title, request=args.request, design=design,
            target_agent=args.target or "codex", fallback=list(args.fallback),
            mode=args.mode, explicit_target=args.target is not None,
        )
    if args.branch is not None:
        from .handoff import validate_handoff

        doc.branch = args.branch  # --branch overrides [expectations] branch in the file
        doc = validate_handoff(doc)
    flag_entries: list[dict[str, Any]] = []
    for text in args.context:
        flag_entries.append({"label": f"context-{len(flag_entries) + 1}", "kind": "text", "text": text})
    for file_path in args.context_file:
        stem = Path(file_path).expanduser().stem or "file"
        flag_entries.append({"label": stem, "kind": "file", "path": str(Path(file_path).expanduser())})
    for skill_path in args.skill:
        name = Path(skill_path).expanduser().name or "skill"
        flag_entries.append({"label": name, "kind": "skill", "path": str(Path(skill_path).expanduser())})
    if flag_entries:
        doc.context_entries = [*doc.context_entries, *flag_entries]
    url, token = _daemon_endpoint()
    result = _post_jsonrpc(url, "message/send", {"message": {
        "kind": "message", "role": "user",
        "parts": [{"kind": "data", "data": doc.to_dict()}],
        "metadata": {"maestro": {"workspace": workspace}},
    }}, token=token)
    task = (result or {}).get("task") or {}
    task_id = task.get("id")
    if not task_id:  # queued: the daemon says why
        reason = (task.get("metadata") or {}).get("reason") or "the task is waiting for a place to run"
        print(json.dumps({"queued": True, "reason": reason}, indent=2))
        return 0
    if args.no_wait:
        print(json.dumps(result, indent=2))
        return 0
    routing = f"mode={doc.mode}" if doc.mode else f"target={doc.target_agent}"
    print(f"[task] {task_id} — {routing} workspace={workspace}", flush=True)
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
            "run_dir": claims.get("task_run_dir") or claims.get("task_workspace"),
            "branch": claims.get("task_branch"),
            "origin_agent": claims.get("task_origin_agent"),
            "target_agent": claims.get("task_target_agent"),
            "attempts": runtime.get("attempts") or [],
            "context": (runtime.get("doc") or {}).get("context") or [],
            "usage": runtime.get("usage"),
            "error": runtime.get("error"),
            "results": results,
        }, indent=2))
        return 0
    finally:
        m.close()


def _try_daemon_endpoint() -> tuple[str, str | None] | None:
    """Like :func:`_daemon_endpoint`, but ``None`` instead of raising (for fallback paths)."""
    try:
        return _daemon_endpoint()
    except ValueError:
        return None


def _cmd_task_receipt(args: argparse.Namespace) -> int:
    import urllib.error
    import urllib.request

    from .receipt import build_receipt, format_receipt

    state_dir = maestro_user_dir()
    m = Maestro(state_dir)
    try:
        resolved: str | None = None
        local_error = ""
        try:
            resolved = m.resolve_task(args.task_id)
        except KeyError as exc:
            local_error = str(exc.args[0])
        receipt: dict[str, Any] | None = None
        endpoint = _try_daemon_endpoint()
        if endpoint is not None:
            url, token = endpoint
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            request = urllib.request.Request(url + f"/tasks/{args.task_id}/receipt", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=5) as resp:
                    receipt = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError):
                receipt = None  # fall back to the durable local state
        if receipt is None:
            if resolved is None and endpoint is None:
                raise ValueError(f"{local_error} (no daemon reachable to check)")
            receipt = build_receipt(resolved or args.task_id, m)
        print(json.dumps(receipt, indent=2) if args.as_json else format_receipt(receipt))
        return 0
    finally:
        m.close()


def _cmd_task_continue(args: argparse.Namespace) -> int:
    url, token = _daemon_endpoint()
    params = {"id": args.task_id, "instruction": args.request, "context_mode": args.context}
    if args.branch is not None:
        params["branch"] = args.branch
    result = _post_jsonrpc(url, "tasks/followup", params, token=token)
    task = (result or {}).get("task") or {}
    task_id = task.get("id")
    if not task_id:
        print(json.dumps(result, indent=2))
        return 0
    if args.no_wait:
        print(json.dumps(result, indent=2))
        return 0
    print(f"[task] {task_id} — continuation (context={args.context}) resuming", flush=True)
    return _stream_task(url, task_id, token=token)


def _cmd_task_answer(args: argparse.Namespace) -> int:
    """Answer the question a parked (input-required) task is waiting on."""
    url, token = _daemon_endpoint()
    result = _post_jsonrpc(url, "tasks/answer", {"id": args.task_id, "answer": " ".join(args.answer)}, token=token)
    task_id = (result or {}).get("task_id")
    if args.no_wait or not task_id or (result or {}).get("state") != "working":
        # The answer can leave the task waiting again (for example, a routing
        # answer that names an unknown agent). Print the result instead of
        # waiting on a turn that did not start.
        print(json.dumps(result, indent=2))
        return 0
    print(f"[task] {task_id} — answered, resuming", flush=True)
    return _stream_task(url, task_id, token=token)


def _cmd_task_cancel(args: argparse.Namespace) -> int:
    url, token = _daemon_endpoint()
    result = _post_jsonrpc(url, "tasks/cancel", {"id": args.task_id, "reason": args.reason}, token=token)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_task_cleanup(args: argparse.Namespace) -> int:
    """Remove a task's worktree through the running daemon."""
    url, token = _daemon_endpoint()
    result = _post_jsonrpc(url, "tasks/cleanup", {"id": args.task_id, "force": bool(args.force)}, token=token)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_task_rename_branch(args: argparse.Namespace) -> int:
    """Rename a task's branch through the running daemon, or directly in the
    durable record when no daemon is running (nothing can be working then)."""
    endpoint = _try_daemon_endpoint()
    if endpoint is not None:
        url, token = endpoint
        result = _post_jsonrpc(url, "tasks/renameBranch", {"id": args.task_id, "branch": args.branch}, token=token)
        rename = (result or {}).get("rename") or {}
    else:
        from .branches import rename_task_branch

        m = Maestro(maestro_user_dir())
        try:
            rename = rename_task_branch(m, m.resolve_task(args.task_id), args.branch)
        finally:
            m.close()
    if rename.get("pending"):
        print(f"Task {rename['task_id']} has no branch yet; its next turn will create {rename['branch']} (instead of {rename['old_branch']})")
    elif rename.get("git_renamed"):
        print(f"Renamed branch {rename['old_branch']} -> {rename['branch']} for {rename['task_id']}")
    elif rename.get("old_branch") == rename.get("branch"):
        print(f"Task {rename['task_id']} is already on branch {rename['branch']}; nothing changed")
    else:
        print(f"Recorded branch {rename['branch']} for {rename['task_id']} (it was already renamed in git from {rename['old_branch']})")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import format_doctor, run_doctor

    workspace = _workspace(getattr(args, "workspace", None))
    if getattr(args, "project", None):
        workspace = _project(args.project)
    report = run_doctor(workspace=workspace)
    print(json.dumps(report, indent=2) if args.as_json else format_doctor(report))
    return 0 if report.get("ok") else 1


def _format_daemon_info(info, started_verb: str) -> str:
    lines = [f"Maestro daemon {started_verb}"]
    if info.pid is not None:
        lines.append(f"PID: {info.pid}")
    if info.url:
        lines.append(f"URL: {info.url}")
    if info.port is not None and started_verb in {"running", "stopped"}:
        lines.append(f"Port: {info.port}")
    if info.state_dir is not None:
        lines.append(f"State: {info.state_dir}")
    if info.uptime_s is not None:
        lines.append(f"Uptime: {int(info.uptime_s)}s")
    if info.detail and started_verb == "stopped":
        lines.append(info.detail)
    return "\n".join(lines)


def _cmd_daemon(args: argparse.Namespace) -> int:
    from . import daemonctl

    try:
        if args.daemon_cmd == "status":
            info = daemonctl.status()
            if args.as_json:
                print(json.dumps(info.to_dict(), indent=2))
            else:
                verb = "running" if info.running else "stopped"
                print(_format_daemon_info(info, verb))
            return 0 if info.running else 1
        if args.daemon_cmd == "start":
            info = daemonctl.start()
            print(_format_daemon_info(info, "already running" if info.already_running else "started"))
            return 0
        if args.daemon_cmd == "stop":
            info = daemonctl.stop()
            print(_format_daemon_info(info, "stopped"))
            return 0
        # restart
        info = daemonctl.restart()
        print(_format_daemon_info(info, "restarted"))
        return 0
    except (RuntimeError, TimeoutError) as exc:
        print(f"maestro: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Anything else (an unreadable state directory, a port that answers
        # with something other than HTTP) is still reported as one line.
        print(f"maestro: daemon {args.daemon_cmd} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _cmd_skill(args: argparse.Namespace) -> int:
    from .integrations import SKILL_NAME, SkillManager, skill_source

    manager = SkillManager()
    if args.skill_cmd == "list":
        print(json.dumps({
            "skill": SKILL_NAME,
            "source": str(skill_source()),
            "supported_agents": [entry["display_name"] for entry in manager.status()],
        }, indent=2))
        return 0
    if args.skill_cmd == "status":
        print(json.dumps(manager.status(), indent=2))
        return 0
    if args.skill_cmd == "install":
        results = manager.install(agent=args.agent, all_agents=args.all_agents)
        for result in results:
            mark = "✓" if result.ok and result.action not in {"not-detected"} else ("-" if result.action == "not-detected" else "✗")
            line = f"{mark} {result.display_name:<16} {result.action}"
            if result.detail:
                line += f"  ({result.detail})"
            print(line)
        return 0 if all(r.ok for r in results) else 1
    # uninstall
    results = manager.uninstall(agent=args.agent)
    for result in results:
        mark = "✓" if result.ok else "✗"
        line = f"{mark} {result.display_name:<16} {result.action}"
        if result.detail:
            line += f"  ({result.detail})"
        print(line)
    return 0 if all(r.ok for r in results) else 1


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

        def removable_age(task_id: str, record: dict[str, Any] | None) -> float | None:
            """The task's age in days if gc should remove it (finished and older
            than --days), otherwise None."""
            if record is None:
                return None
            phase = str(m._claims(task_id).get("task_status") or "")
            if phase not in {"COMPLETE", "FAILED"}:  # canceled tasks land in FAILED
                return None
            age = age_days_for(task_id, record)
            return age if age is not None and age >= args.days else None

        def dirty_worktree(task_id: str) -> tuple[Path, list[str]] | None:
            """The task's worktree and its uncommitted files, or None when it
            has no worktree on disk."""
            claims = m._claims(task_id)
            if claims.get("task_run_dir_kind") != "worktree":
                return None
            # The whole worktree, even when the task ran in a subdirectory of it.
            path = worktrees.worktree_path(state_dir, task_id)
            return (path, worktrees.dirty_files(path)) if path.is_dir() else None

        removed: list[dict[str, Any]] = []
        kept_worktrees: list[dict[str, Any]] = []
        kept = 0
        for record in m._registry_records():
            task_id = str(record.get("task_id") or "")
            if not task_id:
                continue
            age = removable_age(task_id, record)
            if age is None:
                kept += 1
                continue
            worktree = dirty_worktree(task_id)
            if worktree is not None and worktree[1]:
                # Uncommitted work: keep the worktree, and the record that
                # tells the user where it is.
                kept += 1
                kept_worktrees.append({"task_id": task_id, "run_dir": str(worktree[0]), "uncommitted": worktree[1]})
                continue
            if args.dry_run:
                removed.append({"task_id": task_id, "title": record.get("title"), "age_days": round(age, 1), "dry_run": True})
                continue
            # The check is repeated under the locks: a claim written since the
            # first check (a follow-up started, say) means the task is kept.
            if worktree is not None:
                claims = m._claims(task_id)
                try:
                    worktrees.remove_worktree(Path(claims.get("task_workspace") or worktree[0]), worktree[0], force=False)
                except RuntimeError as exc:  # locked, or git refused: keep the task so the user can see why
                    kept += 1
                    kept_worktrees.append({"task_id": task_id, "run_dir": str(worktree[0]), "uncommitted": [], "error": str(exc)})
                    continue
            dropped = m.unregister_task(task_id, should_remove=lambda current, task_id=task_id: removable_age(task_id, current) is not None)
            if dropped is None:
                kept += 1
                continue
            shutil.rmtree(state_dir / "tasks" / task_id, ignore_errors=True)
            removed.append({"task_id": task_id, "title": record.get("title"), "age_days": round(age, 1), "claims_dropped": dropped})
        output: dict[str, Any] = {"removed": removed, "kept": kept}
        if kept_worktrees:
            output["kept_worktrees"] = kept_worktrees
        print(json.dumps(output, indent=2))
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
    task_tail.add_argument("task_id", nargs="?", default=None,
                           help="Task number or task id to follow. Leave it out when you pass --all.")
    task_tail.add_argument("--all", action="store_true", help="Follow the global stream of every task instead of one task")
    task_audit = task_sub.add_parser("audit", help="Show the durable audit record (attempts, usage, errors)")
    task_audit.add_argument("task_id")
    task_receipt = task_sub.add_parser("receipt", help="Show the execution receipt (attempts, verification, gates, totals)")
    task_receipt.add_argument("task_id")
    task_receipt.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable JSON receipt")
    task_continue = task_sub.add_parser(
        "continue",
        help="Continue a finished task with a new instruction (reuses the same task, workspace, branch, and routing; compact task-knowledge context by default)",
    )
    task_continue.add_argument("task_id")
    task_continue.add_argument("--request", required=True, help="The new instruction for this turn")
    task_continue.add_argument("--context", choices=("reuse", "fresh"), default="reuse",
                               help="'reuse' injects the compact task-knowledge snapshot (default); 'fresh' starts a clean reasoning context")
    task_continue.add_argument("--no-wait", action="store_true", help="Return as soon as the turn is submitted (no SSE streaming)")
    task_continue.add_argument("--branch", default=None, metavar="NAME",
                               help="Rename the task's branch to NAME before this turn (same rules as 'task rename-branch')")
    task_answer = task_sub.add_parser(
        "answer",
        help="Answer the question a task in state input-required is waiting on ('task status' shows it); the task then resumes",
    )
    task_answer.add_argument("task_id")
    task_answer.add_argument("answer", nargs="+",
                             help="The answer; several words are joined with spaces (for a routing question: 'codex' or 'agent=codex model=...')")
    task_answer.add_argument("--no-wait", action="store_true", help="Return as soon as the answer is accepted (no SSE streaming)")
    task_cancel = task_sub.add_parser("cancel", help="Cancel a task that is waiting, queued or running")
    task_cancel.add_argument("task_id")
    task_cancel.add_argument("--reason", default="", help="Why the task is canceled (kept in its record)")
    task_cleanup = task_sub.add_parser(
        "cleanup",
        help="Remove a task's worktree (never its branch, never your checkout); refused when the worktree has uncommitted changes unless --force",
    )
    task_cleanup.add_argument("task_id")
    task_cleanup.add_argument("--force", action="store_true", help="Remove the worktree even though it has uncommitted changes")
    task_rename = task_sub.add_parser(
        "rename-branch",
        help="Rename a finished task's git branch and update its record (or record a rename already done with 'git branch -m')",
    )
    task_rename.add_argument("task_id")
    task_rename.add_argument("branch", help="The new branch name")

    d = sub.add_parser("delegate", help="Delegate a handoff to any registered agent via the local daemon")
    d.add_argument("--file", default=None, help="Handoff document file (TOML or JSON)")
    d.add_argument("--title", default=None)
    d.add_argument("--request", default=None)
    d.add_argument("--target", default=None)
    d.add_argument("--mode", default=None, help="Work-mode preset name from config [modes] (pins implementer/reviewer/verifier/fixer)")
    d.add_argument("--fallback", action="append", default=[], help="Fallback agent (repeatable)")
    d.add_argument("--branch", default=None, metavar="NAME",
                   help="Name for the task's git branch (default maestro/<task_id>); must not exist yet")
    d.add_argument("--design-file", default=None, help="Design text file to attach")
    d.add_argument("--context", action="append", default=[], metavar="TEXT",
                   help="Context entry text injected into the agent's prompt (repeatable; label auto-numbered)")
    d.add_argument("--context-file", action="append", default=[], metavar="PATH",
                   help="Context file inlined (or artifact-referenced) for the task (repeatable; label = file stem)")
    d.add_argument("--skill", action="append", default=[], metavar="PATH",
                   help="Agent Skills directory containing SKILL.md, staged for the task (repeatable; label = directory name)")
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

    doctor = sub.add_parser("doctor", help="Diagnose whether this environment can run Maestro (state, daemon, git, agents, workspace)")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable JSON report")
    _add_target_args(doctor)

    s = sub.add_parser("status", help="Show task status")
    s.add_argument("task_id")
    _add_target_args(s)
    top = sub.add_parser("list", help="List tasks")
    _add_target_args(top)
    cfg = sub.add_parser("config", help="Show effective config (Codex defaults, routing [defaults], modes, context)")
    _add_target_args(cfg)
    storage = sub.add_parser("storage", help="Manage storage backends")
    storage_sub = storage.add_subparsers(dest="storage_cmd", required=True)
    migrate = storage_sub.add_parser("migrate-memvara", help="Import legacy state")
    _add_target_args(migrate)

    daemon = sub.add_parser("daemon", help="Manage the background daemon (start/stop/status/restart)")
    daemon_sub = daemon.add_subparsers(dest="daemon_cmd", required=True)
    daemon_sub.add_parser("start", help="Start the daemon in the background and return immediately (idempotent)")
    daemon_stop = daemon_sub.add_parser("stop", help="Stop the daemon gracefully: SIGTERM, grace period, SIGKILL if needed (idempotent)")
    daemon_status = daemon_sub.add_parser("status", help="Show daemon state; exits 0 when running, 1 when stopped")
    daemon_status.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable JSON status")
    daemon_sub.add_parser("restart", help="Stop (if running) and start the daemon")

    skill = sub.add_parser("skill", help="Manage the global maestro-driven-development skill across coding agents")
    skill_sub = skill.add_subparsers(dest="skill_cmd", required=True)
    skill_sub.add_parser("list", help="List the managed skill and its supported agents")
    skill_sub.add_parser("status", help="Show per-agent detection/installation status (JSON)")
    skill_install = skill_sub.add_parser("install", help="Install the skill for detected agents")
    _add_skill_target_args(skill_install)
    skill_uninstall = skill_sub.add_parser("uninstall", help="Remove the skill (from one --agent, or everywhere it is installed)")
    _add_skill_target_args(skill_uninstall)

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
    agents_register = agents_sub.add_parser("register-discovered", help="Register all discovered agent CLIs that are not registered yet (preserves existing registrations)")
    agents_register.add_argument("--dry-run", action="store_true", help="Report what would be registered without writing")
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
            return _cmd_task_tail(args)
        if args.cmd == "task" and args.task_cmd == "audit":
            return _cmd_task_audit(args)
        if args.cmd == "task" and args.task_cmd == "receipt":
            return _cmd_task_receipt(args)
        if args.cmd == "task" and args.task_cmd == "continue":
            return _cmd_task_continue(args)
        if args.cmd == "task" and args.task_cmd == "answer":
            return _cmd_task_answer(args)
        if args.cmd == "task" and args.task_cmd == "cancel":
            return _cmd_task_cancel(args)
        if args.cmd == "task" and args.task_cmd == "cleanup":
            return _cmd_task_cleanup(args)
        if args.cmd == "task" and args.task_cmd == "rename-branch":
            return _cmd_task_rename_branch(args)
        if args.cmd == "doctor":
            return _cmd_doctor(args)
        if args.cmd == "daemon":
            return _cmd_daemon(args)
        if args.cmd == "skill":
            return _cmd_skill(args)

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
                print(json.dumps([s.to_dict(redact=True) for s in registry.list()], indent=2)); return 0
            if args.agents_cmd == "add":
                spec = AgentSpec(
                    name=args.name, kind=args.kind, display_name=args.display_name,
                    skills=list(args.skill), command=args.command,
                    input_mode=args.input_mode, output_format=args.output_format,
                    workspace_policy=args.workspace_policy, token=args.token,
                )
                registry.save(spec)
                print(json.dumps(registry.get(args.name).to_dict(redact=True), indent=2)); return 0
            if args.agents_cmd == "remove":
                if not registry.remove(args.name):
                    raise ValueError(f"Agent not registered: {args.name}")
                print(json.dumps({"name": args.name, "removed": True}, indent=2)); return 0
            if args.agents_cmd == "discover":
                print(json.dumps(registry.discover(), indent=2)); return 0
            if args.agents_cmd == "register-discovered":
                print(json.dumps(registry.register_discovered(dry_run=args.dry_run), indent=2)); return 0
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
                modes = {name: preset.to_dict() for name, preset in (m.config.get("modes") or {}).items()}
                context = {label: entry.to_dict() for label, entry in (m.config.get("context") or {}).items()}
                print(json.dumps({**m.codex_defaults(), "defaults": m.config.get("defaults") or {}, "modes": modes, "context": context, "continuation": m.config.get("continuation") or {}}, indent=2)); return 0
            raise AssertionError("unhandled command")  # pragma: no cover
        finally:
            m.close()
    except KeyError as exc:
        print(f"maestro: {exc.args[0]}", file=sys.stderr); return 2
    except RegistryUnreadableError as exc:
        print(f"maestro: {exc}", file=sys.stderr); return 2
    except ValueError as exc:
        print(f"maestro: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())  # pragma: no cover
