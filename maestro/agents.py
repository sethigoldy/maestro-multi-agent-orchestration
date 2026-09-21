"""Agent registry and adapter specifications.

Maestro treats every delegable agent as a first-class citizen: one TOML file per
registered agent under ``~/.maestro/agents/``. Builtin adapters (codex, claude_code,
hermes, pi, cline, openhands, cursor, copilot, a2a_remote) are identified by kind;
the ``generic`` kind is the declarative path that onboards any CLI without Python
code (launch command + input/output mode + workspace policy).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Adapter kinds implemented in ``maestro/adapters/`` (M2+).
BUILTIN_ADAPTERS: tuple[str, ...] = (
    "codex",
    "claude_code",
    "hermes",
    "pi",
    "cline",
    "openhands",
    "cursor",
    "copilot",
    "a2a_remote",
)

#: The declarative, config-only adapter kind.
GENERIC_KIND = "generic"

VALID_KINDS: tuple[str, ...] = BUILTIN_ADAPTERS + (GENERIC_KIND,)

INPUT_MODES: tuple[str, ...] = ("arg", "stdin")
OUTPUT_FORMATS: tuple[str, ...] = ("text", "jsonl", "rpc")
WORKSPACE_POLICIES: tuple[str, ...] = ("cwd", "flag")

#: Default executable per builtin adapter kind (used by discovery/status).
DEFAULT_BINARIES: dict[str, str | None] = {
    "codex": "codex",
    "claude_code": "claude",
    "hermes": "hermes",
    "pi": "pi",
    "cline": "cline",
    "openhands": "openhands",
    "cursor": "cursor-agent",
    "copilot": "copilot",
    "a2a_remote": None,
}

#: (binary, adapter kind, display name) — used by ``AgentRegistry.discover``.
KNOWN_CLIS: tuple[tuple[str, str, str], ...] = (
    ("codex", "codex", "Codex"),
    ("claude", "claude_code", "Claude Code"),
    ("hermes", "hermes", "Hermes Agent"),
    ("pi", "pi", "Pi"),
    ("cline", "cline", "Cline"),
    ("cursor-agent", "cursor", "Cursor"),
    ("copilot", "copilot", "GitHub Copilot CLI"),
    ("openhands", "openhands", "OpenHands"),
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class AgentSpec:
    """One registered agent."""

    name: str
    kind: str
    display_name: str = ""
    skills: list[str] = field(default_factory=list)
    enabled: bool = True
    source: str = "user"  # "user" | "discovered"
    # Per-agent execution defaults (round-3 decision: registry carries them; any
    # delegation may override per task).
    model: str | None = None
    effort: str | None = None
    timeout_s: float | None = None
    # Remote-auth for a2a_remote / api agents (sent as Bearer token). Never
    # announced over discovery; it lives only in the registry entry.
    token: str | None = None
    # Generic-kind fields (ignored for builtin kinds):
    command: str | None = None
    input_mode: str = "arg"
    output_format: str = "text"
    workspace_policy: str = "cwd"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "display_name": self.display_name or self.name,
            "skills": list(self.skills),
            "enabled": self.enabled,
            "source": self.source,
        }
        if self.model is not None:
            data["model"] = self.model
        if self.effort is not None:
            data["effort"] = self.effort
        if self.timeout_s is not None:
            data["timeout_s"] = self.timeout_s
        if self.token is not None:
            data["token"] = self.token
        if self.kind == GENERIC_KIND:
            data.update(
                {
                    "command": self.command,
                    "input_mode": self.input_mode,
                    "output_format": self.output_format,
                    "workspace_policy": self.workspace_policy,
                }
            )
        elif self.kind == "a2a_remote" and self.command is not None:
            data["command"] = self.command  # the remote base URL
        return data


def validate_agent_spec(spec: AgentSpec) -> AgentSpec:
    """Validate a spec and return it; raise ValueError on any violation."""
    if not isinstance(spec.name, str) or not _NAME_RE.match(spec.name):
        raise ValueError(f"Agent name must match {_NAME_RE.pattern}: {spec.name!r}")
    if spec.kind not in VALID_KINDS:
        raise ValueError(f"Unknown adapter kind {spec.kind!r}; expected one of {', '.join(VALID_KINDS)}")
    if spec.source not in {"user", "discovered"}:
        raise ValueError(f"Unknown source {spec.source!r}; expected 'user' or 'discovered'")
    for skill in spec.skills:
        if not isinstance(skill, str) or not skill.strip():
            raise ValueError(f"Skills must be non-empty strings: {skill!r}")
    if spec.kind == GENERIC_KIND:
        if not spec.command or not str(spec.command).strip():
            raise ValueError("Generic agents require a launch 'command'")
        if spec.input_mode not in INPUT_MODES:
            raise ValueError(f"input_mode must be one of {INPUT_MODES}: {spec.input_mode!r}")
        if spec.output_format not in OUTPUT_FORMATS:
            raise ValueError(f"output_format must be one of {OUTPUT_FORMATS}: {spec.output_format!r}")
        if spec.workspace_policy not in WORKSPACE_POLICIES:
            raise ValueError(f"workspace_policy must be one of {WORKSPACE_POLICIES}: {spec.workspace_policy!r}")
    if spec.kind == "a2a_remote":
        if not spec.command or not str(spec.command).startswith(("http://", "https://")):
            raise ValueError("a2a_remote agents require a command that is an http(s) base URL of the remote daemon")
    return spec


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    raise TypeError(f"Unsupported TOML scalar type: {type(value).__name__}")


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize a flat/one-level-nested mapping to TOML (subset writer)."""
    lines: list[str] = []
    nested: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            nested[key] = value
        elif isinstance(value, list):
            if all(isinstance(item, str) for item in value):
                lines.append(f"{key} = [{', '.join(_toml_scalar(item) for item in value)}]")
            elif all(isinstance(item, dict) for item in value):
                # Array of tables: [[key]] blocks (e.g. the handoff's [[context]]).
                for item in value:
                    lines.append("")
                    lines.append(f"[[{key}]]")
                    for sub_key, sub_value in item.items():
                        if isinstance(sub_value, list):
                            lines.append(f"{sub_key} = [{', '.join(_toml_scalar(x) for x in sub_value)}]")
                        else:
                            lines.append(f"{sub_key} = {_toml_scalar(sub_value)}")
            else:
                raise TypeError(f"List {key!r} must contain only strings or tables")
        else:
            lines.append(f"{key} = {_toml_scalar(value)}")
    for key, table in nested.items():
        lines.append("")
        lines.append(f"[{key}]")
        for sub_key, value in table.items():
            if isinstance(value, list):
                lines.append(f"{sub_key} = [{', '.join(_toml_scalar(item) for item in value)}]")
            else:
                lines.append(f"{sub_key} = {_toml_scalar(value)}")
    return "\n".join(lines).rstrip() + "\n"


