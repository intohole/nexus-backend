"""LLM cost budget guard - token/cost budgets per period, block on exceed.

Shared LLM budget primitive for all apps. Originally from goldenFish,
promoted to middleware. r217 rework: rolling 24h window -> UTC calendar
window (litellm budget_duration semantics; r14 stall root cause removed);
in-memory list -> optional SQLite WAL persistence (litellm: "Budgets
require a database" - restart must not resurrect a spent budget);
unknown-model pricing fallback now warns + counts (was silent);
call_with_guard records real prompt/completion split (//2 hard split
distorts cost up to 1:6.4 price gap).
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from ironman.observability.pricing import PRICING_TABLE as _IRONMAN_PRICING

from nexus.logging import get_logger

logger = get_logger("nexus.cost_guard")

# Per-1K-token USD prices; apps may override via configure_pricing().
# Single source: ironman PRICING_TABLE (nexus->ironman dependency direction).
# Kept: business-facing claude aliases (ironman table only has full names).

DEFAULT_PRICING: Dict[str, Dict[str, float]] = {
    **_IRONMAN_PRICING,
    "claude-haiku": {"prompt": 0.00025, "completion": 0.00125},
    "claude-sonnet": {"prompt": 0.003, "completion": 0.015},
    "claude-opus": {"prompt": 0.015, "completion": 0.075},
}
# Fallback price for unknown models: fleet-active main model
# (deepseek-flash peak conservative). Old gpt-4o-mini is on no fleet chain.
_FALLBACK_MODEL = "deepseek-flash"

_pricing: Dict[str, Dict[str, float]] = dict(DEFAULT_PRICING)
_unknown_pricing_events = 0


def configure_pricing(pricing: Dict[str, Dict[str, float]]) -> None:
    """Override/extend model pricing table (key: model name, value: {"prompt": $/1K, "completion": $/1K})."""
    _pricing.update(pricing)


def get_unknown_pricing_events() -> int:
    return _unknown_pricing_events


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
    """Budget exhausted or estimate over remaining."""


def _calendar_period_start(period: BudgetPeriod, now: float) -> float:
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    if period == BudgetPeriod.DAILY:
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == BudgetPeriod.WEEKLY:
        start = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    model TEXT NOT NULL DEFAULT '',
    operation TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cost_usage_ts ON cost_usage(ts);
"""


