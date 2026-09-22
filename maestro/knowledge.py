"""Task knowledge: a compact, durable continuation context for task turns.

A Maestro task is durable; a turn is an execution. Over its lifetime a task
accumulates raw execution history (claim journal entries, per-turn result
files, agent logs, verification reports) — the complete audit trail. That
history is never replayed wholesale into later turns: instead this module
projects it into a small structured snapshot, :class:`TaskKnowledge`, that is
persisted as one ``task_knowledge`` claim in the existing claim journal (so it
works identically on the filesystem and memvara backends and survives daemon
restarts) and rendered into a budgeted continuation block for follow-up turns.

Design notes and the architecture audit that preceded this feature live in
``docs/design-task-continuation.md``. Deterministic extraction only — v1 has no
LLM summarization: ``decisions``/``assumptions`` are reserved fields that stay
empty until a future release can populate them honestly.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: Deterministic chars-per-token estimate used for the continuation budget.
#: A labeled approximation (never an exact tokenizer count).
CHARS_PER_TOKEN = 4

DEFAULT_MAX_TOKENS = 6000

#: Section names in value order: highest-value first. When the budget is
#: exceeded, lowest-value sections are dropped before any truncation happens.
_SECTION_ORDER = ("goal", "current_state", "verification", "known_issues", "constraints", "latest_summary")

#: A2A state for each durable phase (mirrors the receipt's mapping so the
#: projection stays consistent without importing the daemon).
_PHASE_TO_STATE = {
    "DESIGNED": "submitted",
    "IMPLEMENTING": "working",
    "VERIFYING": "working",
    "FIXING": "working",
    "REVIEWING": "completed",
    "COMPLETE": "completed",
    "FAILED": "failed",
}

_FAILURE_LINE_RE = re.compile(r"^FAILED\s+\S+")


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (chars / CHARS_PER_TOKEN).

    An honest approximation for budgeting and reporting — clearly labeled as an
    estimate everywhere it is surfaced, never presented as an exact count.
    """
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def parse_continuation(raw: Any) -> dict[str, Any]:
    """Parse and validate the ``[continuation]`` config table.

    ``enabled`` (bool, default true) switches reuse-mode knowledge injection on
    follow-ups; ``max_tokens`` (positive int, default 6000) is the estimated
    token budget for the rendered continuation block. Absent table -> defaults;
    malformed values raise so misconfiguration fails at daemon start.
    """
    if raw is None:
        return {"enabled": True, "max_tokens": DEFAULT_MAX_TOKENS}
    if not isinstance(raw, dict):
        raise ValueError("[continuation] must be a table")
    unknown = set(raw) - {"enabled", "max_tokens"}
    if unknown:
        raise ValueError(f"[continuation] has unknown keys: {', '.join(sorted(unknown))}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("[continuation] enabled must be a boolean")
    max_tokens = raw.get("max_tokens", DEFAULT_MAX_TOKENS)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("[continuation] max_tokens must be a positive integer")
    return {"enabled": enabled, "max_tokens": max_tokens}


def continuation_budget_chars(config: dict[str, Any]) -> int:
    """Character budget for the continuation block from merged config.

    ``MAESTRO_CONTINUATION_MAX_TOKENS`` overrides the configured value (an
    invalid override falls back to config, matching other MAESTRO_* env knobs).
    """
    cont = parse_continuation(config.get("continuation") if isinstance(config, dict) else None)
    raw_env = os.environ.get("MAESTRO_CONTINUATION_MAX_TOKENS", "").strip()
    if raw_env:
        try:
            return max(1, int(raw_env)) * CHARS_PER_TOKEN
        except ValueError:
            pass  # invalid override: fall back to the configured budget
    return cont["max_tokens"] * CHARS_PER_TOKEN


@dataclass
class TaskKnowledge:
    """One task's compact continuation snapshot (schema v1).

    Every field is derived from durable state at projection time; the object
    itself carries no live references. ``decisions``/``assumptions`` are
    reserved for future deterministic or agent-reported knowledge and are empty
    in v1 (see the module docstring).
    """

    schema_version: int = SCHEMA_VERSION
    task_id: str = ""
    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    current_state: str = ""
    verification: dict[str, Any] = field(default_factory=lambda: {"status": None, "command": None, "failures": []})
    known_issues: list[str] = field(default_factory=list)
    latest_summary: str = ""
    last_updated: str = ""
    source_turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "goal": self.goal,
            "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "assumptions": list(self.assumptions),
            "files_changed": list(self.files_changed),
            "current_state": self.current_state,
            "verification": {
                "status": self.verification.get("status"),
                "command": self.verification.get("command"),
                "failures": list(self.verification.get("failures") or []),
            },
            "known_issues": list(self.known_issues),
            "latest_summary": self.latest_summary,
            "last_updated": self.last_updated,
            "source_turn": self.source_turn,
        }

    def serialize(self) -> str:
        """Canonical JSON form (sorted keys) — same knowledge, same bytes."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @staticmethod
    def from_dict(data: Any) -> "TaskKnowledge | None":
        """Tolerant reconstruction of a stored snapshot.

        Missing optional fields fall back to defaults (older snapshots), unknown
        fields are ignored (future versions). Returns ``None`` when the payload
        is not a mapping or carries no usable schema version — callers treat
        that as "no knowledge yet", never as an error.
        """
        if not isinstance(data, dict):
            return None
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return None

        def _str_list(key: str) -> list[str]:
            value = data.get(key)
            if not isinstance(value, list):
                return []
            return [str(x) for x in value]

        verification = data.get("verification")
        if not isinstance(verification, dict):
            verification = {}
        failures = verification.get("failures")
        source_turn = data.get("source_turn")
        return TaskKnowledge(
            schema_version=version,
            task_id=str(data.get("task_id") or ""),
            goal=str(data.get("goal") or ""),
            constraints=_str_list("constraints"),
            decisions=_str_list("decisions"),
            assumptions=_str_list("assumptions"),
            files_changed=_str_list("files_changed"),
            current_state=str(data.get("current_state") or ""),
            verification={
                "status": verification.get("status"),
                "command": verification.get("command"),
                "failures": [str(x) for x in failures] if isinstance(failures, list) else [],
            },
            known_issues=_str_list("known_issues"),
            latest_summary=str(data.get("latest_summary") or ""),
            last_updated=str(data.get("last_updated") or ""),
            source_turn=0 if isinstance(source_turn, bool) or not isinstance(source_turn, int) else source_turn,
        )


def _tail(text: str, limit: int) -> str:
    """Bounded tail of a text with a visible truncation marker."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"...[truncated {len(text) - limit} chars] " + text[-limit:]