def _spec_from_data(data: dict[str, Any]) -> AgentSpec:
    known = {"name", "kind", "display_name", "skills", "enabled", "source", "command", "input_mode", "output_format", "workspace_policy", "model", "effort", "timeout_s", "token"}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown agent spec fields: {', '.join(sorted(unknown))}")
    return AgentSpec(
        name=str(data["name"]),
        kind=str(data["kind"]),
        display_name=str(data.get("display_name", "")),
        skills=[str(s) for s in data.get("skills", [])],
        enabled=bool(data.get("enabled", True)),
        source=str(data.get("source", "user")),
        model=data.get("model"),
        effort=data.get("effort"),
        timeout_s=float(data["timeout_s"]) if data.get("timeout_s") is not None else None,
        token=data.get("token"),
        command=data.get("command"),
        input_mode=str(data.get("input_mode", "arg")),
        output_format=str(data.get("output_format", "text")),
        workspace_policy=str(data.get("workspace_policy", "cwd")),
    )


class AgentRegistry:
    """File-backed registry of agents under ``<state_dir>/agents/``."""

    def __init__(self, state_dir: str | Path) -> None:
        self.dir = Path(state_dir) / "agents"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"Invalid agent name: {name!r}")
        return self.dir / f"{name}.toml"

    def list(self) -> list[AgentSpec]:
        out: list[AgentSpec] = []
        for path in sorted(self.dir.glob("*.toml")):
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                spec = validate_agent_spec(_spec_from_data(data))
            except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError):
                continue
            out.append(spec)
        return out

    def get(self, name: str) -> AgentSpec | None:
        path = self._path(name)
        if not path.is_file():
            return None
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            return validate_agent_spec(_spec_from_data(data))
        except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError):
            return None

    def save(self, spec: AgentSpec) -> AgentSpec:
        spec = validate_agent_spec(spec)
        self._path(spec.name).write_text(_dump_toml(spec.to_dict()), encoding="utf-8")
        return spec

    def remove(self, name: str) -> bool:
        path = self._path(name)
        if path.is_file():
            path.unlink()
            return True
        return False

    def discover(self) -> list[dict[str, Any]]:
        """Scan PATH for known agent CLIs; returns candidates (not auto-registered)."""
        out: list[dict[str, Any]] = []
        for binary, kind, display in KNOWN_CLIS:
            path = shutil.which(binary)
            out.append(
                {
                    "name": kind,
                    "kind": kind,
                    "display_name": display,
                    "binary": binary,
                    "path": path,
                    "found": path is not None,
                }
            )
        return out

    def register_discovered(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """Register every discovered agent CLI that is not registered yet.

        Idempotent and conservative: an existing registration (user or previously
        discovered) is preserved exactly as-is — custom names, models, tokens,
        skills survive untouched. Only newly found agents receive a default spec
        with ``source="discovered"``. Returns one report entry per discovered
        candidate (missing binaries are not reported).
        """
        out: list[dict[str, Any]] = []
        for candidate in self.discover():
            if not candidate["found"]:
                continue
            name = str(candidate["name"])
            existing = self.get(name)
            if existing is not None:
                out.append({"name": name, "action": "preserved", "detail": f"already registered (source={existing.source})"})
                continue
            spec = AgentSpec(
                name=name, kind=str(candidate["kind"]),
                display_name=str(candidate["display_name"]), source="discovered",
            )
            if not dry_run:
                self.save(spec)
            out.append({"name": name, "action": "would-register" if dry_run else "registered", "detail": f"kind={candidate['kind']}"})
        return out

    def status(self, name: str) -> dict[str, Any]:
        """Registration + availability status for one agent."""
        spec = self.get(name)
        if spec is None:
            return {"name": name, "registered": False}
        # URL-based agents (a2a_remote / api mode): no local binary — report
        # the URL and a live preflight (Agent Card fetch, with the stored token).
        if spec.command and str(spec.command).startswith(("http://", "https://")):
            from .adapters import make_adapter

            base = {
                "name": name,
                "registered": True,
                "kind": spec.kind,
                "display_name": spec.display_name or name,
                "enabled": spec.enabled,
                "url": spec.command,
            }
            try:
                preflight = make_adapter(spec).preflight()
            except Exception as exc:  # malformed spec etc. — surface, don't crash
                return {**base, "reachable": False, "error": str(exc)}
            return {**base, "reachable": preflight.ok, "version": preflight.version if preflight.ok else None, "error": None if preflight.ok else preflight.error}
        binary = spec.command.split()[0] if spec.kind == GENERIC_KIND and spec.command else DEFAULT_BINARIES.get(spec.kind)
        path = shutil.which(binary) if binary else None
        version = _probe_version(path)
        return {
            "name": name,
            "registered": True,
            "kind": spec.kind,
            "display_name": spec.display_name or name,
            "enabled": spec.enabled,
            "binary": binary,
            "found": path is not None,
            "path": path,
            "version": version,
        }


def _probe_version(path: str | None) -> str | None:
    if not path:
        return None
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    if not output:
        return None
    return output.splitlines()[0].strip()
