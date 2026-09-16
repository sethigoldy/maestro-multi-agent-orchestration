from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import Maestro


def main() -> int:
    p = argparse.ArgumentParser(prog="maestro", description="Claude-supervised orchestration with Memvara")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("handoff", help="Manually create and launch a Codex handoff")
    h.add_argument("--title", required=True)
    h.add_argument("--request", required=True)
    h.add_argument("--design-file", required=True)
    h.add_argument("--model", help="Codex model override")
    h.add_argument("--effort", choices=["low", "medium", "high", "xhigh"], help="Codex reasoning effort override")

    r = sub.add_parser("run", help="Launch implementation for a task")
    r.add_argument("task_id")

    s = sub.add_parser("status", help="Show task status; accepts number or full task id")
    s.add_argument("task_id")

    sub.add_parser("list", help="List tasks with human-friendly numbers")
    sub.add_parser("config", help="Show effective Codex defaults")

    args = p.parse_args()
    m = Maestro(Path.cwd())
    try:
        if args.cmd == "handoff":
            design = Path(args.design_file).read_text(encoding="utf-8")
            task = m.create_handoff(args.title, args.request, design, model=args.model, effort=args.effort)
            launch = m.implement_async(task["task_id"])
            print(json.dumps({"task": task, "launch": launch}, indent=2))
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
    except KeyError as exc:
        print(f"maestro: {exc.args[0]}", file=__import__("sys").stderr)
        return 2
    except ValueError as exc:
        print(f"maestro: {exc}", file=__import__("sys").stderr)
        return 2
    finally:
        m.close()


if __name__ == "__main__":
    raise SystemExit(main())
