"""v2-M6: budget caps — parsing, spend accounting, and launch-time enforcement."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maestro.budgets import BudgetCaps, check, cost_of, daily_spend, spent_by_agent


def _record(task_id="t1", started_at=None, attempts=None, usage=None):
    return {
        "task_id": task_id,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "attempts": attempts or [],
        "usage": usage,
    }


# ------------------------------------------------------------------- parsing

def test_caps_from_env(monkeypatch):
    monkeypatch.delenv("MAESTRO_BUDGET_PER_AGENT_USD", raising=False)
    monkeypatch.delenv("MAESTRO_BUDGET_DAILY_USD", raising=False)
    caps = BudgetCaps.from_env()
    assert caps.per_agent_usd is None and caps.daily_usd is None and caps.any() is False

    monkeypatch.setenv("MAESTRO_BUDGET_PER_AGENT_USD", "12.5")
    monkeypatch.setenv("MAESTRO_BUDGET_DAILY_USD", "0")
    caps = BudgetCaps.from_env()
    assert caps.per_agent_usd == 12.5 and caps.daily_usd == 0.0 and caps.any() is True

    # garbage or negative values are ignored (misconfig must not block launches)
    monkeypatch.setenv("MAESTRO_BUDGET_PER_AGENT_USD", "lots")
    monkeypatch.setenv("MAESTRO_BUDGET_DAILY_USD", "-3")
    caps = BudgetCaps.from_env()
    assert caps.per_agent_usd is None and caps.daily_usd is None


# ---------------------------------------------------------------- cost_of

def _claim(subject, predicate, obj):
    class C:
        pass

    c = C()
    c.subject = subject
    c.predicate = predicate
    c.object = obj
    return c


def test_records_from_claims_filters_and_parses():
    from maestro.budgets import _records_from_claims

    claims = [
        _claim("maestro:episode:1", "task_runtime", json.dumps({"attempts": []})),  # wrong subject prefix
        _claim("maestro:task:t1", "task_status", "working"),  # wrong predicate
        _claim("maestro:task:t2", "task_runtime", "{not json"),  # malformed object
        _claim("maestro:task:t3", "task_runtime", json.dumps([1, 2])),  # non-dict payload
        _claim("maestro:task:t4", "task_runtime", json.dumps({"attempts": []})),  # good
    ]
    out = _records_from_claims(claims)
    assert set(out) == {"t4"}


def test_attempt_costs_skips_non_dict_entries():
    from maestro.budgets import attempt_costs

    record = {"attempts": ["junk", None, {"agent": "codex", "usage": {"total_cost_usd": 1.0}}]}
    assert attempt_costs(record) == [("codex", 1.0)]


def test_daily_spend_naive_timestamps_assumed_utc():
    from maestro.budgets import daily_spend

    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    records = [
        {"task_id": "t1", "started_at": "2026-09-17T01:00:00", "attempts": [{"agent": "codex", "usage": {"total_cost_usd": 3.0}}]},
    ]
    assert daily_spend(records, now=now) == pytest.approx(3.0)


def test_cost_of_shapes():
    assert cost_of(None) == 0.0
    assert cost_of("nope") == 0.0
    assert cost_of({"total_cost_usd": True}) == 0.0  # bool is not a number here
    assert cost_of({"total_cost_usd": "1.5"}) == 0.0
    assert cost_of({"total_cost_usd": 0.25}) == 0.25
    assert cost_of({"other": 1}) == 0.0


# ------------------------------------------------------------ spend helpers

def test_spent_by_agent_and_daily():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    yesterday = datetime(2026, 9, 16, 23, 59, tzinfo=timezone.utc).isoformat()
    today = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc).isoformat()

    records = [
        _record("t1", started_at=today, attempts=[{"agent": "codex", "usage": {"total_cost_usd": 0.30}}, {"agent": "codex", "usage": {"total_cost_usd": 0.2}}]),
        _record("t2", started_at=yesterday, attempts=[{"agent": "copilot", "usage": {"total_cost_usd": 9.9}}]),
        _record("t3", started_at="not a date", attempts=[{"agent": "codex", "usage": {"total_cost_usd": 1.0}}]),
        _record("t4", started_at=today, attempts=[{"agent": "claude_code"}, {"bogus": 1}]),
    ]
    per_agent = spent_by_agent(records)
    assert per_agent == {"codex": pytest.approx(1.5), "copilot": pytest.approx(9.9)}
    # t3 has an unparseable started_at -> excluded from the daily total
    assert daily_spend(records, now=now) == pytest.approx(0.5)


def test_daily_spend_defaults_to_now():
    records = [_record("t1", attempts=[{"agent": "codex", "usage": {"total_cost_usd": 2.0}}])]
    assert daily_spend(records) == pytest.approx(2.0)


# ------------------------------------------------------------------ check()

def test_check_enforcement_matrix():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    today = now.isoformat()
    records = [
        _record("t1", started_at=today, attempts=[{"agent": "codex", "usage": {"total_cost_usd": 5.0}}]),
        _record("t2", started_at=today, attempts=[{"agent": "copilot", "usage": {"total_cost_usd": 4.0}}]),
    ]

    assert check(BudgetCaps(), "codex", records, now=now) is None  # no caps -> never blocks
    assert check(BudgetCaps(per_agent_usd=10), "codex", records, now=now) is None  # under cap
    err = check(BudgetCaps(per_agent_usd=5), "codex", records, now=now)
    assert err and "per-agent budget cap for 'codex'" in err and "$5.0000" in err
    assert check(BudgetCaps(per_agent_usd=5), "copilot", records, now=now) is None  # other agent unaffected
    assert check(BudgetCaps(daily_usd=10), "anything", records, now=now) is None  # 9.0 < 10
    err = check(BudgetCaps(daily_usd=8), "anything", records, now=now)
    assert err and "daily budget cap exhausted" in err


# -------------------------------------------------------- daemon integration

def _daemon_with_costs(tmp_path, monkeypatch):
    from maestro.daemon import MaestroDaemon

    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    return d


def test_delegate_blocks_when_per_agent_cap_exhausted(tmp_path, monkeypatch):
    from maestro.handoff import HandoffDoc

    d = _daemon_with_costs(tmp_path, monkeypatch)
    try:
        # seed durable spend via a live record (as after a completed task)
        d._tasks["t-seed"] = {
            "task_id": "t-seed",
            "state": "completed",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "attempts": [{"agent": "codex", "ok": True, "usage": {"total_cost_usd": 3.0}}],
            "usage": {"total_cost_usd": 3.0},
        }
        d._persist("t-seed")
        monkeypatch.setenv("MAESTRO_BUDGET_PER_AGENT_USD", "2")

        doc = HandoffDoc(title="t", request="r", target_agent="codex", commit_policy="no-commit")
        with pytest.raises(ValueError, match="per-agent budget cap"):
            d.delegate(doc, tmp_path)

        # a different agent is unaffected by codex's spend
        monkeypatch.setenv("MAESTRO_BUDGET_PER_AGENT_USD", "2")
        doc2 = HandoffDoc(title="t", request="r", target_agent="copilot", commit_policy="no-commit")
        started = d.delegate(doc2, tmp_path)
        assert started["task_id"]  # allowed
    finally:
        d.stop()


def test_delegate_blocks_when_daily_cap_exhausted(tmp_path, monkeypatch):
    from maestro.handoff import HandoffDoc

    d = _daemon_with_costs(tmp_path, monkeypatch)
    try:
        d._tasks["t-seed"] = {
            "task_id": "t-seed",
            "state": "completed",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "attempts": [{"agent": "codex", "ok": True, "usage": {"total_cost_usd": 7.0}}],
            "usage": {"total_cost_usd": 7.0},
        }
        d._persist("t-seed")
        monkeypatch.setenv("MAESTRO_BUDGET_DAILY_USD", "5")

        doc = HandoffDoc(title="t", request="r", target_agent="codex", commit_policy="no-commit")
        with pytest.raises(ValueError, match="daily budget cap"):
            d.delegate(doc, tmp_path)
    finally:
        d.stop()


def test_delegate_unaffected_without_caps(tmp_path, monkeypatch):
    from maestro.handoff import HandoffDoc

    d = _daemon_with_costs(tmp_path, monkeypatch)
    try:
        monkeypatch.delenv("MAESTRO_BUDGET_PER_AGENT_USD", raising=False)
        monkeypatch.delenv("MAESTRO_BUDGET_DAILY_USD", raising=False)
        doc = HandoffDoc(title="t", request="r", target_agent="codex", commit_policy="no-commit")
        started = d.delegate(doc, tmp_path)  # no caps -> launches (task then fails on preflight; fine)
        assert started["task_id"]
    finally:
        d.stop()


def test_budget_records_merge_claims_and_live(tmp_path, monkeypatch):
    from maestro.daemon import MaestroDaemon

    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    d = MaestroDaemon(state_dir=home, start_http=False, max_retries=0, backoff_s=0)
    try:
        # durable record from a "previous run" (claims only, not in _tasks)
        snapshot = {
            "task_id": "t-old",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "attempts": [{"agent": "codex", "ok": True, "usage": {"total_cost_usd": 1.5}}],
        }
        d.maestro._write_claim("t-old", "task_runtime", json.dumps(snapshot))
        # live record overrides the same id with fresher spend
        d._tasks["t-live"] = {
            "task_id": "t-live",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "attempts": [{"agent": "copilot", "ok": True, "usage": {"total_cost_usd": 2.0}}],
        }
        records = d._budget_records()
        per_agent = spent_by_agent(records)
        assert per_agent == {"codex": pytest.approx(1.5), "copilot": pytest.approx(2.0)}

        # live wins when both exist for the same id
        d.maestro._write_claim("t-live", "task_runtime", json.dumps({"attempts": [{"agent": "stale", "usage": {"total_cost_usd": 99}}]}))
        records = d._budget_records()
        assert spent_by_agent(records) == {"codex": pytest.approx(1.5), "copilot": pytest.approx(2.0)}
    finally:
        d.stop()


# ----------------------------------------------------------------------- CLI

def test_budgets_cli(tmp_path, monkeypatch):
    from maestro import cli as clic
    from maestro.core import Maestro

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    # seed one durable task with cost
    m = Maestro(home)
    m._write_claim(
        "t-seed",
        "task_runtime",
        json.dumps({"started_at": datetime.now(timezone.utc).isoformat(), "attempts": [{"agent": "codex", "usage": {"total_cost_usd": 1.25}}]}),
    )
    m.close()

    import contextlib
    import io

    # no caps configured
    monkeypatch.delenv("MAESTRO_BUDGET_PER_AGENT_USD", raising=False)
    monkeypatch.delenv("MAESTRO_BUDGET_DAILY_USD", raising=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert clic.main(["budgets"]) == 0
    assert "no budget caps configured" in out.getvalue()

    # per-agent cap only: the daily section is omitted entirely
    monkeypatch.setenv("MAESTRO_BUDGET_PER_AGENT_USD", "5")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert clic.main(["budgets"]) == 0
    text = out.getvalue()
    assert "per-agent cap: $5.0000" in text and "daily cap" not in text

    # caps set: spend lines appear
    monkeypatch.setenv("MAESTRO_BUDGET_PER_AGENT_USD", "5")
    monkeypatch.setenv("MAESTRO_BUDGET_DAILY_USD", "1")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert clic.main(["budgets"]) == 0
    text = out.getvalue()
    assert "per-agent cap: $5.0000" in text
    assert "codex: $1.2500" in text and "(EXHAUSTED)" not in text.split("daily")[0]
    assert "daily cap: $1.0000" in text and "(EXHAUSTED)" in text  # 1.25 >= 1.0

    # daily cap only: the per-agent section is omitted entirely
    monkeypatch.delenv("MAESTRO_BUDGET_PER_AGENT_USD", raising=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert clic.main(["budgets"]) == 0
    text = out.getvalue()
    assert "daily cap: $1.0000" in text and "per-agent cap" not in text
