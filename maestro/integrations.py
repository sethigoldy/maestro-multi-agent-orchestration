"""Global skill installation for supported coding agents.

Each supported agent has its own global instruction/skill mechanism. This module
defines one :class:`AgentIntegration` per agent (detection, install, uninstall,
status) and a :class:`SkillManager` that drives them all. The shell installer
(``install.sh``) and the ``maestro skill`` CLI both call this layer — no agent
paths are hardcoded anywhere else.

Mechanisms (all under the user's home directory, never system paths):

- Claude Code: global Agent Skill at ``~/.claude/skills/maestro-driven-development/SKILL.md``
- Codex: managed block in ``~/.codex/instructions.md`` (global instructions)
- GitHub Copilot CLI: managed block in ``~/.copilot/instructions.md``
- Cursor: global user rule at ``~/.cursor/rules/maestro-driven-development.mdc``
- Hermes: managed block in ``~/.hermes/instructions.md``
- Pi: managed block in ``~/.pi/instructions.md``
- Cline: managed block in ``~/.cline/rules.md`` (global rules)
- OpenHands: managed block inside ``custom_instructions`` of
  ``~/.openhands/agent_settings.json``

Block-based installs are idempotent: the managed region is delimited by marker
lines, so reinstalling replaces the region instead of duplicating it, and
uninstall removes exactly that region (and nothing the user wrote).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_NAME = "maestro-driven-development"

_BEGIN = f"<!-- {SKILL_NAME}:begin -->"
_END = f"<!-- {SKILL_NAME}:end -->"


def skill_source() -> Path:
    """Path of the packaged SKILL.md source for the global skill."""
    return Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"


def load_skill_content() -> str:
    return skill_source().read_text(encoding="utf-8")


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block (--- ... ---) if present."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1 :]).lstrip("\n")
    return text


def _managed_block(content: str) -> str:
    return f"{_BEGIN}\n{content.strip()}\n{_END}"


def _replace_block(text: str, block: str) -> tuple[str, bool]:
    """Insert or replace the managed region; returns (new_text, was_present)."""
    pattern = re.compile(re.escape(_BEGIN) + r".*?" + re.escape(_END), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _m: block, text, count=1), True
    base = text.rstrip("\n")
    prefix = f"{base}\n\n" if base else ""
    return f"{prefix}{block}\n", False


def _remove_block(text: str) -> tuple[str, bool]:
    pattern = re.compile(re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\n?", re.DOTALL)
    new_text, count = pattern.subn("", text)
    return new_text.strip("\n") + ("\n" if new_text.strip() else ""), count > 0


@dataclass
class IntegrationResult:
    """Outcome of one integration operation."""

    kind: str
    display_name: str
    ok: bool
    action: str  # installed | already-installed | uninstalled | not-detected | skipped | error
    detail: str = ""
    path: str | None = None


class AgentIntegration:
    """One supported agent's global skill mechanism."""

    kind: str = ""
    display_name: str = ""
    #: human-readable mechanism description for status output
    mechanism: str = "instructions"

    def __init__(self) -> None:
        self._binary: str | None = None

    # -- detection ----------------------------------------------------------
    def binary(self) -> str | None:
        if self._binary is None:
            from .agents import DEFAULT_BINARIES

            self._binary = DEFAULT_BINARIES.get(self.kind)
        return self._binary

    def detect(self) -> bool:
        """True when the agent's CLI is on PATH."""
        binary = self.binary()
        return bool(binary) and shutil.which(binary) is not None

    # -- skill operations ----------------------------------------------------
    def install_skill(self, home: Path, content: str) -> IntegrationResult:
        raise NotImplementedError

    def uninstall_skill(self, home: Path) -> IntegrationResult:
        raise NotImplementedError

    def status(self, home: Path) -> dict[str, Any]:
        raise NotImplementedError


