"""LLM 生成任务的墙钟阶段预算：超限降级而非炸任务。

与 llm_budget（token 输出预算）不同轴：本模块管任务级墙钟。
教训背景（MiaoBi r17 / verseCraft 生产实锤）：串行增强链（压缩/一致性/读者模拟/
质量环）任何一个吃满单调用超时都会拖爆整个任务；增强是加分项不是门槛。
"""
from __future__ import annotations

import contextvars
import time


class GenerationBudget:
    """墙钟预算：从创建时刻计时，remaining() 返回剩余秒数。"""

    def __init__(self, total_seconds: float):
        self.total_seconds = float(total_seconds)
        self._started = time.monotonic()

    def remaining(self) -> float:
        return self.total_seconds - (time.monotonic() - self._started)

    def allows(self, need_seconds: float) -> bool:
        return self.remaining() >= need_seconds

    def stage_timeout(self, limit_seconds: float, reserve: float = 0.0) -> float | None:
        """增强阶段独立上限：min(阶段上限, 剩余-保留)。不足则返回 None（调用方降级跳过）。"""
        affordable = self.remaining() - reserve
        if affordable <= 0:
            return None
        return min(limit_seconds, affordable)


_budget_var: contextvars.ContextVar[GenerationBudget | None] = contextvars.ContextVar(
    "generation_budget", default=None
)


def set_generation_budget(total_seconds: float) -> GenerationBudget:
    budget = GenerationBudget(total_seconds)
    _budget_var.set(budget)
    return budget


def current_budget() -> GenerationBudget | None:
    return _budget_var.get()
