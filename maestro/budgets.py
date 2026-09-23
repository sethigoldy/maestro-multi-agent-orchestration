"""Budget caps for Maestro delegation (launch-time enforcement).

Caps are configured per daemon via environment:

- ``MAESTRO_BUDGET_PER_AGENT_USD`` — cumulative USD cap per agent name.
- ``MAESTRO_BUDGET_DAILY_USD`` — cumulative USD cap across all agents, reset
  at UTC midnight.

Spend is computed from task usage claims: each attempt records the usage its
agent produced (``cost_usd`` / ``total_cost_usd``), so per-agent attribution is
exact and a task's cost survives failed attempts (failed work still costs money).
Enforcement happens only in ``delegate()`` — running tasks always finish; a
blocked launch raises ``ValueError`` like any other precondition failure.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None  # misconfiguration must not block delegation
    return value if value >= 0 else None


class BudgetCaps:
    """Parsed budget configuration (either cap may be unset)."""

    def __init__(self, per_agent_usd: float | None = None, daily_usd: float | None = None) -> None:
        self.per_agent_usd = per_agent_usd
        self.daily_usd = daily_usd

    @classmethod
    def from_env(cls) -> "BudgetCaps":
        return cls(_env_float("MAESTRO_BUDGET_PER_AGENT_USD"), _env_float("MAESTRO_BUDGET_DAILY_USD"))

    def any(self) -> bool:
        return self.per_agent_usd is not None or self.daily_usd is not None


def cost_of(usage: Any) -> float:
    """Extract a USD cost from a usage dict (0.0 when absent/non-numeric).

    Adapters report the attempt cost under ``cost_usd``; ``total_cost_usd`` is
    accepted as well so both spellings attribute spend identically.
    """
    if not isinstance(usage, dict):
        return 0.0
    for key in ("total_cost_usd", "cost_usd"):
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    return 0.0


def _records_from_claims(claims: list[Any]) -> dict[str, dict[str, Any]]:
    """Latest task_runtime snapshot per task from the claim journal.

    Claim subjects are ``maestro:task:<task_id>``; the returned mapping is
    keyed by the bare task id so live records can override it.
    """
    out: dict[str, dict[str, Any]] = {}
    prefix = "maestro:task:"
    for claim in claims:
        if getattr(claim, "predicate", None) != "task_runtime":
            continue
        subject = str(getattr(claim, "subject", ""))
        if not subject.startswith(prefix):
            continue
        try:
            parsed = json.loads(claim.object)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            out[subject[len(prefix):]] = parsed  # journal is append-only: last wins
    return out


def attempt_costs(record: dict[str, Any]) -> list[tuple[str, float]]:
    """(agent, cost) pairs for one task record's attempts."""
    pairs: list[tuple[str, float]] = []
    for attempt in record.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        agent = str(attempt.get("agent") or "unknown")
        cost = cost_of(attempt.get("usage"))
        if cost > 0:
            pairs.append((agent, cost))
    return pairs


def spent_by_agent(records: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for record in records:
        for agent, cost in attempt_costs(record):
            totals[agent] = totals.get(agent, 0.0) + cost
    return totals


def _utc_date(raw: Any) -> date | None:
    """UTC calendar date of an ISO timestamp, or None when it cannot be parsed.

    A timestamp without a zone is taken to be UTC.
    """
    try:
        moment = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date()


def daily_spend(records: list[dict[str, Any]], now: datetime | None = None) -> float:
    """Total cost of the attempts that finished today (UTC).

    Each attempt is dated by its own ``finished_at`` time, so money spent today
    by a follow-up on an older task counts toward today's cap. An attempt with
    no usable finish time falls back to the task's ``started_at`` time, which is
    how records written before attempts carried a timestamp are dated.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    total = 0.0
    for record in records:
        task_date = _utc_date(record.get("started_at"))
        for attempt in record.get("attempts") or []:
            if not isinstance(attempt, dict):
                continue
            attempt_date = _utc_date(attempt.get("finished_at")) or task_date
            cost = cost_of(attempt.get("usage"))
            if attempt_date == today and cost > 0:
                total += cost
    return total


def check(caps: BudgetCaps, agent_name: str, records: list[dict[str, Any]], now: datetime | None = None) -> str | None:
    """Return a human-readable violation message, or None when the launch is allowed."""
    if not caps.any():
        return None
    per_agent = spent_by_agent(records)
    daily = daily_spend(records, now=now)
    if caps.per_agent_usd is not None:
        spent = per_agent.get(agent_name, 0.0)
        if spent >= caps.per_agent_usd:
            return f"per-agent budget cap for {agent_name!r} exhausted: spent ${spent:.4f} >= cap ${caps.per_agent_usd:.4f}"
    if caps.daily_usd is not None and daily >= caps.daily_usd:
        return f"daily budget cap exhausted: spent ${daily:.4f} today (UTC) >= cap ${caps.daily_usd:.4f}"
    return None