def _git_changed_files(workspace: Path | None) -> list[str]:
    """Changed files in the workspace (tracked diff vs HEAD + untracked).

    The live filesystem is authoritative for implementation state; any git
    failure (missing dir, not a repo, no commits yet) degrades to an empty
    list rather than an error.
    """
    if workspace is None or not workspace.is_dir():
        return []
    try:
        diff = subprocess.run(["git", "-C", str(workspace), "diff", "--name-only", "HEAD"], text=True, capture_output=True, timeout=10)
        untracked = subprocess.run(["git", "-C", str(workspace), "ls-files", "--others", "--exclude-standard"], text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    tracked = [line.strip() for line in diff.stdout.splitlines() if line.strip()] if diff.returncode == 0 else []
    others = [line.strip() for line in untracked.stdout.splitlines() if line.strip()] if untracked.returncode == 0 else []
    return sorted(set(tracked) | set(others))


def _parse_verification_claim(claim: str | None) -> dict[str, Any]:
    """Project the durable ``task_verification`` claim into knowledge fields."""
    section = {"status": None, "command": None, "failures": []}
    if not claim:
        return section
    result, _, report = str(claim).partition(": ")
    section["status"] = result or None
    if not report:
        return section
    path = Path(report)
    if not path.is_file():
        return section
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return section
    for line in text.splitlines():
        if line.startswith("verification command: "):
            section["command"] = line[len("verification command: "):].strip() or None
            break
    failures = [line.strip() for line in text.splitlines() if _FAILURE_LINE_RE.match(line)]
    if failures:
        section["failures"] = failures[:20]
    elif result == "FAILED":
        # No parseable failure lines: keep a bounded tail of the report so the
        # continuation still shows what went wrong.
        section["failures"] = [_tail(text, 500)]
    return section


def _known_issues(claims: dict[str, str], runtime: dict[str, Any]) -> list[str]:
    """Gate verdict issues plus failed-attempt errors (deduped, capped)."""
    issues: list[str] = []
    raw_gates = claims.get("task_gates")
    if raw_gates:
        try:
            parsed = json.loads(raw_gates)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("verdicts"), dict):
            for role, verdict in parsed["verdicts"].items():
                if isinstance(verdict, dict):
                    for issue in verdict.get("issues") or []:
                        issues.append(f"{role}: {issue}")
    for attempt in runtime.get("attempts") or []:
        if isinstance(attempt, dict) and not attempt.get("ok"):
            error = str(attempt.get("error") or "").strip()
            if error:
                issues.append(error.splitlines()[0])
    seen: set[str] = set()
    out: list[str] = []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            out.append(issue)
    return out[:10]