class _BlockIntegration(AgentIntegration):
    """Agents whose global instructions live in one markdown file."""

    def instructions_path(self, home: Path) -> Path:
        raise NotImplementedError

    def install_skill(self, home: Path, content: str) -> IntegrationResult:
        path = self.instructions_path(home)
        try:
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot read {path}: {exc}", str(path))
        new_text, present = _replace_block(existing, _managed_block(content))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot write {path}: {exc}", str(path))
        action = "already-installed" if present else "installed"
        return IntegrationResult(self.kind, self.display_name, True, action, f"{self.mechanism} updated", str(path))

    def uninstall_skill(self, home: Path) -> IntegrationResult:
        path = self.instructions_path(home)
        if not path.is_file():
            return IntegrationResult(self.kind, self.display_name, True, "skipped", "nothing installed", str(path))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot read {path}: {exc}", str(path))
        new_text, removed = _remove_block(text)
        if not removed:
            return IntegrationResult(self.kind, self.display_name, True, "skipped", "not installed", str(path))
        try:
            if new_text.strip():
                path.write_text(new_text + "\n", encoding="utf-8")
            else:
                path.unlink()  # the file only ever contained our block
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot write {path}: {exc}", str(path))
        return IntegrationResult(self.kind, self.display_name, True, "uninstalled", f"{self.mechanism} removed", str(path))

    def status(self, home: Path) -> dict[str, Any]:
        path = self.instructions_path(home)
        installed = False
        if path.is_file():
            try:
                installed = _BEGIN in path.read_text(encoding="utf-8")
            except OSError:
                installed = False
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "mechanism": self.mechanism,
            "path": str(path),
            "detected": self.detect(),
            "installed": installed,
        }


class CodexIntegration(_BlockIntegration):
    kind = "codex"
    display_name = "Codex"
    mechanism = "global instructions block (~/.codex/instructions.md)"

    def instructions_path(self, home: Path) -> Path:
        return home / ".codex" / "instructions.md"


class CopilotIntegration(_BlockIntegration):
    kind = "copilot"
    display_name = "GitHub Copilot CLI"
    mechanism = "global instructions block (~/.copilot/instructions.md)"

    def instructions_path(self, home: Path) -> Path:
        return home / ".copilot" / "instructions.md"


class HermesIntegration(_BlockIntegration):
    kind = "hermes"
    display_name = "Hermes Agent"
    mechanism = "global instructions block (~/.hermes/instructions.md)"

    def instructions_path(self, home: Path) -> Path:
        return home / ".hermes" / "instructions.md"


class PiIntegration(_BlockIntegration):
    kind = "pi"
    display_name = "Pi"
    mechanism = "global instructions block (~/.pi/instructions.md)"

    def instructions_path(self, home: Path) -> Path:
        return home / ".pi" / "instructions.md"


class ClineIntegration(_BlockIntegration):
    kind = "cline"
    display_name = "Cline"
    mechanism = "global rules block (~/.cline/rules.md)"

    def instructions_path(self, home: Path) -> Path:
        return home / ".cline" / "rules.md"


class OpencodeIntegration(_BlockIntegration):
    kind = "opencode"
    display_name = "OpenCode"
    mechanism = "global rules block (~/.config/opencode/AGENTS.md)"

    def instructions_path(self, home: Path) -> Path:
        return home / ".config" / "opencode" / "AGENTS.md"


class ClaudeCodeIntegration(AgentIntegration):
    """Claude Code global Agent Skills (``~/.claude/skills/<name>/SKILL.md``)."""

    kind = "claude_code"
    display_name = "Claude Code"
    mechanism = "global skill (~/.claude/skills/maestro-driven-development/SKILL.md)"

    def skill_dir(self, home: Path) -> Path:
        return home / ".claude" / "skills" / SKILL_NAME

    def install_skill(self, home: Path, content: str) -> IntegrationResult:
        target_dir = self.skill_dir(home)
        target = target_dir / "SKILL.md"
        present = target.is_file()
        try:
            if present:
                existing = target.read_text(encoding="utf-8")
            else:
                existing = ""
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot read {target}: {exc}", str(target))
        try:
            import shutil as _shutil

            # Updates are delete-then-reinstall: wipe the existing skill directory
            # first so stale files from a previous Maestro version never survive.
            if target_dir.exists():
                _shutil.rmtree(target_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot write {target}: {exc}", str(target))
        action = "already-installed" if present and existing == content else "installed"
        return IntegrationResult(self.kind, self.display_name, True, action, "global skill written (previous copy removed)", str(target))

    def uninstall_skill(self, home: Path) -> IntegrationResult:
        target_dir = self.skill_dir(home)
        target = target_dir / "SKILL.md"
        if not target.is_file():
            return IntegrationResult(self.kind, self.display_name, True, "skipped", "nothing installed", str(target))
        import shutil as _shutil

        try:
            _shutil.rmtree(target_dir)
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot remove {target_dir}: {exc}", str(target))
        return IntegrationResult(self.kind, self.display_name, True, "uninstalled", "global skill removed", str(target))

    def status(self, home: Path) -> dict[str, Any]:
        target = self.skill_dir(home) / "SKILL.md"
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "mechanism": self.mechanism,
            "path": str(target),
            "detected": self.detect(),
            "installed": target.is_file(),
        }


