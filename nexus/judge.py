"""判官通用核（r57 批2 收编）：跨族选模 + 断供退避重试 + 分维解析骨架。

收编边界（评审门分层修正）：只收三仓逐字重复的通用骨架——arcade
services/judge.py 与 smartCode evals/judge_router.py 同源段（pick_judge_model/
候选表/断电语义）。rubric 维度表、人格头、过线阈值、结果对象形状是业务域资产，
经参数注入不进共享仓；降级结果 dataclass 等第二消费者出现后再收（r58 门A 前置）。

射程外（诚实边界，与 arcade s149 行为指纹「射程外登记」一致）：组级编排、
schema 修复环、评分 rubric——不改变这些面。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("nexus.judge")

DEGRADE_KIND_BUDGET = "budget_exceeded"
DEGRADE_KIND_REVIEW = "review_exhausted"
DEGRADE_KIND_SUPPLY = "supply_broken"

DEFAULT_PRIMARY_MODEL = "deepseek-flash"
JUDGE_CANDIDATES: tuple[str, ...] = ("glm-4.5-air",)
NO_CROSS_FAMILY_NOTE = "judge_skipped:no_cross_family_judge"


def model_family(model: str) -> str:
    return (model or "").split("-")[0].strip().lower()


def pick_judge_model(
    primary_model: str = DEFAULT_PRIMARY_MODEL,
    candidates: tuple[str, ...] = JUDGE_CANDIDATES,
) -> Optional[str]:
    primary_family = model_family(primary_model)
    for candidate in candidates:
        if model_family(candidate) != primary_family:
            return candidate
    return None


def resolve_judge_model(
    explicit: str,
    primary_model: str = DEFAULT_PRIMARY_MODEL,
    candidates: tuple[str, ...] = JUDGE_CANDIDATES,
) -> tuple[Optional[str], str]:
    """显式钉模跳过跨族候选（r25 判官钉模语义单源化）；返回 (picked, judge_model)。"""
    if explicit:
        return None, explicit
    picked = pick_judge_model(primary_model, candidates)
    return picked, picked or primary_model


def is_circuit_open(exc: Exception) -> bool:
    """按类型名判定（业务仓网关链可能包一层再抛，类恒等判定会漏网）。"""
    return type(exc).__name__ == "CircuitBreakerOpenError"


async def ask_with_supply_retry(
    ask: Callable[[], Awaitable[object]],
    retry_delay_s: float = 20.0,
) -> object:
    """断供退避重试一次（r26 判例单源化）：熔断打开不重试，第二次失败如实上抛。

    重试撞开闸白等且殃及同进程其他 LLM 消费方；20s 窗口覆盖短瞬断供。
    """
    try:
        return await ask()
    except Exception as exc:
        if is_circuit_open(exc):
            raise
        logger.warning("judge supply retry after: %s: %s", type(exc).__name__, str(exc)[:140])
    await asyncio.sleep(retry_delay_s)
    return await ask()


def parse_group_scores(
    data: object,
    group_dims: tuple[str, ...],
    cap: int = 2,
) -> Optional[tuple[dict[str, int], list[str]]]:
    """分维 0..cap 解析：非 dict/缺键/越界/非整数→None（调用侧按缺键重问一次语义）。"""
    if not isinstance(data, dict):
        return None
    dims: dict[str, int] = {}
    for key in group_dims:
        value = data.get(key)
        if not isinstance(value, int) or value < 0 or value > cap:
            return None
        dims[key] = value
    feedback = [str(item)[:200] for item in data.get("feedback", []) if isinstance(item, str)]
    return dims, feedback
