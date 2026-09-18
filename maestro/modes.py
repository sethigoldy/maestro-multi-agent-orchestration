"""Work modes: named presets that pin agents to task-cycle phases.

A preset maps the model-running phases of a task (implement / verify / review / fix)
to registered agent names, so cost/quality profiles ("economy", "tri-agent", ...) are
config data rather than supervisor discipline. Presets live in the standard Maestro
config chain under ``[modes.<name>]`` tables:

    [modes.economy]
    implementer = "codex-mini"   # required — becomes the handoff target_agent
    verifier    = "codex-mini"   # optional LLM verification pass (VERIFYING)
    reviewer    = "codex"        # optional LLM review gate (REVIEWING)
    fixer       = "codex-mini"   # optional — defaults to implementer (FIXING)
    max_bounces = 2              # optional auto-fix cap (default 2; 0 = park on first issue)

Expansion maps a preset onto the handoff's routing fields at delegate time. Explicit
routing fields in the handoff always win over the preset, except that the preset's
implementer takes over ``target_agent`` when the handoff did not name one explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Auto-fix bounce cap applied when neither the handoff nor its preset sets one.
DEFAULT_MAX_BOUNCES = 2

_PRESET_KEYS = ("implementer", "verifier", "reviewer", "fixer", "max_bounces")


@dataclass(frozen=True)
class ModePreset:
    """One validated ``[modes.<name>]`` table."""

    name: str
    implementer: str
    verifier: str | None = None
    reviewer: str | None = None
    fixer: str | None = None
    max_bounces: int = DEFAULT_MAX_BOUNCES

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view (used by ``maestro config``)."""
        return {
            "implementer": self.implementer,
            "verifier": self.verifier,
            "reviewer": self.reviewer,
            "fixer": self.fixer,
            "max_bounces": self.max_bounces,
        }


def _agent_slot(table: dict, key: str, preset_name: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[modes.{preset_name}] {key} must be a non-empty agent name")
    return value


def parse_modes(raw: object) -> dict[str, ModePreset]:
    """Parse and validate the ``[modes]`` table from merged config.

    ``raw`` is the value under the top-level ``modes`` key (may be absent). Returns a
    mapping of preset name to :class:`ModePreset`; raises ``ValueError`` on any
    malformed table so misconfiguration fails at daemon start, not mid-delegation.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("[modes] must be a table of preset tables")
    out: dict[str, ModePreset] = {}
    for name, table in raw.items():
        preset_name = str(name)
        if not isinstance(table, dict):
            raise ValueError(f"[modes.{preset_name}] must be a table")
        unknown = set(table) - set(_PRESET_KEYS)
        if unknown:
            raise ValueError(f"[modes.{preset_name}] has unknown keys: {', '.join(sorted(unknown))}")
        implementer = _agent_slot(table, "implementer", preset_name)
        if implementer is None:
            raise ValueError(f"[modes.{preset_name}] requires an 'implementer' agent name")
        max_bounces = table.get("max_bounces", DEFAULT_MAX_BOUNCES)
        if isinstance(max_bounces, bool) or not isinstance(max_bounces, int) or max_bounces < 0:
            raise ValueError(f"[modes.{preset_name}] max_bounces must be an integer >= 0")
        out[preset_name] = ModePreset(
            name=preset_name,
            implementer=implementer,
            verifier=_agent_slot(table, "verifier", preset_name),
            reviewer=_agent_slot(table, "reviewer", preset_name),
            fixer=_agent_slot(table, "fixer", preset_name),
            max_bounces=max_bounces,
        )
    return out


def resolve_mode(modes: dict[str, ModePreset], name: str) -> ModePreset:
    """Return the named preset or raise ``ValueError`` listing what is defined."""
    preset = modes.get(name)
    if preset is None:
        available = ", ".join(sorted(modes)) or "none"
        raise ValueError(f"Unknown work mode {name!r}. Defined modes: {available}")
    return preset


def expand(preset: ModePreset, doc) -> object:
    """Apply a preset to a handoff document, in place, and return it.

    Precedence: explicit routing fields on the document win; the preset fills only
    what is unset. The preset's implementer becomes ``target_agent`` unless the
    document named a target explicitly (``doc.explicit_target``). When the preset has
    no fixer, the fix slot defaults to the implementer (safe default: no implicit
    expensive spend).
    """
    if not doc.explicit_target:
        doc.target_agent = preset.implementer
    if doc.review_agent is None and preset.reviewer is not None:
        doc.review_agent = preset.reviewer
    if doc.verify_agent is None and preset.verifier is not None:
        doc.verify_agent = preset.verifier
    if doc.fix_agent is None:
        doc.fix_agent = preset.fixer or preset.implementer
    if doc.max_bounces is None:
        doc.max_bounces = preset.max_bounces
    return doc