class CursorIntegration(AgentIntegration):
    """Cursor global user rules (``~/.cursor/rules/<name>.mdc``)."""

    kind = "cursor"
    display_name = "Cursor"
    mechanism = "global rule (~/.cursor/rules/maestro-driven-development.mdc)"

    def rule_path(self, home: Path) -> Path:
        return home / ".cursor" / "rules" / f"{SKILL_NAME}.mdc"

    @staticmethod
    def _render_rule(content: str) -> str:
        body = _strip_frontmatter(content)
        return (
            "---\n"
            "description: Maestro is the default development execution backend\n"
            "alwaysApply: true\n"
            "---\n\n"
            + body
        )

    def install_skill(self, home: Path, content: str) -> IntegrationResult:
        target = self.rule_path(home)
        present = target.is_file()
        rendered = self._render_rule(content)
        try:
            if present:
                existing = target.read_text(encoding="utf-8")
            else:
                existing = ""
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot read {target}: {exc}", str(target))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot write {target}: {exc}", str(target))
        action = "already-installed" if present and existing == rendered else "installed"
        return IntegrationResult(self.kind, self.display_name, True, action, "global rule written", str(target))

    def uninstall_skill(self, home: Path) -> IntegrationResult:
        target = self.rule_path(home)
        if not target.is_file():
            return IntegrationResult(self.kind, self.display_name, True, "skipped", "nothing installed", str(target))
        try:
            target.unlink()
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot remove {target}: {exc}", str(target))
        return IntegrationResult(self.kind, self.display_name, True, "uninstalled", "global rule removed", str(target))

    def status(self, home: Path) -> dict[str, Any]:
        target = self.rule_path(home)
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "mechanism": self.mechanism,
            "path": str(target),
            "detected": self.detect(),
            "installed": target.is_file(),
        }


class OpenHandsIntegration(AgentIntegration):
    """OpenHands global instructions via ``custom_instructions`` in agent_settings.json."""

    kind = "openhands"
    display_name = "OpenHands"
    mechanism = "agent settings (~/.openhands/agent_settings.json custom_instructions)"

    def settings_path(self, home: Path) -> Path:
        return home / ".openhands" / "agent_settings.json"

    def _load(self, path: Path) -> tuple[dict[str, Any] | None, str | None]:
        if not path.is_file():
            return {}, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, f"cannot parse {path}: {exc}"
        if not isinstance(data, dict):
            return None, f"{path} is not a JSON object"
        return data, None

    def install_skill(self, home: Path, content: str) -> IntegrationResult:
        path = self.settings_path(home)
        data, error = self._load(path)
        if data is None:
            return IntegrationResult(self.kind, self.display_name, False, "error", error or "unreadable settings", str(path))
        block = _managed_block(content)
        current = data.get("custom_instructions")
        current = current if isinstance(current, str) else ""
        present = _BEGIN in current
        if not present:
            current = f"{current.rstrip(chr(10))}\n\n{block}\n" if current.strip() else f"{block}\n"
        data["custom_instructions"] = current
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot write {path}: {exc}", str(path))
        action = "already-installed" if present else "installed"
        return IntegrationResult(self.kind, self.display_name, True, action, "custom_instructions updated", str(path))

    def uninstall_skill(self, home: Path) -> IntegrationResult:
        path = self.settings_path(home)
        data, error = self._load(path)
        if data is None:  # only reachable when the file exists but is unreadable
            return IntegrationResult(self.kind, self.display_name, False, "error", error or "unreadable settings", str(path))
        current = data.get("custom_instructions")
        current = current if isinstance(current, str) else ""
        new_text, removed = _remove_block(current)
        if not removed:
            return IntegrationResult(self.kind, self.display_name, True, "skipped", "not installed", str(path))
        if new_text.strip():
            data["custom_instructions"] = new_text + "\n"
        else:
            data.pop("custom_instructions", None)
        try:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as exc:
            return IntegrationResult(self.kind, self.display_name, False, "error", f"cannot write {path}: {exc}", str(path))
        return IntegrationResult(self.kind, self.display_name, True, "uninstalled", "custom_instructions cleaned", str(path))

    def status(self, home: Path) -> dict[str, Any]:
        path = self.settings_path(home)
        installed = False
        data, _error = self._load(path)
        if isinstance(data, dict):
            current = data.get("custom_instructions")
            installed = isinstance(current, str) and _BEGIN in current
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "mechanism": self.mechanism,
            "path": str(path),
            "detected": self.detect(),
            "installed": installed,
        }


