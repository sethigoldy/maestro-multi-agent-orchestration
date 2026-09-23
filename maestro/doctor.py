"""``maestro doctor``: environment diagnostics for running Maestro.

Fast, deterministic, and strictly read-only: it never modifies the environment,
installs anything, or changes PATH. Missing optional agents are reported, not
treated as failures; only a state directory that cannot be used (missing parent
not creatable, unwritable), an invalid configuration, or a configured storage
backend that cannot be loaded is blocking. Tokens are never printed — only
whether authentication applies.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import VERSION
from .agents import AgentRegistry, KNOWN_CLIS
from .budgets import BudgetCaps, _records_from_claims, daily_spend, spent_by_agent
from .core import Maestro, maestro_user_dir
from .worker import _verification_command


def _git_info() -> dict[str, Any]:
    path = shutil.which("git")
    if not path:
        return {"installed": False, "version": None}
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {"installed": True, "version": None}
    output = (result.stdout or "").strip()
    version = output.splitlines()[0].strip() if output else None
    return {"installed": True, "version": version}


def _probe_binary_version(path: str | None) -> str | None:
    if not path:
        return None
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0].strip() if output else None


def _daemon_status(state_dir: Path) -> dict[str, Any]:
    """Resolve the daemon endpoint like the CLI does and probe it once.

    Resolution order: ``MAESTRO_DAEMON_URL`` (+ optional ``MAESTRO_DAEMON_TOKEN``),
    else the ``daemon.json`` marker (liveness-checked). The probe is a single
    ``GET /tasks`` with a short timeout; 401 means reachable-but-unauthorized.
    """
    import json

    url: str | None = None
    token: str | None = None
    env_url = os.environ.get("MAESTRO_DAEMON_URL", "").strip()
    if env_url:
        url = env_url.rstrip("/")
        token = os.environ.get("MAESTRO_DAEMON_TOKEN") or None
    else:
        marker = state_dir / "daemon.json"
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(info["pid"])
            os.kill(pid, 0)  # liveness check; a stale marker is rejected
            host = str(info.get("host") or "127.0.0.1")
            url = f"http://{host}:{int(info['port'])}"
            token = info.get("token") or None
        except (OSError, ValueError, KeyError, TypeError):
            url = None
    if not url:
        return {"reachable": False, "url": None, "auth": "none", "status": "no daemon configured"}

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url + "/tasks", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=2) as resp:
            resp.read()
        return {"reachable": True, "url": url, "auth": "token" if token else "none", "status": "ok"}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"reachable": True, "url": url, "auth": "token" if token else "missing", "status": "unauthorized"}
        return {"reachable": False, "url": url, "auth": "token" if token else "none", "status": f"http {exc.code}"}
    except (urllib.error.URLError, OSError):
        return {"reachable": False, "url": url, "auth": "token" if token else "none", "status": "unreachable"}


def _agent_entries(state_dir: Path) -> list[dict[str, Any]]:
    """Availability for every known CLI kind plus any extra registered agents."""
    entries: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    for binary, kind, display in KNOWN_CLIS:
        path = shutil.which(binary)
        version = _probe_binary_version(path)
        entries.append(
            {
                "name": kind,
                "kind": kind,
                "display_name": display,
                "binary": binary,
                "found": path is not None,
                "path": path,
                "version": version,
                "status": "available" if path is not None else "not found",
            }
        )
        seen_kinds.add(kind)
    try:
        registry = AgentRegistry(state_dir)
    except OSError:
        return entries
    for spec in registry.list():
        if spec.kind in seen_kinds and spec.name == spec.kind:
            continue  # already covered by the builtin row
        try:
            status = registry.status(spec.name)
        except Exception:  # malformed entry: report, do not crash doctor
            entries.append({"name": spec.name, "kind": spec.kind, "display_name": spec.display_name or spec.name, "status": "error"})
            continue
        entries.append(
            {
                "name": spec.name,
                "kind": spec.kind,
                "display_name": spec.display_name or spec.name,
                "binary": status.get("binary"),
                "found": bool(status.get("found")) if status.get("url") is None else True,
                "path": status.get("path"),
                "version": status.get("version"),
                "url": status.get("url"),
                "status": "available" if (status.get("found") or status.get("reachable")) else ("unreachable" if status.get("url") else "not found"),
            }
        )
    return entries


def _workspace_info(workspace: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(workspace),
        "exists": workspace.is_dir(),
        "is_git_repo": False,
        "git_root": None,
        "readable": os.access(workspace, os.R_OK) if workspace.exists() else False,
        "writable": os.access(workspace, os.W_OK) if workspace.exists() else False,
        "project_type": "unknown",
        "verification_command": None,
    }
    if not info["exists"]:
        return info
    if shutil.which("git"):
        try:
            probe = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            probe = None
        if probe is not None and probe.returncode == 0 and probe.stdout.strip():
            info["is_git_repo"] = True
            info["git_root"] = probe.stdout.strip()
    for marker, label in (
        ("pyproject.toml", "python"),
        ("package.json", "node"),
        ("go.mod", "go"),
        ("Cargo.toml", "rust"),
        ("Makefile", "make"),
    ):
        if (workspace / marker).exists():
            info["project_type"] = label
            break
    try:
        command, _note = _verification_command(workspace)
        info["verification_command"] = command
    except (OSError, ValueError):
        info["verification_command"] = None
    return info


def run_doctor(workspace: str | Path | None = None) -> dict[str, Any]:
    """Collect the full diagnostic report as stable JSON-safe data.

    ``workspace`` defaults to the current directory (the CLI passes its own
    resolution); the state directory is always :func:`maestro.core.maestro_user_dir`
    (honoring ``MAESTRO_HOME``). Returns the report dict plus an ``ok`` flag:
    False only when a blocking problem exists (unusable state dir, missing
    workspace, invalid configuration, or a configured storage backend that
    cannot be loaded, such as memvara when its package is not installed).
    Missing optional agents and a missing daemon are reported but never
    blocking.
    """
    state = maestro_user_dir()
    ws = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()

    report: dict[str, Any] = {
        "maestro": {"version": VERSION},
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "system": {"platform": platform.platform(), "os": sys.platform, "arch": platform.machine()},
        "state": {},
        "daemon": {},
        "git": _git_info(),
        "agents": [],
        "workspace": _workspace_info(ws),
        "budget": {},
    }

    blocking: list[str] = []
    state_exists = state.is_dir()
    state_writable = os.access(state, os.W_OK) if state_exists else False
    config_paths = [state / "config.toml", ws / ".maestro" / "config.toml"]
    maestro_instance: Maestro | None = None
    try:
        maestro_instance = Maestro(ws)
    except ValueError as exc:
        report["state"] = {
            "dir": str(state),
            "exists": state_exists,
            "writable": state_writable,
            "config_paths": [str(p) for p in config_paths],
            "storage_backend": None,
            "error": f"invalid configuration: {exc}",
        }
        blocking.append(f"invalid Maestro configuration: {exc}")
    except OSError as exc:
        report["state"] = {
            "dir": str(state),
            "exists": state_exists,
            "writable": state_writable,
            "config_paths": [str(p) for p in config_paths],
            "storage_backend": None,
            "error": f"state directory unusable: {exc}",
        }
        blocking.append(f"state directory unusable: {exc}")
    except RuntimeError as exc:
        # The configured storage backend cannot be loaded. For example, the
        # configuration asks for the memvara backend but the memvara package
        # is not installed. Maestro cannot run until this is fixed.
        report["state"] = {
            "dir": str(state),
            "exists": state_exists,
            "writable": state_writable,
            "config_paths": [str(p) for p in config_paths],
            "storage_backend": None,
            "error": f"storage backend unavailable: {exc}",
        }
        blocking.append(f"storage backend unavailable: {exc}")
    else:
        # Re-probe after construction: on a fresh install the directory did not
        # exist when we first checked, but Maestro() just created it and wrote
        # state files into it — the pre-creation probe would wrongly block.
        state_writable = os.access(state, os.W_OK)
        effective_paths = [
            maestro_instance.user_state_dir / "config.toml",
            maestro_instance.project_root / ".maestro" / "config.toml",
            maestro_instance.root / ".maestro" / "config.toml",
        ]
        report["state"] = {
            "dir": str(state),
            "exists": True,  # Maestro() created it if needed
            "writable": state_writable,
            "config_paths": [str(p) for p in effective_paths],
            "storage_backend": maestro_instance.config.get("storage_backend"),
        }
        if not state_writable:
            blocking.append(f"state directory is not writable: {state}")

    if workspace and not report["workspace"]["exists"]:
        blocking.append(f"workspace does not exist: {ws}")

    report["daemon"] = _daemon_status(state)

    report["agents"] = _agent_entries(state)

    caps = BudgetCaps.from_env()
    budget: dict[str, Any] = {"per_agent_usd": caps.per_agent_usd, "daily_usd": caps.daily_usd}
    if maestro_instance is not None:
        try:
            records = list(_records_from_claims(maestro_instance.mem.get_all()).values())
            budget["spent_today_usd"] = round(daily_spend(records), 6)
            budget["spent_by_agent"] = {agent: round(cost, 6) for agent, cost in sorted(spent_by_agent(records).items())}
        finally:
            maestro_instance.close()
    report["budget"] = budget

    report["ok"] = not blocking
    if blocking:
        report["problems"] = list(blocking)
    return report


def format_doctor(report: dict[str, Any]) -> str:
    """Render the doctor report as a human-readable terminal document."""
    lines: list[str] = ["Maestro doctor", "-" * 40, ""]

    def mark(ok: bool) -> str:
        return "✓" if ok else "✗"

    maestro = report.get("maestro") or {}
    python = report.get("python") or {}
    system = report.get("system") or {}
    lines.append("Maestro")
    lines.append(f"  {mark(True)} version {maestro.get('version') or '?'}")
    lines.append(f"  {mark(True)} python {python.get('version') or '?'} ({python.get('executable') or '?'})")
    lines.append(f"  {mark(True)} system {system.get('os') or '?'} / {system.get('arch') or '?'}")

    state = report.get("state") or {}
    lines.extend(["", "State"])
    if state.get("error"):
        lines.append(f"  ✗ {state['error']}")
    else:
        ok = bool(state.get("exists")) and bool(state.get("writable"))
        detail = "exists, writable" if ok else ("missing (created on first use)" if not state.get("exists") else "NOT WRITABLE")
        lines.append(f"  {mark(ok)} state dir {state.get('dir')} — {detail}")
        backend = state.get("storage_backend")
        if backend:
            lines.append(f"  {mark(True)} storage backend: {backend}")
    for path in state.get("config_paths") or []:
        suffix = "" if Path(path).is_file() else " (not present)"
        lines.append(f"      config: {path}{suffix}")

    daemon = report.get("daemon") or {}
    lines.extend(["", "Daemon"])
    if daemon.get("reachable"):
        auth = f" — auth: {daemon.get('auth')}" if daemon.get("auth") != "none" else ""
        lines.append(f"  {mark(True)} reachable at {daemon.get('url')}{auth}")
    elif daemon.get("url"):
        lines.append(f"  ✗ not reachable at {daemon.get('url')} ({daemon.get('status')})")
    else:
        lines.append("  ! no daemon running — start one with 'maestro-daemon' (expected before first start)")

    git = report.get("git") or {}
    lines.extend(["", "Git"])
    if git.get("installed"):
        version = f" {git['version']}" if git.get("version") else ""
        lines.append(f"  {mark(True)} git{version}")
    else:
        lines.append("  ✗ git not found on PATH (needed for task branches and verification)")

    agents = report.get("agents") or []
    lines.extend(["", "Agents"])
    for agent in agents:
        if agent.get("status") == "available":
            version = f" — {agent['version']}" if agent.get("version") else ""
            lines.append(f"  ✓ {agent.get('display_name') or agent.get('name')} ({agent.get('binary') or agent.get('kind')}){version} — available")
        elif agent.get("status") == "error":
            lines.append(f"  ✗ {agent.get('display_name') or agent.get('name')} — registry entry error")
        else:
            url = f" — {agent['url']}" if agent.get("url") else ""
            lines.append(f"  ✗ {agent.get('display_name') or agent.get('name')} ({agent.get('binary') or '?'}){url} — {agent.get('status')}")

    workspace = report.get("workspace") or {}
    lines.extend(["", f"Workspace ({workspace.get('path')})"])
    if not workspace.get("exists"):
        lines.append("  ✗ workspace does not exist")
    else:
        if workspace.get("is_git_repo"):
            lines.append(f"  ✓ git repository (root: {workspace.get('git_root')})")
        else:
            lines.append("  ! not a git repository (task branches and commit policies need one)")
        perms = "readable, writable" if (workspace.get("readable") and workspace.get("writable")) else ("read-only" if workspace.get("readable") else "not readable")
        lines.append(f"  {mark(bool(workspace.get('readable') and workspace.get('writable')))} {perms}")
        project_type = workspace.get("project_type") or "unknown"
        lines.append(f"  project type: {project_type}")
        command = workspace.get("verification_command")
        if command:
            lines.append(f"  verification command: {' '.join(command)}")

    budget = report.get("budget") or {}
    lines.extend(["", "Budget"])
    if budget.get("per_agent_usd") is None and budget.get("daily_usd") is None:
        lines.append("  no budget caps configured (MAESTRO_BUDGET_PER_AGENT_USD / MAESTRO_BUDGET_DAILY_USD)")
    else:
        if budget.get("per_agent_usd") is not None:
            spent = budget.get("spent_by_agent") or {}
            lines.append(f"  per-agent cap: ${budget['per_agent_usd']:.2f} — spent so far: " + (", ".join(f"{a} ${c:.2f}" for a, c in sorted(spent.items())) or "$0.00"))
        if budget.get("daily_usd") is not None:
            lines.append(f"  daily cap: ${budget['daily_usd']:.2f} — spent today (UTC): ${budget.get('spent_today_usd') or 0.0:.2f}")

    lines.extend(["", "Result"])
    if report.get("ok"):
        lines.append("  ✓ environment is usable")
    else:
        for problem in report.get("problems") or []:
            lines.append(f"  ✗ {problem}")
    return "\n".join(lines)
