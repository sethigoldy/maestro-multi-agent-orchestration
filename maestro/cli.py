from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .core import Maestro

VERSION = "0.5.5"


def _workspace(value: str | None) -> Path:
    candidate = value or os.environ.get("MAESTRO_WORKSPACE") or str(Path.cwd())
    return Path(candidate).expanduser().resolve()


def _project(value: str | None) -> Path:
    candidate = value or str(Path.cwd())
    path = Path(candidate).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Project does not exist or is not a directory: {path}")
    return path


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help=(
            "Target Git workspace containing the task's .maestro state. "
            "Defaults to MAESTRO_WORKSPACE or the current directory."
        ),
    )
    group.add_argument(
        "--project",
        default=argparse.SUPPRESS,
        help=(
            "Project root to discover tasks across the project and its "
            ".claude/worktrees/* worktrees."
        ),
    )


def _effective_workspace(args: argparse.Namespace) -> Path:
    return _workspace(getattr(args, "workspace", None))


def _discover_workspaces(project: Path) -> list[Path]:
    """Find existing Maestro workspaces without creating new state directories."""
    candidates: list[Path] = [project]
    worktrees = project / ".claude" / "worktrees"
    if worktrees.is_dir():
        candidates.extend(p for p in sorted(worktrees.iterdir()) if p.is_dir())

    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        state = resolved / ".maestro"
        if (state / "memory.db").is_file() or (state / "tasks.json").is_file():
            result.append(resolved)
    return result


def _project_tasks(project: Path) -> list[dict]:
    tasks: list[dict] = []
    for workspace in _discover_workspaces(project):
        m = Maestro(workspace)
        try:
            tasks.extend(m.list_tasks())
        finally:
            m.close()
    return sorted(tasks, key=lambda x: (str(x.get("workspace", "")), int(x.get("number", 0))))


def _project_status(project: Path, task_ref: str) -> dict:
    matches: list[tuple[Path, dict]] = []
    for workspace in _discover_workspaces(project):
        m = Maestro(workspace)
        try:
            try:
                result = m.status(task_ref)
            except KeyError:
                continue
            matches.append((workspace, result))
        finally:
            m.close()

    if not matches:
        raise KeyError(
            f"Unknown task reference {task_ref!r} in project {project}. "
            "Run `maestro task list --project <path>` to see available tasks."
        )
    if len(matches) > 1:
        workspaces = ", ".join(str(path) for path, _ in matches)
        raise KeyError(
            f"Task reference {task_ref!r} is ambiguous across workspaces: {workspaces}. "
            "Use the full task id or pass --workspace."
        )
    return matches[0][1]


def _normalize_argv(argv: list[str]) -> list[str]:
    """Normalize `maestro task <ref>` to `maestro task status <ref>`."""
    if len(argv) >= 3 and argv[1] == "task":
        subcommand = argv[2]
        if not subcommand.startswith("-") and subcommand not in {"list", "status", "show"}:
            return [argv[0], "task", "status", *argv[2:]]
    return argv


def main() -> int:
    p = argparse.ArgumentParser(
        prog="maestro",
        description="Claude-supervised orchestration with Memvara",
    )
    p.add_argument("--version", action="version", version=VERSION)
    _add_target_args(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    # Modern command shape.
    task = sub.add_parser("task", help="Inspect and manage tasks")
    task_sub = task.add_subparsers(dest="task_cmd")

    task_list = task_sub.add_parser("list", help="List tasks")
    _add_target_args(task_list)

    task_status = task_sub.add_parser("status", help="Show task status")
    task_status.add_argument("task_id")
    _add_target_args(task_status)

    # Convenience: `maestro task <id>` is normalized below to status.
    task_ref = task_sub.add_parser("show", help="Alias for task status")
    task_ref.add_argument("task_id")
    _add_target_args(task_ref)

    # Legacy top-level commands remain supported.
    h = sub.add_parser("handoff", help="Manually create and launch a Codex handoff")
    h.add_argument("--title", required=True)
    h.add_argument("--request", required=True)
    h.add_argument("--design-file", required=True)
    h.add_argument("--model", help="Codex model override")
    h.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="Codex reasoning effort override")
    _add_target_args(h)

    r = sub.add_parser("run", help="Launch implementation for a task")
    r.add_argument("task_id")
    _add_target_args(r)

    s = sub.add_parser("status", help="Show task status; accepts number or full task id")
    s.add_argument("task_id")
    _add_target_args(s)

    top_list = sub.add_parser("list", help="List tasks with human-friendly numbers")
    _add_target_args(top_list)

    cfg = sub.add_parser("config", help="Show effective Codex defaults")
    _add_target_args(cfg)

    args = p.parse_args(_normalize_argv(sys.argv[1:]))

    # Preserve the convenient `maestro task <id>` syntax without doing a second
    # parse (the old implementation incorrectly called parse_args([]), which
    # fails because the top-level command is required).
    if args.cmd == "task" and args.task_cmd is None:
        task.print_help()
        return 2

    try:
        target_project = getattr(args, "project", None)
        if target_project:
            project = _project(target_project)
            if args.cmd == "task" and args.task_cmd == "list":
                print(json.dumps(_project_tasks(project), indent=2))
                return 0
            if args.cmd == "task" and args.task_cmd in {"status", "show"}:
                print(json.dumps(_project_status(project, args.task_id), indent=2))
                return 0
            raise ValueError("--project is supported with `maestro task list` and `maestro task status/show` only")

        workspace = _effective_workspace(args)
        m = Maestro(workspace)
        try:
            if args.cmd == "task":
                if args.task_cmd == "list":
                    print(json.dumps(m.list_tasks(), indent=2))
                    return 0
                if args.task_cmd in {"status", "show"}:
                    print(json.dumps(m.status(args.task_id), indent=2))
                    return 0
                task.print_help()
                return 2

            if args.cmd == "handoff":
                design = Path(args.design_file).read_text(encoding="utf-8")
                task_data = m.create_handoff(
                    args.title, args.request, design, model=args.model, effort=args.effort
                )
                launch = m.implement_async(task_data["task_id"])
                print(json.dumps({"task": task_data, "launch": launch}, indent=2))
                return 0
            if args.cmd == "run":
                print(json.dumps(m.implement_async(args.task_id), indent=2))
                return 0
            if args.cmd == "status":
                print(json.dumps(m.status(args.task_id), indent=2))
                return 0
            if args.cmd == "list":
                print(json.dumps(m.list_tasks(), indent=2))
                return 0
            if args.cmd == "config":
                print(json.dumps(m.codex_defaults(), indent=2))
                return 0
            return 1
        finally:
            m.close()
    except KeyError as exc:
        print(f"maestro: {exc.args[0]}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"maestro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
