"""Context injection: user-controlled context for agent turns.

A typed, layered channel through which the user (not a supervising agent) injects
context into implementation and gate turns:

- **Sources** — standing entries from config tables ``[context.<label>]`` (user file,
  then project/worktree files; later wins per label) plus per-task entries carried in
  the handoff's ``[[context]]`` section. Composition happens once at delegate time;
  the merged list is stored on the handoff so every task record shows exactly what
  was injected (invariant C2).
- **Kinds** — ``text`` (inline instruction), ``file`` (path; inlined when small,
  artifact-referenced when large), ``skill`` (a directory with a ``SKILL.md``, the
  Agent Skills open standard; staged into the task dir and exposed to Claude Code via
  ``--add-dir``, prompt-referenced for every other adapter).
- **Phases** — each entry applies to a subset of ``("implementer", "verifier",
  "reviewer")`` (fix bounces and follow-ups are implementer-phase turns), so a
  reviewer can carry its own checklist.
- **Caps** — per-entry inline limit and a total rendered-block limit; overflow
  degrades to artifact references or a visible dropped-labels note, never an error
  (C3). Context is data: Maestro stages and references files but never executes
  context content (C1).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PHASES = ("implementer", "verifier", "reviewer")
FILE_INLINE_LIMIT = 8192  # bytes; larger files become artifact references
TOTAL_CONTEXT_LIMIT = 32768  # bytes; cap on the rendered context block
_KINDS = ("text", "file", "skill")


@dataclass(frozen=True)
class ContextEntry:
    """One validated context entry (label + exactly one of text/path)."""

    label: str
    kind: str  # "text" | "file" | "skill"
    text: str | None = None
    path: str | None = None
    phases: tuple[str, ...] = PHASES
    source: str = ""  # "user config" | "project config" | "handoff" (stamped at compose time)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"label": self.label, "kind": self.kind}
        if self.text is not None:
            data["text"] = self.text
        if self.path is not None:
            data["path"] = self.path
        if self.phases != PHASES:
            data["phases"] = list(self.phases)
        if self.source:
            data["source"] = self.source
        return data


def parse_entry(raw: Any, source: str = "") -> ContextEntry:
    """Validate one context entry (a mapping) and return a :class:`ContextEntry`.

    Raises ValueError naming the offending label on any violation (C5).
    """
    if not isinstance(raw, dict):
        raise ValueError("context entries must be tables")
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("each context entry needs a non-empty 'label'")
    label = label.strip()
    text = raw.get("text")
    path = raw.get("path")
    kind = raw.get("kind")
    if kind is None:
        kind = "text" if text is not None else "file"
    if kind not in _KINDS:
        raise ValueError(f"context entry {label!r}: kind must be one of {_KINDS}, got {kind!r}")
    if kind == "text":
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"context entry {label!r}: text entries need a non-empty 'text'")
        if path is not None:
            raise ValueError(f"context entry {label!r}: set exactly one of 'text' or 'path'")
    else:
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"context entry {label!r}: {kind} entries need a 'path'")
        if text is not None:
            raise ValueError(f"context entry {label!r}: set exactly one of 'text' or 'path'")
    phases = raw.get("phases")
    if phases is None:
        phases = PHASES
    else:
        if not isinstance(phases, list) or not all(isinstance(p, str) for p in phases):
            raise ValueError(f"context entry {label!r}: 'phases' must be a list of strings")
        if not phases:
            raise ValueError(f"context entry {label!r}: 'phases' must not be empty")
        unknown = [p for p in phases if p not in PHASES]
        if unknown:
            raise ValueError(f"context entry {label!r}: unknown phase(s) {unknown}; valid: {list(PHASES)}")
        phases = tuple(phases)
    return ContextEntry(label=label, kind=kind, text=text, path=path, phases=phases, source=source)


def parse_context_config(raw: Any, source: str) -> dict[str, ContextEntry]:
    """Parse a config ``[context]`` table (label-keyed entry tables).

    The table key is the label; an explicit conflicting ``label`` field is rejected.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("[context] must be a table of label-keyed entry tables")
    entries: dict[str, ContextEntry] = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            raise ValueError(f"[context.{name}] must be a table")
        inner_label = item.get("label")
        if inner_label is not None and str(inner_label) != name:
            raise ValueError(f"[context.{name}]: 'label' field conflicts with the table key {name!r}")
        entries[name] = parse_entry({**item, "label": name}, source=source)
    return entries


