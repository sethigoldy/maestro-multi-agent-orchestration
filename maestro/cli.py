from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .core import Maestro

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
    if len(argv) >= 2 and argv[0] == "task":
        subcommand = argv[1]
        if not subcommand.startswith("-") and subcommand not in {"list", "status", "show"}:  # pragma: no branch
            return ["task", "status", *argv[1:]]
    if len(argv) >= 3 and argv[1] == "task":
        subcommand = argv[2]
        if not subcommand.startswith("-") and subcommand not in {"list", "status", "show"}:  # pragma: no branch
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


def main() -> int:
    p = argparse.ArgumentParser(prog="maestro", description="Claude-supervised orchestration with user-level task state")
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

    h = sub.add_parser("handoff", help="Manually create and launch a Codex handoff")
    h.add_argument("--title", required=True)
    h.add_argument("--request", required=True)
    h.add_argument("--design-file", required=True)
    h.add_argument("--model")
    h.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    _add_target_args(h)
    follow = sub.add_parser("codex-followup", help="Delegate a follow-up directly to Codex")
    follow.add_argument("task_id")
    follow.add_argument("instruction")
    _add_target_args(follow)
    r = sub.add_parser("run", help="Launch implementation for a task")
    r.add_argument("task_id")
    _add_target_args(r)
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

    args = p.parse_args(_normalize_argv(sys.argv[1:]))
    if args.cmd == "task" and args.task_cmd is None:
        task.print_help()
        return 2
    try:
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

        target = _workspace(getattr(args, "workspace", None))
        if getattr(args, "project", None):
            target = _project(args.project)
        m = Maestro(target)
        try:
            if args.cmd == "task" and args.task_cmd in {"status", "show"}:
                print(json.dumps(m.status(args.task_id), indent=2)); return 0
            if args.cmd == "handoff":
                try:
                    design = Path(args.design_file).read_text(encoding="utf-8")
                except OSError as exc:
                    raise ValueError(f"Unable to read design file: {exc}") from exc
                task_data = m.create_handoff(args.title, args.request, design, model=args.model, effort=args.effort)
                launch = m.implement_async(task_data["task_id"])
                print(json.dumps({"task": task_data, "launch": launch}, indent=2)); return 0
            if args.cmd == "codex-followup":
                print(json.dumps(m.codex_followup(args.task_id, args.instruction), indent=2)); return 0
            if args.cmd == "run":
                print(json.dumps(m.implement_async(args.task_id), indent=2)); return 0
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
