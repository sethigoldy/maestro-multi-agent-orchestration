"""Budget caps for Maestro delegation (launch-time enforcement).

Caps are configured per daemon via environment:

- ``MAESTRO_BUDGET_PER_AGENT_USD`` — cumulative USD cap per agent name.
- ``MAESTRO_BUDGET_DAILY_USD`` — cumulative USD cap across all agents, reset
  at UTC midnight.

Spend is computed from task usage claims: each attempt records the usage its
agent produced (``total_cost_usd``), so per-agent attribution is exact and a
task's cost survives failed attempts (failed work still costs money).
Enforcement happens only in ``delegate()`` — running tasks always finish; a
blocked launch raises ``ValueError`` like any other precondition failure.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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
    """Extract a USD cost from a usage dict (0.0 when absent/non-numeric)."""
    if not isinstance(usage, dict):
        return 0.0
    value = usage.get("total_cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


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


def daily_spend(records: list[dict[str, Any]], now: datetime | None = None) -> float:
    """Total cost of attempts whose task started today (UTC)."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    total = 0.0
    for record in records:
        started_at = str(record.get("started_at") or "")
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if started.astimezone(timezone.utc).date() != today:
            continue
        total += sum(cost for _, cost in attempt_costs(record))
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
