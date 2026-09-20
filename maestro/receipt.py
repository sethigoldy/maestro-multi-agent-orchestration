"""Execution receipts: a durable, serializable projection of a task's run.

A receipt is the final readable outcome of a task: identity, terminal state,
every attempt (agent, phase, duration, cost), the deterministic verification
result, work-mode gates, and totals. It is *derived*, never stored as a second
source of truth — :func:`build_receipt` projects the task's registry record,
claim journal (status, workspace, branch, request, verification, gates, runtime
snapshot) and artifact files into one document that survives daemon restarts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .budgets import cost_of

#: Phase (durable ``task_status`` claim) -> A2A state. Used only when the
#: runtime snapshot is absent (pre-0.9 records). REVIEWING maps to completed
#: per the 0.8.x semantics: finished work awaiting cross-review.
_STATE_BY_PHASE = {
    "DESIGNED": "submitted",
    "IMPLEMENTING": "working",
    "VERIFYING": "working",
    "FIXING": "working",
    "REVIEWING": "completed",
    "COMPLETE": "completed",
    "FAILED": "failed",
}

#: Attempt role -> receipt phase label (the IMPLEMENT/VERIFY/REVIEW/FIX chain).
_PHASE_BY_ROLE = {
    "implement": "IMPLEMENT",
    "verifier": "VERIFY",
    "reviewer": "REVIEW",
    "fix": "FIX",
}


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _attempt_view(raw: dict[str, Any], n: int) -> tuple[dict[str, Any], datetime | None]:
    """One raw runtime attempt entry -> receipt view (+ parsed finish time)."""
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
    cost = cost_of(usage) if usage is not None else 0.0
    role = str(raw.get("role") or "implement")
    return (
        {
            "n": n,
            "agent": str(raw.get("agent") or "?"),
            "role": role,
            "phase": _PHASE_BY_ROLE.get(role, "IMPLEMENT"),
            "ok": bool(raw.get("ok")),
            "exit_code": raw.get("exit_code"),
            "duration_s": raw.get("duration_s"),
            # None = the adapter did not report a cost; never fabricated.
            "cost_usd": cost if (usage is not None and cost > 0) else None,
            "usage": usage,
            "error": raw.get("error"),
            "log": raw.get("result_file"),
        },
        _parse_ts(raw.get("finished_at")),
    )


def _verification_section(maestro: Any, task_id: str, claims: dict[str, str]) -> dict[str, Any]:
    """Project the durable ``task_verification`` claim(s) into a section."""
    claim = claims.get("task_verification")
    if not claim:
        return {"ran": False, "result": None, "command": None, "report": None, "attempts": 0}
    result, _, report = str(claim).partition(": ")
    command: str | None = None
    if report:
        path = Path(report)
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("verification command: "):
                        command = line[len("verification command: "):].strip() or None
                        break
            except OSError:
                command = None
    runs = len(maestro.mem.history(f"maestro:task:{task_id}", "task_verification"))
    return {
        "ran": True,
        "result": result.upper() if result in ("PASSED", "FAILED") else result,
        "command": command,
        "report": report or None,
        "attempts": max(runs, 1),
    }


def _gates_section(claims: dict[str, str], routing: dict[str, Any]) -> dict[str, Any]:
    """Project the durable ``task_gates`` claim plus the handoff's gate agents."""
    verdicts: dict[str, Any] = {}
    bounces: int | None = None
    raw_gates = claims.get("task_gates")
    if raw_gates:
        try:
            parsed = json.loads(raw_gates)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            raw_verdicts = parsed.get("verdicts")
            if isinstance(raw_verdicts, dict):
                verdicts = {
                    role: {"agent": v.get("agent"), "ok": v.get("ok"), "issues": list(v.get("issues") or [])}
                    for role, v in raw_verdicts.items()
                    if isinstance(v, dict)
                }
            bounces = parsed.get("bounces")
    return {
        "verifier": routing.get("verify_agent"),
        "reviewer": routing.get("review_agent"),
        "fixer": routing.get("fix_agent"),
        "verdicts": verdicts,
        "bounces": bounces,
    }


