"""LLM 调用限流器 — 速率限制 + 并发控制，防止并行任务触发 LLM 网关雪崩。

配置优先级: lion business/llm_quota > 环境变量 > 默认值。
源自 golden 生产实践，上移为通用中间件能力。
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from nexus.logging import get_logger

logger = get_logger("nexus.llm_rate_limiter")


class LLMRateLimiter:
    def __init__(self, rate_limit: int = 10, period: float = 5.0, max_concurrent: int = 5):
        self._rate_limit = rate_limit
        self._period = period
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()
        self._total_calls = 0
        self._throttled_calls = 0

    @asynccontextmanager
    async def limited(self, caller: str = "", priority: int = 1) -> AsyncGenerator[None, None]:
        async with self._semaphore:
            await self._wait_for_rate_limit(caller)
            self._total_calls += 1
            yield

    async def acquire(self) -> None:
        await self._semaphore.acquire()
        await self._wait_for_rate_limit()
        self._total_calls += 1

    def release(self) -> None:
        self._semaphore.release()

    async def _wait_for_rate_limit(self, caller: str = "") -> None:
        wait_time = 0.0
        async with self._lock:
            now = time.monotonic()
            self._timestamps = [t for t in self._timestamps if now - t < self._period]
            if len(self._timestamps) >= self._rate_limit:
                oldest = self._timestamps[0]
                wait_time = self._period - (now - oldest) + 0.1
                if wait_time > 0:
                    self._throttled_calls += 1
            self._timestamps.append(time.monotonic())
        if wait_time > 0:
            logger.info("LLM限流等待: %.1fs (%s次/%ss, caller=%s)",
                        wait_time, len(self._timestamps), self._period, caller)
            await asyncio.sleep(wait_time)

    def get_stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "throttled_calls": self._throttled_calls,
            "rate_limit": f"{self._rate_limit}/{self._period}s",
            "max_concurrent": self._max_concurrent,
        }


_rate_limiter: Optional[LLMRateLimiter] = None
_rate_limiter_lock = threading.Lock()


def _resolve_config() -> tuple[int, float, int]:
    import os
    rate = int(os.environ.get("LLM_RATE_LIMIT", "10"))
    period = float(os.environ.get("LLM_RATE_PERIOD", "5.0"))
    concurrent = int(os.environ.get("LLM_MAX_CONCURRENT", "5"))
    try:
        from nexus.lion import get_lion
        lion = get_lion()
        cfg = lion.get_business_config_sync("llm_quota") if hasattr(lion, "get_business_config_sync") else None
        if cfg:
            rate = int(cfg.get("rate_limit", rate))
            period = float(cfg.get("rate_period", period))
            concurrent = int(cfg.get("max_concurrent", concurrent))
    except Exception:
        pass
    return rate, period, concurrent


def get_llm_rate_limiter() -> LLMRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        with _rate_limiter_lock:
            if _rate_limiter is None:
                rate, period, concurrent = _resolve_config()
                _rate_limiter = LLMRateLimiter(rate, period, concurrent)
                logger.info("LLM限流器初始化: %s次/%ss, 最大并发=%s", rate, period, concurrent)
    return _rate_limiter
