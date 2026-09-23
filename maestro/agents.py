"""Agent registry and adapter specifications.

Maestro treats every delegable agent as a first-class citizen: one TOML file per
registered agent under ``~/.maestro/agents/``. Builtin adapters (codex, claude_code,
hermes, pi, cline, openhands, cursor, copilot, opencode, a2a_remote) are identified by kind;
the ``generic`` kind is the declarative path that onboards any CLI without Python
code (launch command + input/output mode + workspace policy).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
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
    "opencode",
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
    "opencode": "opencode",
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
    ("opencode", "opencode", "OpenCode"),
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

    def to_dict(self, redact: bool = False) -> dict[str, Any]:
        """The spec as a plain dict. ``redact=True`` replaces the bearer token
        with "<redacted>"; use it for anything shown to a person or a model."""
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
            data["token"] = "<redacted>" if redact else self.token
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


#: Characters a TOML basic string may not hold as-is: backslash, double quote,
#: and every control character except tab (U+0000 to U+001F and U+007F).
_TOML_NEEDS_ESCAPE = re.compile('[\\\\"\x00-\x08\x0a-\x1f\x7f]')
_TOML_SHORT_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n"}
_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Backslash, quote and newline use their short escapes; every other
        # forbidden character is written as \uXXXX, which TOML always accepts.
        escaped = _TOML_NEEDS_ESCAPE.sub(
            lambda match: _TOML_SHORT_ESCAPES.get(match.group(0), f"\\u{ord(match.group(0)):04X}"), value
        )
        return f'"{escaped}"'
    raise TypeError(f"Unsupported TOML scalar type: {type(value).__name__}")


def _toml_key(key: Any) -> str:
    """Write a key bare when TOML allows it, and as a quoted string otherwise."""
    text = str(key)
    return text if _TOML_BARE_KEY_RE.match(text) else _toml_scalar(text)


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize a flat/one-level-nested mapping to TOML (subset writer)."""
    lines: list[str] = []
    nested: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            nested[key] = value
        elif isinstance(value, list):
            if all(isinstance(item, str) for item in value):
                lines.append(f"{_toml_key(key)} = [{', '.join(_toml_scalar(item) for item in value)}]")
            elif all(isinstance(item, dict) for item in value):
                # Array of tables: [[key]] blocks (e.g. the handoff's [[context]]).
                for item in value:
                    lines.append("")
                    lines.append(f"[[{_toml_key(key)}]]")
                    for sub_key, sub_value in item.items():
                        if isinstance(sub_value, list):
                            lines.append(f"{_toml_key(sub_key)} = [{', '.join(_toml_scalar(x) for x in sub_value)}]")
                        else:
                            lines.append(f"{_toml_key(sub_key)} = {_toml_scalar(sub_value)}")
            else:
                raise TypeError(f"List {key!r} must contain only strings or tables")
        else:
            lines.append(f"{_toml_key(key)} = {_toml_scalar(value)}")
    for key, table in nested.items():
        lines.append("")
        lines.append(f"[{_toml_key(key)}]")
        for sub_key, value in table.items():
            if isinstance(value, list):
                lines.append(f"{_toml_key(sub_key)} = [{', '.join(_toml_scalar(item) for item in value)}]")
            else:
                lines.append(f"{_toml_key(sub_key)} = {_toml_scalar(value)}")
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


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by the owner only (mode 0600).

    Used for files that can hold a bearer token. The text goes into a new
    temporary file in the same directory, created with mode 0600, which then
    replaces ``path``. The file is never rewritten in place: Unix checks
    permissions only when a file is opened, so another user who opened an old
    world-readable copy could otherwise read the new token through that handle.
    The temporary file is removed if anything fails.
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class AgentRegistry:
    """File-backed registry of agents under ``<state_dir>/agents/``."""

    def __init__(self, state_dir: str | Path) -> None:
        self.dir = Path(state_dir) / "agents"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._tighten_entry_modes()

    def _tighten_entry_modes(self) -> None:
        """Make entries written by older versions readable by the owner only.

        Older versions wrote entries with the default mode, often 0644, and an
        entry can hold a bearer token. A symbolic link is left alone so
        that its target is never changed. A file this user cannot chmod (for
        example one owned by another user) is skipped rather than failing.
        """
        for path in self.dir.glob("*.toml"):
            try:
                mode = path.lstat().st_mode
                if stat.S_ISREG(mode) and mode & 0o077:
                    os.chmod(path, 0o600)
            except OSError:
                continue

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
        """Write the spec's TOML file without ever leaving a broken file behind.

        The text is parsed before anything is written; if it is not valid TOML
        the save is refused and the current file is left as it was. The text
        is then written to a temporary file next to the target and renamed over
        it, so a failed write cannot truncate the existing entry. The entry can
        hold a bearer token, so the file is readable by its owner only (0600).
        """
        spec = validate_agent_spec(spec)
        path = self._path(spec.name)
        text = _dump_toml(spec.to_dict())
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Refusing to save agent {spec.name!r}: the registry writer produced invalid TOML ({exc})") from exc
        # The entry can hold a bearer token, so only the owner may read it.
        # _write_private replaces the file and removes its temporary file on
        # any failure, including a UnicodeEncodeError from a lone surrogate.
        _write_private(path, text)
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