def build_receipt(task_id: str, maestro: Any, state_dir: str | Path | None = None) -> dict[str, Any]:
    """Project one task's durable state into an execution receipt.

    ``maestro`` is a :class:`maestro.core.Maestro` bound to the state directory
    that owns the task; ``state_dir`` is accepted for interface symmetry and
    currently unused (all sources live under the Maestro instance). The result
    is plain JSON-safe data — no live objects, no Host references.
    """
    task_id = str(task_id)
    claims = maestro._claims(task_id)
    record = next((x for x in maestro._registry_records() if str(x.get("task_id")) == task_id), None)

    runtime: dict[str, Any] = {}
    raw_runtime = claims.get("task_runtime")
    if isinstance(raw_runtime, str):
        try:
            parsed = json.loads(raw_runtime)
            if isinstance(parsed, dict):
                runtime = parsed
        except (ValueError, TypeError):
            runtime = {}

    state = runtime.get("state") or _STATE_BY_PHASE.get(str(claims.get("task_status")), "unknown")
    doc = runtime.get("doc") if isinstance(runtime.get("doc"), dict) else {}
    routing = doc.get("routing") if isinstance(doc.get("routing"), dict) else {}

    pairs: list[tuple[dict[str, Any], datetime | None]] = []
    for index, raw in enumerate(runtime.get("attempts") or []):
        if isinstance(raw, dict):
            pairs.append(_attempt_view(raw, len(pairs) + 1))
    verification = _verification_section(maestro, task_id, claims)
    gates = _gates_section(claims, routing or {})

    started_dt = _parse_ts(runtime.get("started_at"))
    finished_dts = [dt for _, dt in pairs if dt is not None]
    durations = [a["duration_s"] for a, _ in pairs if isinstance(a.get("duration_s"), (int, float))]
    cost_total = sum(a["cost_usd"] or 0.0 for a, _ in pairs)
    cost_reported = any(a["cost_usd"] is not None for a, _ in pairs)
    total_duration: float | None = None
    basis: str | None = None
    if started_dt is not None and finished_dts:
        total_duration = (max(finished_dts) - started_dt).total_seconds()
        basis = "wall_clock"
    elif durations:
        total_duration = sum(durations)
        basis = "attempt_sum"

    number = record.get("number") if record else None
    if number is None and claims.get("task_number"):
        try:
            number = int(claims["task_number"])
        except ValueError:
            number = None

    return {
        "task": {
            "id": task_id,
            "number": number,
            "title": claims.get("task_title") or (record or {}).get("title"),
            "request": claims.get("task_request"),
            "workspace": claims.get("task_workspace") or (record or {}).get("workspace"),
            "branch": claims.get("task_branch") or runtime.get("branch"),
            "created_at": (record or {}).get("created_at"),
            "completed_at": max(finished_dts).isoformat() if finished_dts else None,
        },
        "state": state,
        "error": runtime.get("error"),
        "attempts": [view for view, _ in pairs],
        "verification": verification,
        "gates": gates,
        "totals": {
            "duration_s": round(total_duration, 3) if total_duration is not None else None,
            "duration_basis": basis,
            "cost_usd": round(cost_total, 6) if cost_reported else None,
            "cost_reported": cost_reported,
            "attempts": len(pairs) + verification["attempts"],
            "agent_attempts": len(pairs),
            "verification_attempts": verification["attempts"],
            "review_attempts": sum(1 for a, _ in pairs if a["role"] == "reviewer"),
            "fix_attempts": sum(1 for a, _ in pairs if a["role"] == "fix"),
        },
    }


def _fmt_duration(seconds: Any) -> str | None:
    if seconds is None or isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    total = int(round(float(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _fmt_cost(value: Any) -> str | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"${value:.2f}"


def format_receipt(receipt: dict[str, Any]) -> str:
    """Render a receipt as the human-readable terminal document."""
    task = receipt.get("task") or {}
    totals = receipt.get("totals") or {}
    lines: list[str] = ["Maestro Execution Receipt", "-" * 40, ""]
    if task.get("number") is not None:
        lines.append(f"Task #{task['number']}")
    else:
        lines.append(f"Task {task.get('id') or '?'}")
    if task.get("title"):
        lines.append(str(task["title"]))
    lines.append("")
    # "Runs" is the total number of executions (agent turns + verification
    # runs); the "Attempts" section below lists the agent turns in detail.
    total_runs = totals.get("attempts")
    if total_runs is not None and (totals.get("verification_attempts") or 0) > 0:
        runs_value = f"{total_runs} ({totals.get('agent_attempts', '?')} agent · {totals['verification_attempts']} verification)"
    else:
        runs_value = total_runs
    for label, value in (
        ("Status", str(receipt.get("state") or "unknown").upper()),
        ("Workspace", task.get("workspace")),
        ("Branch", task.get("branch")),
        ("Duration", _fmt_duration(totals.get("duration_s"))),
        ("Cost", _fmt_cost(totals.get("cost_usd"))),
        ("Runs", runs_value),
    ):
        if value not in (None, ""):
            lines.append(f"{label:<12}{value}")

    attempts = receipt.get("attempts") or []
    if attempts:
        lines.extend(["", "Attempts"])
        for attempt in attempts:
            mark = "✓" if attempt.get("ok") else "✗"
            phase = str(attempt.get("phase") or "IMPLEMENT")
            agent = str(attempt.get("agent") or "?")
            duration = _fmt_duration(attempt.get("duration_s")) or "—"
            cost = _fmt_cost(attempt.get("cost_usd")) or "—"
            lines.append(f"{attempt.get('n', '?')}  {phase:<9} {agent:<14} {duration:<8} {cost:<7} {mark}")
            if not attempt.get("ok") and attempt.get("error"):
                first = str(attempt["error"]).splitlines()[0]
                lines.append(f"        {first}")

    verification = receipt.get("verification") or {}
    lines.extend(["", "Verification"])
    if verification.get("ran"):
        mark = "✓" if verification.get("result") == "PASSED" else "✗"
        command = f" ({verification['command']})" if verification.get("command") else ""
        runs = verification.get("attempts") or 1
        suffix = f", {runs} run{'s' if runs != 1 else ''}" if runs > 1 else ""
        lines.append(f"{mark} {verification.get('result') or '?'}{command}{suffix}")
    else:
        lines.append("skipped (no deterministic verification recorded)")

    gates = receipt.get("gates") or {}
    verdicts = gates.get("verdicts") or {}
    if verdicts or gates.get("bounces") is not None:
        lines.extend(["", "Gates"])
        for role, verdict in verdicts.items():
            ok = "PASS" if verdict.get("ok") else "FAIL"
            agent = f" ({verdict['agent']})" if verdict.get("agent") else ""
            issues = f", {len(verdict.get('issues') or [])} issue(s)" if verdict.get("issues") else ""
            lines.append(f"  {role}: {ok}{agent}{issues}")
        if gates.get("bounces") is not None:
            lines.append(f"  bounces: {gates['bounces']}")

    lines.extend(["", "Final result"])
    final = str(receipt.get("state") or "unknown").upper()
    lines.append(final)
    if receipt.get("error"):
        first = str(receipt["error"]).splitlines()[0]
        lines.append(first)
    return "\n".join(lines)
