"""评审/合议编排核（S204 收编）：多席并行 + 席级失败捕获 + 失败三态显影。

收编边界（judge.py 同款纪律）：只收两仓逐字同构的编排骨架——arcade
infra/jury.py 的 gather 多席+任一席失败短路（jury.py:136-147/:176-181）与
travelMate plan_council_service.py 的降级三件套确定性判据
（plan_council_service.py:127-200，verseCraft r23「判定必须用确定性原料」）。
席 persona、输出 schema、檐/超时/配额、留痕对象形状、进度广播是业务域资产，
经参数注入不进共享仓。

失败语义单源（DSH「结构化失败语义」编排面收口）：error_kind ∈ timeout/exception
二态 + degrade_reason 四件套常量（seat_failed/empty_invalid_json/timeout_cap/
ask_failed）——消费方按业务留痕对象映射显影字段，不再各造字符串。

射程外（诚实边界）：席间通信、多轮互辩、裁决清洗、schema 修复环——评审增强
策略归业务仓；fail-open 纪律（评审失败绝不阻断主流程）由调用方保持。
供给形态档位=强契约工具档（显式 import 显式调用）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

DEGRADE_SEAT_FAILED = "seat_failed"
DEGRADE_EMPTY_INVALID_JSON = "empty_invalid_json"
DEGRADE_TIMEOUT_CAP = "timeout_cap"
DEGRADE_ASK_FAILED = "ask_failed"
DEGRADE_ALL_EMPTY = "all_empty"

ERROR_KIND_TIMEOUT = "timeout"
ERROR_KIND_EXCEPTION = "exception"

_ERROR_CAP = 200


@dataclass
class SeatResult:
    name: str
    ok: bool
    value: Any = None
    error_kind: str | None = None
    error: str | None = None
    elapsed: float = 0.0


def _summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:_ERROR_CAP - len(type(exc).__name__) - 2]}"


async def run_seat(
    name: str,
    call: Callable[[], Awaitable[Any]],
    *,
    timeout_s: float | None = None,
) -> SeatResult:
    """单席执行：异常捕获为结构化失败面（fail-open 骨架），timeout_s 有值时 wait_for。"""
    import time

    t0 = time.monotonic()
    try:
        if timeout_s is not None:
            value = await asyncio.wait_for(call(), timeout=timeout_s)
        else:
            value = await call()
    except asyncio.TimeoutError:
        return SeatResult(name=name, ok=False, error_kind=ERROR_KIND_TIMEOUT,
                          error=f"timeout after {timeout_s}s",
                          elapsed=time.monotonic() - t0)
    except Exception as exc:
        return SeatResult(name=name, ok=False, error_kind=ERROR_KIND_EXCEPTION,
                          error=_summary(exc), elapsed=time.monotonic() - t0)
    return SeatResult(name=name, ok=True, value=value, elapsed=time.monotonic() - t0)


async def run_seats(
    calls: dict[str, Callable[[], Awaitable[Any]]],
    *,
    timeout_s: float | None = None,
) -> dict[str, SeatResult]:
    """多席并行（gather 语义收编）：每席独立捕获互不拖垮，空输入返回空 dict。"""
    if not calls:
        return {}
    results = await asyncio.gather(
        *(run_seat(name, call, timeout_s=timeout_s) for name, call in calls.items())
    )
    return {seat.name: seat for seat in results}


def seats_ok(results: dict[str, SeatResult]) -> bool:
    return bool(results) and all(seat.ok for seat in results.values())


def first_error_kind(results: dict[str, SeatResult]) -> str | None:
    for seat in results.values():
        if not seat.ok and seat.error_kind:
            return seat.error_kind
    return None


__all__ = [
    "DEGRADE_ASK_FAILED",
    "DEGRADE_ALL_EMPTY",
    "DEGRADE_EMPTY_INVALID_JSON",
    "DEGRADE_SEAT_FAILED",
    "DEGRADE_TIMEOUT_CAP",
    "ERROR_KIND_EXCEPTION",
    "ERROR_KIND_TIMEOUT",
    "SeatResult",
    "first_error_kind",
    "run_seat",
    "run_seats",
    "seats_ok",
]