class CostGuard:
    def __init__(self, budget: Optional[CostBudget] = None, data_path: Optional[Path] = None):
        self._budget = budget or CostBudget()
        self._data_path = Path(data_path) if data_path else None
        self._usages: List[TokenUsage] = []
        self._lock = asyncio.Lock()
        self._warning_sent = False
        self._blocked = False
        if self._data_path is not None:
            self._data_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self._data_path), check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(_DB_SCHEMA)
            self._db.commit()

    @property
    def is_blocked(self) -> bool:
        tokens, cost = self._aggregate()
        self._blocked = self._usage_rate(tokens, cost) >= 1.0
        return self._blocked

    def _get_period_start(self) -> float:
        return _calendar_period_start(self._budget.period, time.time())

    def _usage_rate(self, total_tokens: int, total_cost: float) -> float:
        token_rate = total_tokens / self._budget.max_tokens if self._budget.max_tokens > 0 else 0
        cost_rate = total_cost / self._budget.max_cost_usd if self._budget.max_cost_usd > 0 else 0
        return max(token_rate, cost_rate)

    def _aggregate(self) -> tuple:
        since = self._get_period_start()
        if self._data_path is not None:
            row = self._db.execute(
                "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0.0) "
                "FROM cost_usage WHERE ts >= ?",
                (since,),
            ).fetchone()
            return int(row[0]), float(row[1])
        usages = [u for u in self._usages if u.timestamp >= since]
        return sum(u.total_tokens for u in usages), sum(u.estimated_cost_usd for u in usages)

    def _aggregate_by_operation(self) -> Dict[str, Dict[str, object]]:
        since = self._get_period_start()
        out: Dict[str, Dict[str, object]] = {}
        if self._data_path is not None:
            rows = self._db.execute(
                "SELECT operation, SUM(total_tokens), SUM(cost_usd), COUNT(*) "
                "FROM cost_usage WHERE ts >= ? GROUP BY operation",
                (since,),
            ).fetchall()
            for op, tokens, cost, count in rows:
                out[op] = {"tokens": int(tokens), "cost": float(cost), "count": int(count)}
            return out
        for u in (x for x in self._usages if x.timestamp >= since):
            op = out.setdefault(u.operation, {"tokens": 0, "cost": 0.0, "count": 0})
            op["tokens"] = int(op["tokens"]) + u.total_tokens
            op["cost"] = float(op["cost"]) + u.estimated_cost_usd
            op["count"] = int(op["count"]) + 1
        return out

    @staticmethod
    def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
        global _unknown_pricing_events
        p = _pricing.get(model)
        if p is None:
            _unknown_pricing_events += 1
            logger.warning(
                "CostGuard: no pricing for model %r, estimating with fallback %r (unknown_pricing_events=%d)",
                model, _FALLBACK_MODEL, _unknown_pricing_events,
            )
            p = _pricing[_FALLBACK_MODEL]
        return (prompt_tokens * p["prompt"] + completion_tokens * p["completion"]) / 1000

    async def check_budget(self, estimated_tokens: int = 0) -> Dict[str, object]:
        async with self._lock:
            total_tokens, total_cost = self._aggregate()
            remaining_tokens = max(0, self._budget.max_tokens - total_tokens)
            remaining_cost = max(0.0, self._budget.max_cost_usd - total_cost)
            usage_rate = self._usage_rate(total_tokens, total_cost)
            self._blocked = usage_rate >= 1.0

            if self._blocked:
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
            if self._data_path is not None:
                self._db.execute(
                    "INSERT INTO cost_usage (ts, prompt_tokens, completion_tokens, total_tokens, cost_usd, model, operation) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (usage.timestamp, usage.prompt_tokens, usage.completion_tokens,
                     usage.total_tokens, usage.estimated_cost_usd, usage.model, usage.operation),
                )
                self._db.commit()
            else:
                self._usages.append(usage)
                if len(self._usages) > 10000:
                    self._usages = self._usages[-5000:]
        logger.info("Token usage: %s tokens ($%.4f) for %s using %s", total, cost, usage.operation, usage.model)

    async def call_with_guard(self, func: Callable[..., Awaitable[object]], estimated_tokens: int = 0,
                              model: str = "", operation: str = "", *args: object, **kwargs: object) -> object:
        check = await self.check_budget(estimated_tokens=estimated_tokens)
        if not check["allowed"]:
            raise CostBudgetExceededError(str(check["reason"]))
        result = await func(*args, **kwargs)
        usage = getattr(result, "usage", None)
        usage = usage if isinstance(usage, dict) else {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        if prompt or completion:
            await self.record_usage(prompt_tokens=prompt, completion_tokens=completion,
                                    model=model, operation=operation)
            return result
        total = int(usage.get("total_tokens") or 0) or estimated_tokens
        if total:
            logger.info("CostGuard: usage breakdown unavailable, crediting %d tokens as completion (conservative side)", total)
            await self.record_usage(prompt_tokens=0, completion_tokens=total,
                                    model=model, operation=operation)
        return result

    def get_summary(self) -> Dict[str, object]:
        total_tokens, total_cost = self._aggregate()
        return {
            "budget": {"max_tokens": self._budget.max_tokens, "max_cost_usd": self._budget.max_cost_usd,
                       "period": self._budget.period.value, "warning_threshold": self._budget.warning_threshold},
            "window_start": self._get_period_start(),
            "current_usage": {"total_tokens": total_tokens, "total_cost_usd": round(total_cost, 4),
                              "usage_rate": round(total_tokens / self._budget.max_tokens, 4) if self._budget.max_tokens > 0 else 0},
            "by_operation": self._aggregate_by_operation(),
            "unknown_pricing_events": _unknown_pricing_events,
            "is_blocked": self._blocked,
        }

    def reset_budget(self) -> None:
        if self._data_path is not None:
            self._db.execute("DELETE FROM cost_usage")
            self._db.commit()
        else:
            self._usages.clear()
        self._warning_sent = False
        self._blocked = False
        logger.info("CostGuard budget reset")


_cost_guard: Optional[CostGuard] = None


def get_cost_guard(budget: Optional[CostBudget] = None,
                   data_path: Optional[Path] = None) -> CostGuard:
    global _cost_guard
    if _cost_guard is None:
        env_path = os.getenv("NEXUS_COST_GUARD_DB")
        _cost_guard = CostGuard(budget=budget,
                                data_path=Path(env_path) if env_path else data_path)
    return _cost_guard