def compose_context(config_entries: dict[str, ContextEntry], handoff_raw: list[Any]) -> list[ContextEntry]:
    """Compose standing + handoff entries.

    Handoff entries override config entries by label; new labels append after the
    standing ones (most specific last). Returns a fresh ordered list.
    """
    composed: dict[str, ContextEntry] = {}
    order: list[str] = []
    for entry in config_entries.values():
        order.append(entry.label)
        composed[entry.label] = entry
    for raw in handoff_raw:
        entry = parse_entry(raw, source="handoff")
        if entry.label not in composed:
            order.append(entry.label)
        composed[entry.label] = entry
    return [composed[label] for label in order]


def entry_from_dict(raw: dict[str, Any]) -> ContextEntry:
    """Rebuild a validated entry from its stored (to_dict) form — no re-validation."""
    return ContextEntry(
        label=str(raw.get("label", "")),
        kind=str(raw.get("kind") or "text"),
        text=raw.get("text"),
        path=raw.get("path"),
        phases=tuple(raw.get("phases") or PHASES),
        source=str(raw.get("source") or ""),
    )


@dataclass(frozen=True)
class RenderedContext:
    """One turn's rendered context.

    ``block`` is the user-prompt CONTEXT block ("" when nothing applies);
    ``system_file`` holds standing-context text for the claude_code
    ``--append-system-prompt-file`` channel; ``skills_root`` is the staged directory
    passed to Claude Code via ``--add-dir`` (layout ``<root>/.claude/skills/<label>/``).
    """

    block: str
    system_file: Path | None = None
    skills_root: Path | None = field(default=None)


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "entry"


def _resolve_path(raw: str, workspace: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path


def _render_entry(entry: ContextEntry, workspace: Path, task_dir: Path) -> tuple[str, Path | None]:
    """Render one entry into its prompt part; returns (part_text, staged_skill_path|None)."""
    if entry.kind == "text":
        return f"[{entry.label}] ({entry.source})\n{entry.text}\n", None
    path = _resolve_path(entry.path or "", workspace)
    if entry.kind == "file":
        try:
            data = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return f"[{entry.label}] ({entry.source})\n(file unreadable: {path})\n", None
        if len(data.encode("utf-8")) <= FILE_INLINE_LIMIT:
            return f"[{entry.label}] ({entry.source})\n{data}\n", None
        task_dir.mkdir(parents=True, exist_ok=True)
        artifact = task_dir / f"context-{_slug(entry.label)}.txt"
        artifact.write_text(data, encoding="utf-8")
        return f"[{entry.label}] ({entry.source})\n(file too large to inline; full content at {artifact})\n", None
    # skill: stage a hermetic copy (D2) under the Claude Code discovery layout.
    skills_root = task_dir / "context" / "skills"
    staged = skills_root / ".claude" / "skills" / _slug(entry.label)
    if staged.exists():
        shutil.rmtree(staged)  # re-renders (follow-ups) must see the current bytes
    shutil.copytree(path, staged)
    return f'Skill "{entry.label}" is available at {staged} — read its SKILL.md and follow it.\n', staged


def render_context(
    entries: list[ContextEntry],
    phase: str,
    workspace: Path,
    task_dir: Path,
    adapter_kind: str,
) -> RenderedContext:
    """Render composed entries for one turn.

    For ``adapter_kind == "claude_code"`` the standing (non-handoff) text/file entries
    render into ``task_dir/context-system.md`` instead of the user block (D1); skill
    availability lines always stay in the user block so every adapter can trigger them.
    Entries that would push the rendered total past TOTAL_CONTEXT_LIMIT are dropped
    and listed in a trailing note (C3).
    """
    applicable = [e for e in entries if phase in e.phases]
    user_parts: list[str] = []
    system_parts: list[str] = []
    skills_root: Path | None = None
    dropped: list[str] = []
    total = 0
    for entry in applicable:
        part, staged = _render_entry(entry, workspace, task_dir)
        if staged is not None and skills_root is None:
            skills_root = task_dir / "context" / "skills"
        goes_system = adapter_kind == "claude_code" and entry.source != "handoff" and entry.kind != "skill"
        if total + len(part) > TOTAL_CONTEXT_LIMIT:
            dropped.append(entry.label)
            continue
        (system_parts if goes_system else user_parts).append(part)
        total += len(part)
    system_file: Path | None = None
    if system_parts:
        task_dir.mkdir(parents=True, exist_ok=True)
        system_file = task_dir / "context-system.md"
        system_file.write_text("".join(system_parts), encoding="utf-8")
    block = ""
    if user_parts or dropped:
        header = "CONTEXT (user-provided; follow these along with the request):"
        body = "\n".join(user_parts)
        block = header + ("\n\n" + body if body else "") + "\n"
        if dropped:
            block += f"(context entries over the size cap were not included: {', '.join(dropped)})\n"
    return RenderedContext(block=block, system_file=system_file, skills_root=skills_root)