#: Every supported integration, keyed by adapter kind.
INTEGRATIONS: dict[str, type[AgentIntegration]] = {
    cls.kind: cls
    for cls in (
        CodexIntegration,
        ClaudeCodeIntegration,
        CopilotIntegration,
        CursorIntegration,
        HermesIntegration,
        PiIntegration,
        ClineIntegration,
        OpencodeIntegration,
        OpenHandsIntegration,
    )
}

#: Accepted spellings for ``--agent`` (adapter kind, binary name, or display name).
AGENT_ALIASES: dict[str, str] = {
    "claude": "claude_code",
    "cursor-agent": "cursor",
    "github-copilot": "copilot",
    "claude code": "claude_code",
    "github copilot cli": "copilot",
    "hermes agent": "hermes",
}


class SkillManager:
    """Drives skill installation across all supported agents for one home dir."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home).expanduser() if home else Path.home()

    def integrations(self) -> list[AgentIntegration]:
        return [cls() for cls in INTEGRATIONS.values()]

    def resolve(self, name: str) -> AgentIntegration | None:
        key = AGENT_ALIASES.get(name.strip().lower(), name.strip().lower())
        for instance in self.integrations():
            if key in (instance.kind.lower(), instance.display_name.lower(), (instance.binary() or "").lower()):
                return instance
        return None

    def install(self, agent: str | None = None, all_agents: bool = False) -> list[IntegrationResult]:
        """Install the skill. With no selector: every DETECTED agent gets it."""
        content = load_skill_content()
        if agent is not None:
            target = self.resolve(agent)
            if target is None:
                return [IntegrationResult(agent, agent, False, "error", f"unknown or unsupported agent: {agent!r}")]
            result = target.install_skill(self.home, content)
            if result.ok and not target.detect():
                result.detail = f"{result.detail}; note: CLI not found on PATH (installed ahead of detection)"
            return [result]
        selected = self.integrations() if all_agents else [i for i in self.integrations() if i.detect()]
        results: list[IntegrationResult] = []
        for integration in selected:
            results.append(integration.install_skill(self.home, content))
        if not all_agents:
            for integration in self.integrations():
                if not integration.detect():
                    results.append(IntegrationResult(integration.kind, integration.display_name, True, "not-detected", "CLI not on PATH"))
        return results

    def uninstall(self, agent: str | None = None) -> list[IntegrationResult]:
        """Remove the skill. With no selector: every agent where it is installed."""
        if agent is not None:
            target = self.resolve(agent)
            if target is None:
                return [IntegrationResult(agent, agent, False, "error", f"unknown or unsupported agent: {agent!r}")]
            return [target.uninstall_skill(self.home)]
        results: list[IntegrationResult] = []
        for integration in self.integrations():
            if integration.status(self.home)["installed"]:
                results.append(integration.uninstall_skill(self.home))
        return results

    def status(self) -> list[dict[str, Any]]:
        out = []
        for integration in self.integrations():
            entry = integration.status(self.home)
            entry["binary"] = integration.binary()
            out.append(entry)
        return out
