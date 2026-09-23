"""The standard Maestro Handoff Document (4-section contract).

Every delegation between agents — human or machine — is normalized to this shape:

    [handoff]       what to do      (title, request, design, context pointers)
    [routing]       who does it     (target_agent, fallback, origin_agent, parent_task,
                                     plus work-mode pins: mode, review_agent, verify_agent,
                                     fix_agent, max_bounces — see maestro/modes.py)
    [expectations]  what done looks like (artifacts, verification, commit_policy, budget_hint)
    [constraints]   guardrails inherited from config (sensitive, max_depth_remaining)

Plus an optional ``[[context]]`` array of typed context entries (see
maestro/context.py) — user-controlled context composed with standing config entries
at delegate time.

The document is carried as structured data in A2A message parts and stored with the
task. Legacy 0.8.x staged handoffs (JSON: title/request/design_file/model/effort)
are converted via :func:`from_legacy`.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import parse_entry

COMMIT_POLICIES = ("no-commit", "branch", "pr")
VERIFICATION_MODES = ("auto", "command", "none")


@dataclass
class HandoffDoc:
    # [handoff]
    title: str
    request: str
    design: str = ""
    context_files: list[str] = field(default_factory=list)
    context_notes: str = ""
    # [[context]] — typed context entries (label + text|path, optional kind/phases).
    # Stored as plain dicts; composed with standing config entries at delegate time
    # and replaced by the merged list so the task record is self-contained (C2).
    context_entries: list[dict[str, Any]] = field(default_factory=list)
    # [routing]
    target_agent: str = "codex"
    fallback: list[str] = field(default_factory=list)
    origin_agent: str = "human"
    parent_task_id: str | None = None
    # Work-mode routing (see maestro/modes.py): a preset name plus per-phase agent
    # pins. None means "not set" — the daemon resolves defaults at delegate time.
    mode: str | None = None
    review_agent: str | None = None
    verify_agent: str | None = None
    fix_agent: str | None = None
    max_bounces: int | None = None
    # Parse metadata (not serialized): True when the document itself named a target,
    # so a work-mode preset does not silently override an explicit choice.
    explicit_target: bool = False
    # [expectations]
    artifacts: list[str] = field(default_factory=lambda: ["code"])
    verification: str = "auto"
    # The command run when verification == "command". None means the command
    # is the request itself (the original shape of command mode).
    verification_command: str | None = None
    commit_policy: str = "branch"
    budget_hint: float | None = None
    # [constraints]
    sensitive: bool = False
    max_depth_remaining: int = 3
    # Per-agent settings overrides for this task (model/effort/etc.).
    agent_settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff": {
                "title": self.title,
                "request": self.request,
                "design": self.design,
                "context_files": list(self.context_files),
                "context_notes": self.context_notes,
            },
            "routing": {
                "target_agent": self.target_agent,
                "fallback": list(self.fallback),
                "origin_agent": self.origin_agent,
                "parent_task_id": self.parent_task_id,
                "mode": self.mode,
                "review_agent": self.review_agent,
                "verify_agent": self.verify_agent,
                "fix_agent": self.fix_agent,
                "max_bounces": self.max_bounces,
                # Whether the target was named by the user (True) or is just a
                # default awaiting work-mode expansion (False). Must cross the
                # wire: without it, from_dict's legacy heuristic treats the
                # default as explicit and mode presets never pin the implementer.
                "explicit_target": self.explicit_target,
            },
            "expectations": {
                "artifacts": list(self.artifacts),
                "verification": self.verification,
                "verification_command": self.verification_command,
                "commit_policy": self.commit_policy,
                "budget_hint": self.budget_hint,
            },
            "constraints": {
                "sensitive": self.sensitive,
                "max_depth_remaining": self.max_depth_remaining,
            },
            "context": [dict(e) for e in self.context_entries],
            "agent_settings": dict(self.agent_settings),
        }


def validate_handoff(doc: HandoffDoc) -> HandoffDoc:
    if not doc.title or not str(doc.title).strip():
        raise ValueError("Handoff requires a non-empty title")
    if not doc.request or not str(doc.request).strip():
        raise ValueError("Handoff requires a non-empty request")
    if not doc.target_agent or not str(doc.target_agent).strip():
        raise ValueError("Handoff routing requires a target_agent")
    if doc.commit_policy not in COMMIT_POLICIES:
        raise ValueError(f"commit_policy must be one of {COMMIT_POLICIES}: {doc.commit_policy!r}")
    if doc.verification not in VERIFICATION_MODES:
        raise ValueError(f"verification must be one of {VERIFICATION_MODES}: {doc.verification!r}")
    if doc.verification_command is not None:
        if not isinstance(doc.verification_command, str) or not doc.verification_command.strip():
            raise ValueError("verification_command must be a non-empty string when set")
        if doc.verification != "command":
            raise ValueError("verification_command requires verification = 'command'")
    if doc.max_depth_remaining < 0:
        raise ValueError("max_depth_remaining must be >= 0")
    if doc.max_bounces is not None and (isinstance(doc.max_bounces, bool) or not isinstance(doc.max_bounces, int) or doc.max_bounces < 0):
        raise ValueError("max_bounces must be an integer >= 0 when set")
    if doc.budget_hint is not None and float(doc.budget_hint) <= 0:
        raise ValueError("budget_hint must be positive when set")
    for agent in doc.fallback:
        if not str(agent).strip():
            raise ValueError("Fallback agents must be non-empty strings")
    for entry in doc.context_entries:
        parse_entry(entry, source="handoff")
    return doc


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"Section [{name}] must be a table")
    return value


def from_dict(data: dict[str, Any]) -> HandoffDoc:
    """Parse the 4-section document shape (as produced by to_dict / TOML)."""
    if not isinstance(data, dict):
        raise ValueError("Handoff document must be a mapping")
    handoff = _section(data, "handoff")
    routing = _section(data, "routing")
    expectations = _section(data, "expectations")
    constraints = _section(data, "constraints")
    doc = HandoffDoc(
        title=str(handoff.get("title", "")),
        request=str(handoff.get("request", "")),
        design=str(handoff.get("design", "")),
        context_files=[str(x) for x in handoff.get("context_files", [])],
        context_notes=str(handoff.get("context_notes", "")),
        context_entries=list(data.get("context", [])),
        target_agent=str(routing.get("target_agent") or "codex"),
        fallback=[str(x) for x in routing.get("fallback", [])],
        origin_agent=str(routing.get("origin_agent") or "human"),
        parent_task_id=routing.get("parent_task_id"),
        mode=routing.get("mode"),
        review_agent=routing.get("review_agent"),
        verify_agent=routing.get("verify_agent"),
        fix_agent=routing.get("fix_agent"),
        max_bounces=routing.get("max_bounces"),
        # New payloads carry the flag explicitly; old persisted records lack it,
        # so fall back to the legacy heuristic (a named target counts as explicit).
        explicit_target=bool(routing.get("explicit_target", "target_agent" in routing)),
        artifacts=[str(x) for x in expectations.get("artifacts", ["code"])],
        verification=str(expectations.get("verification") or "auto"),
        verification_command=expectations.get("verification_command") or None,
        commit_policy=str(expectations.get("commit_policy") or "branch"),
        budget_hint=expectations.get("budget_hint"),
        sensitive=bool(constraints.get("sensitive", False)),
        max_depth_remaining=int(constraints.get("max_depth_remaining", 3)),
        agent_settings=dict(data.get("agent_settings", {})),
    )
    return validate_handoff(doc)


def from_toml(text: str) -> HandoffDoc:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid handoff TOML: {exc}") from exc
    return from_dict(data)


def to_toml(doc: HandoffDoc) -> str:
    """Serialize using the same minimal writer as the agent registry."""
    from .agents import _dump_toml

    data = doc.to_dict()
    flat: dict[str, Any] = {
        section: {k: v for k, v in data[section].items() if v is not None}
        for section in ("handoff", "routing", "expectations", "constraints", "agent_settings")
    }
    if doc.context_entries:
        flat["context"] = [{k: v for k, v in entry.items() if v is not None} for entry in doc.context_entries]
    # 'request'/'design' may be multi-line strings — the minimal writer escapes \n.
    return _dump_toml(flat)


def from_legacy(payload: dict[str, Any]) -> HandoffDoc:
    """Convert a 0.8.x staged handoff (JSON: title/request/design_file/model/effort)."""
    if not isinstance(payload, dict):
        raise ValueError("Legacy handoff must be a mapping")
    for key in ("title", "request"):
        if not payload.get(key):
            raise ValueError(f"Legacy handoff is missing required field: {key}")
    design = ""
    design_file = payload.get("design_file")
    if design_file:
        from pathlib import Path

        design = Path(str(design_file)).read_text(encoding="utf-8")
    doc = HandoffDoc(
        title=str(payload["title"]),
        request=str(payload["request"]),
        design=design,
        target_agent=str(payload.get("implementer") or "codex"),
        # Old 0.8.x files rarely name their supervisor; record the honest generic
        # default instead of assuming a specific agent played that role.
        origin_agent=str(payload.get("supervisor") or "human"),
        commit_policy="branch",
    )
    settings: dict[str, Any] = {}
    if payload.get("model"):
        settings["model"] = str(payload["model"])
    if payload.get("effort"):
        settings["effort"] = str(payload["effort"])
    doc.agent_settings = settings
    return validate_handoff(doc)


def load_handoff_file(path: "str | Path") -> HandoffDoc:
    """Load a staged handoff file in any supported shape.

    Accepts the 4-section document (JSON or TOML) and legacy 0.8.x JSON
    (title/request/design_file/model/effort).
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"Handoff file does not exist: {p}")
    text = p.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return from_toml(text)
    if not isinstance(payload, dict):
        raise ValueError("Handoff file must contain an object")
    if any(section in payload for section in ("handoff", "routing", "expectations", "constraints", "context")):
        return from_dict(payload)
    if payload.get("title") and payload.get("request"):
        return from_legacy(payload)
    raise ValueError("Handoff file is neither a 4-section document nor a legacy handoff (needs title + request)")