def _latest_summary(runtime: dict[str, Any], state: str) -> str:
    """Bounded tail of the last turn's output, or its error, or the state."""
    result = runtime.get("result")
    if isinstance(result, dict):
        path = result.get("output_path")
        if path:
            try:
                return _tail(Path(str(path)).read_text(encoding="utf-8", errors="replace"), 1500)
            except OSError:
                pass
    for attempt in reversed(runtime.get("attempts") or []):
        if isinstance(attempt, dict) and not attempt.get("ok"):
            error = str(attempt.get("error") or "").strip()
            if error:
                return _tail(error, 800)
    return state or "no execution recorded"


def project_knowledge(
    task_id: str,
    claims: dict[str, str],
    runtime: dict[str, Any],
    *,
    workspace: Path | None = None,
) -> TaskKnowledge:
    """Project durable task state into a fresh :class:`TaskKnowledge`.

    Pure and deterministic with respect to its inputs (claims from the journal,
    the parsed ``task_runtime`` snapshot, live git state): no daemon, no LLM.
    Called at every terminal transition and again when a continuation starts,
    so the persisted snapshot always reflects the latest durable reality.
    """
    if not isinstance(runtime, dict):
        runtime = {}
    doc = runtime.get("doc") if isinstance(runtime.get("doc"), dict) else {}
    expectations = doc.get("expectations") if isinstance(doc.get("expectations"), dict) else {}
    constraints_doc = doc.get("constraints") if isinstance(doc.get("constraints"), dict) else {}

    constraints: list[str] = []
    verification_mode = expectations.get("verification")
    commit_policy = expectations.get("commit_policy")
    if verification_mode:
        constraints.append(f"verification={verification_mode}")
    if commit_policy:
        constraints.append(f"commit_policy={commit_policy}")
    if constraints_doc.get("sensitive"):
        constraints.append("sensitive workspace (approval-gated)")

    state = str(runtime.get("state") or _PHASE_TO_STATE.get(str(claims.get("task_status") or ""), "unknown"))
    branch = claims.get("task_branch") or runtime.get("branch")
    turn_raw = runtime.get("turn")
    turn = 0 if isinstance(turn_raw, bool) or not isinstance(turn_raw, int) else turn_raw
    attempts = [a for a in (runtime.get("attempts") or []) if isinstance(a, dict)]

    state_parts = [state]
    if branch:
        state_parts.append(f"branch {branch}")
    if turn:
        state_parts.append(f"turn {turn}")
    if attempts:
        last_agent = str(attempts[-1].get("agent") or "")
        if last_agent:
            state_parts.append(f"last agent {last_agent}")

    return TaskKnowledge(
        task_id=str(task_id),
        goal=str(claims.get("task_request") or ""),
        constraints=constraints,
        files_changed=_git_changed_files(workspace),
        current_state=", ".join(state_parts),
        verification=_parse_verification_claim(claims.get("task_verification")),
        known_issues=_known_issues(claims, runtime),
        latest_summary=_latest_summary(runtime, state),
        last_updated=datetime.now(timezone.utc).isoformat(),
        source_turn=turn,
    )


