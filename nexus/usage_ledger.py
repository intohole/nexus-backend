"""LLM 用量账本单源：逐笔落账 + 失败留痕 + 日/窗口聚合读数。

应用仓从 nexus ask_json_with_usage / chat_with_usage 拿到账单（扁平单笔
或 {bills: [...]} 聚合）后，落账、失败留痕、聚合读数全部走本模块，
业务仓只提供 caller/task_type 与各自的 get_db_session。
写面 fail-open（账本绝不阻断主链），读面 raise（由调用方决策降级）。
表挂 nexus Base：同 Base 应用 create_all 自动建表；独立 Base 应用在
初始化段调 ensure_usage_ledger 补建（r269）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus.database import Base
from nexus.logging import get_logger
from nexus.utils.time import TimeUtils

logger = get_logger("nexus.usage_ledger")

SessionSource = Callable[[], AsyncIterator[AsyncSession]]

_CALLER_MAX = 40
_TASK_MAX = 40
_MODEL_MAX = 80
_EXTRA_MAX = 240


class UsageLedgerRow(Base):
    """逐笔账本行：ref_id 为业务关联键（案件/局/请求），extra 为自由备注。"""

    __tablename__ = "usage_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    caller: Mapped[str] = mapped_column(String(_CALLER_MAX), default="")
    task_type: Mapped[str] = mapped_column(String(_TASK_MAX), default="")
    model: Mapped[str] = mapped_column(String(_MODEL_MAX), default="")
    outcome: Mapped[str] = mapped_column(String(20), default="ok")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ref_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    extra: Mapped[Optional[str]] = mapped_column(String(_EXTRA_MAX), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: TimeUtils.now_utc())

    __table_args__ = (
        Index("ix_usage_ledger_ref_created", "ref_id", "created_at"),
        Index("ix_usage_ledger_created", "created_at"),
    )


def ensure_usage_ledger(conn: object) -> None:
    """独立 Base 应用建表辅助（run_sync 内调用）：幂等 create usage_ledger。"""
    Base.metadata.create_all(bind=conn, tables=[UsageLedgerRow.__table__])


def _clip(value: str | None, limit: int) -> str:
    return str(value or "")[:limit]


def _row(
    caller: str,
    task_type: str,
    model: str,
    outcome: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost_usd: float,
    latency_ms: int,
    ref_id: Optional[int],
    extra: Optional[str],
) -> UsageLedgerRow:
    return UsageLedgerRow(
        caller=_clip(caller, _CALLER_MAX),
        task_type=_clip(task_type, _TASK_MAX),
        model=_clip(model, _MODEL_MAX),
        outcome=_clip(outcome, 20),
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        cached_tokens=int(cached_tokens or 0),
        cost_usd=round(float(cost_usd or 0.0), 6),
        latency_ms=int(latency_ms or 0),
        ref_id=ref_id,
        extra=_clip(extra, _EXTRA_MAX) if extra else None,
    )


def _bill_int(bill: dict, key: str) -> int:
    return int(bill.get(key) or 0)


async def _run(get_session: SessionSource, action: Callable[[AsyncSession], object]) -> object:
    result: object = None
    async with aclosing(get_session()) as sessions:
        async for session in sessions:
            result = await action(session)
            break
    return result


async def record_usage(
    get_session: SessionSource,
    *,
    caller: str,
    task_type: str,
    model: str = "",
    outcome: str = "ok",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    ref_id: Optional[int] = None,
    extra: Optional[str] = None,
) -> bool:
    """单笔落账。fail-open：任何异常 warning 后返回 False，不阻断主链。"""

    async def _write(session: AsyncSession) -> None:
        session.add(_row(caller, task_type, model, outcome, input_tokens,
                         output_tokens, cached_tokens, cost_usd, latency_ms,
                         ref_id, extra))
        await session.commit()

    try:
        await _run(get_session, _write)
        return True
    except Exception:
        logger.warning("usage record failed caller=%s task_type=%s", caller, task_type, exc_info=True)
        return False


async def record_usage_bills(
    get_session: SessionSource,
    caller: str,
    task_type: str,
    usage: dict,
    *,
    outcome: str = "ok",
    ref_id: Optional[int] = None,
    extra: Optional[str] = None,
) -> int:
    """nexus 账单入账：{bills: [...]} 聚合逐笔展开，扁平单笔直接落账；
    空/异常 usage 落一笔零账留痕（outcome 透传，失败路径不静默归零）。
    返回落账笔数，异常 fail-open 返回 0。"""

    async def _write_all(session: AsyncSession) -> int:
        rows: list[UsageLedgerRow] = []
        bills = usage.get("bills") if isinstance(usage, dict) else None
        if isinstance(bills, list) and bills:
            for bill in bills:
                if not isinstance(bill, dict):
                    continue
                rows.append(_row(caller, task_type, str(bill.get("model") or ""),
                                 outcome, _bill_int(bill, "input_tokens"),
                                 _bill_int(bill, "output_tokens"),
                                 _bill_int(bill, "cached_tokens"),
                                 float(bill.get("cost_usd") or 0.0),
                                 _bill_int(bill, "latency_ms"), ref_id, extra))
        elif isinstance(usage, dict) and "input_tokens" in usage:
            rows.append(_row(caller, task_type, str(usage.get("model") or ""),
                             outcome, _bill_int(usage, "input_tokens"),
                             _bill_int(usage, "output_tokens"),
                             _bill_int(usage, "cached_tokens"),
                             float(usage.get("cost_usd") or 0.0),
                             _bill_int(usage, "latency_ms"), ref_id, extra))
        else:
            rows.append(_row(caller, task_type, "", outcome, 0, 0, 0, 0.0, 0,
                             ref_id, extra))
        if not rows:
            return 0
        session.add_all(rows)
        await session.commit()
        return len(rows)

    try:
        return await _run(get_session, _write_all) or 0
    except Exception:
        logger.warning("usage bills record failed caller=%s task_type=%s", caller, task_type, exc_info=True)
        return 0


async def record_usage_error(
    get_session: SessionSource,
    caller: str,
    task_type: str,
    *,
    ref_id: Optional[int] = None,
    extra: Optional[str] = None,
) -> bool:
    """失败笔留痕：调用已发生但未拿到 usage，至少记一次失败调用。"""
    return await record_usage(get_session, caller=caller, task_type=task_type,
                              outcome="error", ref_id=ref_id, extra=extra)


def _day_bounds(day: Optional[str]) -> tuple[datetime, datetime]:
    if day:
        base = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    else:
        base = TimeUtils.now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    return base, base + timedelta(days=1)


async def usage_day(
    get_session: SessionSource,
    *,
    day: Optional[str] = None,
    ref_id: Optional[int] = None,
) -> dict:
    """UTC 日聚合读数（读面 raise，调用方决策降级）。day 缺省=今日（UTC）。"""
    start, end = _day_bounds(day)

    async def _read(session: AsyncSession) -> dict:
        conds = [UsageLedgerRow.created_at >= start, UsageLedgerRow.created_at < end]
        if ref_id is not None:
            conds.append(UsageLedgerRow.ref_id == ref_id)
        row = (await session.execute(
            select(func.count(UsageLedgerRow.id),
                   func.coalesce(func.sum(UsageLedgerRow.cost_usd), 0.0),
                   func.coalesce(func.sum(UsageLedgerRow.input_tokens), 0),
                   func.coalesce(func.sum(UsageLedgerRow.output_tokens), 0),
                   func.coalesce(func.sum(UsageLedgerRow.cached_tokens), 0),
                   func.sum(UsageLedgerRow.outcome != "ok"),
                   func.coalesce(func.avg(UsageLedgerRow.latency_ms), 0.0),
                   ).where(*conds)
        )).one()
        calls = int(row[0])
        return {
            "date": start.date().isoformat(),
            "calls": calls,
            "failed_calls": int(row[5] or 0),
            "cost_usd": round(float(row[1]), 4),
            "input_tokens": int(row[2]),
            "output_tokens": int(row[3]),
            "cached_tokens": int(row[4]),
            "avg_latency_ms": round(float(row[6])) if calls else None,
        }

    return await _run(get_session, _read) or {}


async def usage_summary(
    get_session: SessionSource,
    *,
    since_utc: datetime,
    until_utc: Optional[datetime] = None,
    group_by: str = "task_type",
    ref_id: Optional[int] = None,
) -> list[dict]:
    """窗口分组聚合读数（group_by: task_type|caller|model|outcome|date）。"""
    if group_by not in ("task_type", "caller", "model", "outcome", "date"):
        raise ValueError(f"unsupported group_by: {group_by}")
    col = {
        "task_type": UsageLedgerRow.task_type,
        "caller": UsageLedgerRow.caller,
        "model": UsageLedgerRow.model,
        "outcome": UsageLedgerRow.outcome,
        "date": func.date(UsageLedgerRow.created_at),
    }[group_by]

    async def _read(session: AsyncSession) -> list[dict]:
        conds = [UsageLedgerRow.created_at >= since_utc]
        if until_utc is not None:
            conds.append(UsageLedgerRow.created_at < until_utc)
        if ref_id is not None:
            conds.append(UsageLedgerRow.ref_id == ref_id)
        rows = (await session.execute(
            select(col,
                   func.count(UsageLedgerRow.id),
                   func.coalesce(func.sum(UsageLedgerRow.cost_usd), 0.0),
                   func.coalesce(func.sum(UsageLedgerRow.input_tokens), 0),
                   func.coalesce(func.sum(UsageLedgerRow.output_tokens), 0),
                   func.coalesce(func.sum(UsageLedgerRow.cached_tokens), 0),
                   func.sum(UsageLedgerRow.outcome != "ok"),
                   ).where(*conds).group_by(col).order_by(func.count(UsageLedgerRow.id).desc())
        )).all()
        return [
            {
                "group": str(r[0]),
                "calls": int(r[1]),
                "failed_calls": int(r[6] or 0),
                "cost_usd": round(float(r[2]), 4),
                "input_tokens": int(r[3]),
                "output_tokens": int(r[4]),
                "cached_tokens": int(r[5]),
            }
            for r in rows
        ]

    return await _run(get_session, _read) or []
