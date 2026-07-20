"""LLM 成本预算守卫 — 按周期控制 token/费用预算，超限阻断。

所有调用 LLM 的应用共用此能力，避免 token 费用失控。
源自 goldenFish 生产实践，上移为通用中间件能力。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional

from nexus.logging import get_logger

logger = get_logger("nexus.cost_guard")

# 每 1K token 的美元价格，应用可通过 configure_pricing() 覆盖
DEFAULT_PRICING: Dict[str, Dict[str, float]] = {
    "gpt-4o": {"prompt": 0.005, "completion": 0.015},
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4": {"prompt": 0.03, "completion": 0.06},
    "gpt-3.5-turbo": {"prompt": 0.0005, "completion": 0.0015},
    "claude-haiku": {"prompt": 0.00025, "completion": 0.00125},
    "claude-sonnet": {"prompt": 0.003, "completion": 0.015},
    "claude-opus": {"prompt": 0.015, "completion": 0.075},
    "deepseek-chat": {"prompt": 0.00014, "completion": 0.00028},
    "glm-4": {"prompt": 0.014, "completion": 0.014},
    "glm-4-flash": {"prompt": 0.0, "completion": 0.0},
}
_FALLBACK_MODEL = "gpt-4o-mini"

_pricing: Dict[str, Dict[str, float]] = dict(DEFAULT_PRICING)


def configure_pricing(pricing: Dict[str, Dict[str, float]]) -> None:
    """覆盖/扩展模型定价表（键为模型名，值为 {"prompt": $/1K, "completion": $/1K}）。"""
    _pricing.update(pricing)


class BudgetPeriod(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class CostBudget:
    max_tokens: int = 100000
    max_cost_usd: float = 10.0
    period: BudgetPeriod = BudgetPeriod.DAILY
    warning_threshold: float = 0.8


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    timestamp: float = 0.0
    model: str = ""
    operation: str = ""


class CostBudgetExceededError(Exception):
    """预算耗尽或预估超限时抛出。"""


class CostGuard:
    def __init__(self, budget: Optional[CostBudget] = None):
        self._budget = budget or CostBudget()
        self._usages: List[TokenUsage] = []
        self._lock = asyncio.Lock()
        self._warning_sent = False
        self._blocked = False

    @property
    def is_blocked(self) -> bool:
        return self._blocked

    def _get_period_start(self) -> float:
        now = time.time()
        if self._budget.period == BudgetPeriod.DAILY:
            return now - 86400
        if self._budget.period == BudgetPeriod.WEEKLY:
            return now - 604800
        return now - 2592000

    def _get_usages_in_period(self) -> List[TokenUsage]:
        period_start = self._get_period_start()
        return [u for u in self._usages if u.timestamp >= period_start]

    @staticmethod
    def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p = _pricing.get(model, _pricing[_FALLBACK_MODEL])
        return (prompt_tokens * p["prompt"] + completion_tokens * p["completion"]) / 1000

    async def check_budget(self, estimated_tokens: int = 0, model: str = "") -> Dict[str, object]:
        async with self._lock:
            usages = self._get_usages_in_period()
            total_tokens = sum(u.total_tokens for u in usages)
            total_cost = sum(u.estimated_cost_usd for u in usages)
            remaining_tokens = max(0, self._budget.max_tokens - total_tokens)
            remaining_cost = max(0.0, self._budget.max_cost_usd - total_cost)
            token_rate = total_tokens / self._budget.max_tokens if self._budget.max_tokens > 0 else 0
            cost_rate = total_cost / self._budget.max_cost_usd if self._budget.max_cost_usd > 0 else 0
            usage_rate = max(token_rate, cost_rate)

            if usage_rate >= 1.0:
                self._blocked = True
                return {"allowed": False, "reason": f"Budget exceeded: {total_tokens} tokens, ${total_cost:.4f}",
                        "usage_rate": usage_rate, "remaining_tokens": 0, "remaining_cost": 0.0}

            if usage_rate >= self._budget.warning_threshold and not self._warning_sent:
                self._warning_sent = True
                logger.warning("CostGuard: usage %.1f%% of budget (%s tokens, $%.4f)",
                               usage_rate * 100, total_tokens, total_cost)

            if estimated_tokens > 0 and estimated_tokens > remaining_tokens:
                return {"allowed": False,
                        "reason": f"Estimated tokens {estimated_tokens} exceed remaining {remaining_tokens}",
                        "usage_rate": usage_rate, "remaining_tokens": remaining_tokens, "remaining_cost": remaining_cost}

            return {"allowed": True, "usage_rate": usage_rate, "remaining_tokens": remaining_tokens,
                    "remaining_cost": remaining_cost, "total_tokens": total_tokens, "total_cost": total_cost}

    async def record_usage(self, prompt_tokens: int, completion_tokens: int,
                           model: str = "", operation: str = "") -> None:
        total = prompt_tokens + completion_tokens
        cost = self.estimate_cost(model or _FALLBACK_MODEL, prompt_tokens, completion_tokens)
        usage = TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                           total_tokens=total, estimated_cost_usd=cost, timestamp=time.time(),
                           model=model or "unknown", operation=operation or "unknown")
        async with self._lock:
            self._usages.append(usage)
            if len(self._usages) > 10000:
                self._usages = self._usages[-5000:]
        logger.info("Token usage: %s tokens ($%.4f) for %s using %s", total, cost, operation, model)

    async def call_with_guard(self, func: Callable[..., Awaitable[object]], estimated_tokens: int = 0,
                              model: str = "", operation: str = "", *args: object, **kwargs: object) -> object:
        check = await self.check_budget(estimated_tokens=estimated_tokens, model=model)
        if not check["allowed"]:
            raise CostBudgetExceededError(str(check["reason"]))
        result = await func(*args, **kwargs)
        actual = getattr(result, "usage", {})
        actual_tokens = actual.get("total_tokens", estimated_tokens // 2) if isinstance(actual, dict) else estimated_tokens // 2
        await self.record_usage(prompt_tokens=actual_tokens // 2, completion_tokens=actual_tokens // 2,
                                model=model, operation=operation)
        return result

    def get_summary(self) -> Dict[str, object]:
        usages = self._get_usages_in_period()
        total_tokens = sum(u.total_tokens for u in usages)
        total_cost = sum(u.estimated_cost_usd for u in usages)
        by_operation: Dict[str, Dict[str, object]] = {}
        for u in usages:
            op = by_operation.setdefault(u.operation, {"tokens": 0, "cost": 0.0, "count": 0})
            op["tokens"] = int(op["tokens"]) + u.total_tokens
            op["cost"] = float(op["cost"]) + u.estimated_cost_usd
            op["count"] = int(op["count"]) + 1
        return {
            "budget": {"max_tokens": self._budget.max_tokens, "max_cost_usd": self._budget.max_cost_usd,
                       "period": self._budget.period.value, "warning_threshold": self._budget.warning_threshold},
            "current_usage": {"total_tokens": total_tokens, "total_cost_usd": round(total_cost, 4),
                              "usage_rate": round(total_tokens / self._budget.max_tokens, 4) if self._budget.max_tokens > 0 else 0},
            "by_operation": by_operation,
            "is_blocked": self._blocked,
        }

    def reset_budget(self) -> None:
        self._usages.clear()
        self._warning_sent = False
        self._blocked = False
        logger.info("CostGuard budget reset")


_cost_guard: Optional[CostGuard] = None


def get_cost_guard(budget: Optional[CostBudget] = None) -> CostGuard:
    global _cost_guard
    if _cost_guard is None:
        _cost_guard = CostGuard(budget=budget)
    return _cost_guard