def _render_section(name: str, knowledge: TaskKnowledge) -> str:
    if name == "goal":
        return f"GOAL:\n{knowledge.goal}" if knowledge.goal else ""
    if name == "current_state":
        files = ", ".join(knowledge.files_changed) if knowledge.files_changed else ""
        state = knowledge.current_state or ""
        if not state and not files:
            return ""  # nothing known yet: do not inject an empty shell
        return f"CURRENT STATE:\n{state or '(unknown)'}\nFiles changed: {files or '(none detected)'}"
    if name == "verification":
        status = knowledge.verification.get("status")
        if not status:
            return ""
        command = f" ({knowledge.verification['command']})" if knowledge.verification.get("command") else ""
        failures = knowledge.verification.get("failures") or []
        failure_text = "\n".join(f"- {f}" for f in failures) if failures else "(no failure lines recorded)"
        return f"VERIFICATION: {status}{command}\n{failure_text}"
    if name == "known_issues":
        if not knowledge.known_issues:
            return ""
        return "KNOWN ISSUES:\n" + "\n".join(f"- {i}" for i in knowledge.known_issues)
    if name == "constraints":
        if not knowledge.constraints:
            return ""
        return "CONSTRAINTS:\n" + "\n".join(f"- {c}" for c in knowledge.constraints)
    # latest_summary
    return f"LATEST SUMMARY (tail of the last turn's output):\n{knowledge.latest_summary}" if knowledge.latest_summary else ""


def _omission_note(header_len: int, budget: int, names: list[str]) -> str:
    """The omission note for dropped sections, sized to fit the budget."""
    if not names:
        return ""
    note = f"\n(continuation context budget exceeded; truncated or omitted: {', '.join(names)})"
    if header_len + len(note) > budget:
        note = "\n[context truncated]"  # minimal marker for tiny budgets
    if header_len + len(note) > budget:
        note = ""  # pathological budget: the header alone is all that fits
    return note


def render_continuation_block(knowledge: TaskKnowledge, budget_chars: int) -> str:
    """Render the compact continuation context within a character budget.

    Sections appear in value order (goal first). When the budget is exceeded,
    lowest-value sections are dropped and the first non-fitting section is
    truncated with visible markers; an omission note lists what was left out.
    The rendered block never exceeds ``budget_chars`` (the note and the
    separators between sections count too). The new instruction is never part
    of this block (it travels as the handoff's request), so it can never be
    truncated here. Returns "" when there is nothing to inject or the budget
    cannot fit even the header.
    """
    sections: list[tuple[str, str]] = []
    for name in _SECTION_ORDER:
        text = _render_section(name, knowledge)
        if text:
            sections.append((name, text))
    if not sections:
        return ""
    header = "TASK KNOWLEDGE — compact continuation snapshot (raw history stays in the task record):\n"
    budget = max(0, int(budget_chars))
    if budget < len(header):
        return ""

    # Deterministic search: keep as many highest-value sections as fit; at the
    # cutoff, truncate that section to fill the remaining room (note and join
    # separators included). Trying cutoffs from most-kept to least-kept makes
    # the result a function of (knowledge, budget) alone.
    for kept_count in range(len(sections), 0, -1):
        kept = sections[:kept_count]
        kept_cost = sum(len(t) for _, t in kept) + max(0, len(kept) - 1)  # join separators
        note = _omission_note(len(header), budget, [n for n, _ in sections[kept_count:]])
        base = len(header) + kept_cost + len(note)
        if base > budget:
            continue
        if kept_count == len(sections):
            return header + "\n".join(t for _, t in kept)  # everything fits
        room = budget - base
        marker = "\n[truncated to fit continuation budget]"
        sep = 1 if kept else 0
        if room >= sep + len(marker) + 1:
            parts = [t for _, t in kept] + [sections[kept_count][1][: room - sep - len(marker)] + marker]
            return header + "\n".join(parts) + note
        # Not even the marker fits: drop the cutoff section, keep the note.
        return header + "\n".join(t for _, t in kept) + note
    # No positive cutoff fit: truncate the first (highest-value) section into
    # whatever room remains. This always fits — _omission_note degrades until
    # header + note <= budget, leaving at least zero room for content.
    note = _omission_note(len(header), budget, [n for n, _ in sections])
    room = budget - len(header) - len(note)
    marker = "\n[truncated to fit continuation budget]"
    if room >= len(marker) + 1:
        return header + sections[0][1][: room - len(marker)] + marker + note
    return header + note
