"""usage_ledger 原语守底：落账三件/聚合读数/fail-open/建表幂等（r269）。"""
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from nexus.usage_ledger import (
    SessionSource,
    UsageLedgerRow,
    ensure_usage_ledger,
    record_usage,
    record_usage_bills,
    record_usage_error,
    usage_day,
    usage_summary,
)


@pytest_asyncio.fixture()
async def session_source() -> AsyncIterator[tuple[async_sessionmaker, SessionSource]]:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(ensure_usage_ledger)

    def _source() -> AsyncIterator:
        async def _gen() -> AsyncIterator:
            async with maker() as session:
                yield session
        return _gen()

    yield maker, _source
    await engine.dispose()


@pytest.fixture()
def maker(session_source: tuple[async_sessionmaker, SessionSource]) -> async_sessionmaker:
    return session_source[0]


@pytest.fixture()
def source(session_source: tuple[async_sessionmaker, SessionSource]) -> SessionSource:
    return session_source[1]


@pytest.mark.asyncio()
async def test_record_usage_roundtrip(source: SessionSource, maker: async_sessionmaker) -> None:
    ok = await record_usage(source, caller="xunji.case", task_type="extract",
                            model="deepseek-flash", outcome="ok", input_tokens=100,
                            output_tokens=20, cached_tokens=64, cost_usd=0.0012,
                            latency_ms=800, ref_id=7)
    assert ok is True
    async with maker() as session:
        row = (await session.execute(select(UsageLedgerRow))).scalars().one()
    assert row.caller == "xunji.case" and row.task_type == "extract"
    assert row.input_tokens == 100 and row.output_tokens == 20
    assert row.cached_tokens == 64 and row.cost_usd == pytest.approx(0.0012)
    assert row.latency_ms == 800 and row.ref_id == 7 and row.outcome == "ok"
    assert row.model == "deepseek-flash" and row.created_at is not None


@pytest.mark.asyncio()
async def test_record_usage_clips_long_fields(source: SessionSource, maker: async_sessionmaker) -> None:
    await record_usage(source, caller="c" * 99, task_type="t" * 99, model="m" * 99)
    async with maker() as session:
        row = (await session.execute(select(UsageLedgerRow))).scalars().one()
    assert len(row.caller) == 40 and len(row.task_type) == 40 and len(row.model) == 80


@pytest.mark.asyncio()
async def test_record_usage_bills_expands(source: SessionSource, maker: async_sessionmaker) -> None:
    usage = {"bills": [
        {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 0, "cost_usd": 0.0001,
         "latency_ms": 100, "model": "glm-4-flash"},
        {"input_tokens": 20, "output_tokens": 8, "cached_tokens": 4, "cost_usd": 0.0002,
         "latency_ms": 200, "model": "deepseek-flash"},
    ]}
    n = await record_usage_bills(source, "golden", "analyze", usage, ref_id=3)
    assert n == 2
    async with maker() as session:
        rows = (await session.execute(select(UsageLedgerRow).order_by(UsageLedgerRow.id))).scalars().all()
    assert [r.model for r in rows] == ["glm-4-flash", "deepseek-flash"]
    assert all(r.ref_id == 3 and r.task_type == "analyze" for r in rows)
    assert rows[1].cached_tokens == 4


@pytest.mark.asyncio()
async def test_record_usage_bills_flat_and_empty(source: SessionSource, maker: async_sessionmaker) -> None:
    flat = {"input_tokens": 5, "output_tokens": 2, "cost_usd": 0.0, "latency_ms": 50, "model": "k"}
    assert await record_usage_bills(source, "a", "t", flat) == 1
    assert await record_usage_bills(source, "a", "t", {}, outcome="error") == 1
    async with maker() as session:
        rows = (await session.execute(select(UsageLedgerRow).order_by(UsageLedgerRow.id))).scalars().all()
    assert rows[0].model == "k" and rows[0].input_tokens == 5
    assert rows[1].outcome == "error" and rows[1].input_tokens == 0 and rows[1].model == ""


@pytest.mark.asyncio()
async def test_record_usage_error(source: SessionSource, maker: async_sessionmaker) -> None:
    assert await record_usage_error(source, "a", "t", ref_id=9, extra="boom" * 100) is True
    async with maker() as session:
        row = (await session.execute(select(UsageLedgerRow))).scalars().one()
    assert row.outcome == "error" and row.ref_id == 9
    assert row.extra is not None and len(row.extra) <= 240


@pytest.mark.asyncio()
async def test_fail_open_on_bad_session() -> None:
    async def _broken() -> AsyncIterator:
        raise RuntimeError("db down")
        yield None  # pragma: no cover

    assert await record_usage(_broken, caller="a", task_type="t") is False
    assert await record_usage_bills(_broken, "a", "t", {"input_tokens": 1}) == 0


@pytest.mark.asyncio()
async def test_usage_day_aggregate(source: SessionSource) -> None:
    await record_usage(source, caller="a", task_type="extract", input_tokens=10, output_tokens=5,
                       cost_usd=0.001, latency_ms=100)
    await record_usage(source, caller="a", task_type="extract", input_tokens=20, output_tokens=10,
                       cached_tokens=6, cost_usd=0.002, latency_ms=300)
    await record_usage_error(source, "a", "extract")
    summary = await usage_day(source)
    assert summary["calls"] == 3 and summary["failed_calls"] == 1
    assert summary["input_tokens"] == 30 and summary["output_tokens"] == 15
    assert summary["cached_tokens"] == 6 and summary["cost_usd"] == pytest.approx(0.003)
    assert summary["avg_latency_ms"] == 133 and summary["date"] is not None


@pytest.mark.asyncio()
async def test_usage_summary_grouping_and_ref_filter(source: SessionSource) -> None:
    from datetime import datetime, timezone

    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await record_usage(source, caller="x", task_type="a", model="m1", input_tokens=1, ref_id=1)
    await record_usage(source, caller="y", task_type="b", model="m2", input_tokens=2, ref_id=2)
    by_task = await usage_summary(source, since_utc=since, group_by="task_type")
    assert {r["group"] for r in by_task} == {"a", "b"}
    by_ref = await usage_summary(source, since_utc=since, group_by="task_type", ref_id=2)
    assert [r["group"] for r in by_ref] == ["b"] and by_ref[0]["input_tokens"] == 2
    by_date = await usage_summary(source, since_utc=since, group_by="date")
    assert len(by_date) == 1 and by_date[0]["calls"] == 2
    with pytest.raises(ValueError):
        await usage_summary(source, since_utc=since, group_by="nonsense")


@pytest.mark.asyncio()
async def test_ensure_usage_ledger_idempotent() -> None:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(ensure_usage_ledger)
        async with engine.begin() as conn:
            await conn.run_sync(ensure_usage_ledger)
    finally:
        await engine.dispose()
