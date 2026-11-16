import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nexus.cost_guard import (
    BudgetPeriod,
    CostBudget,
    CostBudgetExceededError,
    CostGuard,
    _calendar_period_start,
    get_unknown_pricing_events,
)


def _test_budget(**kw) -> CostBudget:
    return CostBudget(max_tokens=100000, max_cost_usd=10.0, **kw)


@pytest.mark.asyncio
async def test_memory_mode_contract_preserved():
    cg = CostGuard(budget=_test_budget())
    await cg.record_usage(prompt_tokens=100, completion_tokens=50, model="deepseek-flash", operation="t")
    summary = cg.get_summary()
    assert summary["current_usage"]["total_tokens"] == 150
    assert summary["by_operation"]["t"]["count"] == 1
    assert summary["window_start"] > 0
    check = await cg.check_budget(estimated_tokens=10)
    assert check["allowed"] is True


@pytest.mark.asyncio
async def test_calendar_daily_window_drops_yesterday(tmp_path: Path):
    cg = CostGuard(budget=CostBudget(max_tokens=1000, max_cost_usd=100.0, period=BudgetPeriod.DAILY))
    yesterday = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0) - timedelta(seconds=1)
    cg._usages.append(type(cg._usages[0] if cg._usages else None) if False else
                      __import__("nexus.cost_guard", fromlist=["TokenUsage"]).TokenUsage(
                          prompt_tokens=900, completion_tokens=0, total_tokens=900,
                          estimated_cost_usd=0.5, timestamp=yesterday.timestamp()))
    check = await cg.check_budget()
    assert check["allowed"] is True, "yesterday usage must not count into UTC calendar day"
    assert check["total_tokens"] == 0


@pytest.mark.asyncio
async def test_calendar_month_window_boundary():
    now = datetime(2026, 11, 15, 10, 30, tzinfo=timezone.utc).timestamp()
    assert _calendar_period_start(BudgetPeriod.MONTHLY, now) == datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
    assert _calendar_period_start(BudgetPeriod.DAILY, now) == datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp()
    monday = datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp()
    assert _calendar_period_start(BudgetPeriod.WEEKLY, now) == monday


@pytest.mark.asyncio
async def test_persistence_survives_recreate(tmp_path: Path):
    db = tmp_path / "cost.db"
    cg1 = CostGuard(budget=_test_budget(), data_path=db)
    await cg1.record_usage(prompt_tokens=200, completion_tokens=100, model="deepseek-flash", operation="gen")
    summary1 = cg1.get_summary()
    assert summary1["current_usage"]["total_tokens"] == 300

    cg2 = CostGuard(budget=_test_budget(), data_path=db)
    summary2 = cg2.get_summary()
    assert summary2["current_usage"]["total_tokens"] == 300, "restart must not resurrect budget"
    assert summary2["by_operation"]["gen"]["count"] == 1


@pytest.mark.asyncio
async def test_persistence_blocks_across_instances(tmp_path: Path):
    db = tmp_path / "cost.db"
    tiny = CostBudget(max_tokens=100, max_cost_usd=1000.0)
    cg1 = CostGuard(budget=tiny, data_path=db)
    await cg1.record_usage(prompt_tokens=80, completion_tokens=30, model="deepseek-flash", operation="t")
    assert cg1.is_blocked is True

    cg2 = CostGuard(budget=tiny, data_path=db)
    check = await cg2.check_budget(estimated_tokens=50)
    assert check["allowed"] is False


@pytest.mark.asyncio
async def test_unknown_model_warns_and_counts():
    before = get_unknown_pricing_events()
    cost = CostGuard.estimate_cost("never-registered-model", 1000, 1000)
    assert cost > 0
    assert get_unknown_pricing_events() == before + 1
    known = CostGuard.estimate_cost("deepseek-flash", 1000, 1000)
    assert known < cost * 100


@pytest.mark.asyncio
async def test_call_with_guard_real_usage_split():
    cg = CostGuard(budget=_test_budget())

    class Resp:
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}

    async def dummy():
        return Resp()

    await cg.call_with_guard(dummy, estimated_tokens=500, model="deepseek-flash", operation="t")
    summary = cg.get_summary()
    assert summary["current_usage"]["total_tokens"] == 120
    db = getattr(cg, "_usages", [])
    assert db and db[-1].prompt_tokens == 100 and db[-1].completion_tokens == 20


@pytest.mark.asyncio
async def test_call_with_guard_conservative_without_usage():
    cg = CostGuard(budget=_test_budget())

    async def dummy():
        return {"raw": True}

    await cg.call_with_guard(dummy, estimated_tokens=77, model="deepseek-flash", operation="t")
    row = cg._usages[-1]
    assert row.prompt_tokens == 0 and row.completion_tokens == 77


@pytest.mark.asyncio
async def test_call_with_guard_blocks_before_call():
    cg = CostGuard(budget=CostBudget(max_tokens=50, max_cost_usd=10.0))
    await cg.record_usage(prompt_tokens=60, completion_tokens=0, model="deepseek-flash", operation="t")

    async def dummy():
        raise AssertionError("must not be called")

    with pytest.raises(CostBudgetExceededError):
        await cg.call_with_guard(dummy, estimated_tokens=100)


@pytest.mark.asyncio
async def test_reset_clears_persistence(tmp_path: Path):
    db = tmp_path / "cost.db"
    cg = CostGuard(budget=_test_budget(), data_path=db)
    await cg.record_usage(prompt_tokens=500, completion_tokens=0, model="deepseek-flash", operation="t")
    cg.reset_budget()
    assert cg.get_summary()["current_usage"]["total_tokens"] == 0
    assert cg.is_blocked is False
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM cost_usage").fetchone()[0] == 0
    con.close()


@pytest.mark.asyncio
async def test_default_fallback_is_fleet_model():
    from nexus.cost_guard import _FALLBACK_MODEL
    assert _FALLBACK_MODEL == "deepseek-flash"
