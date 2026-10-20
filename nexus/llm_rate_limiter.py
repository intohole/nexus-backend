"""LLM 调用限流器 — 速率限制 + 并发控制，防止并行任务触发 LLM 网关雪崩。

双通道：交互通道（默认）与后台通道（background=True，独立窗口+信号量，
交互排队时后台让行），后台批量任务不再挤占交互配额。
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

_BG_YIELD_SECONDS = 0.1


class LLMRateLimiter:
    def __init__(self, rate_limit: int = 10, period: float = 5.0, max_concurrent: int = 5,
                 background_rate_limit: Optional[int] = None,
                 background_period: Optional[float] = None,
                 background_max_concurrent: int = 1):
        self._rate_limit = rate_limit
        self._period = period
        self._max_concurrent = max_concurrent
        self._background_rate_limit = rate_limit if background_rate_limit is None else background_rate_limit
        self._background_period = period if background_period is None else background_period
        self._background_max_concurrent = background_max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._bg_semaphore = asyncio.Semaphore(background_max_concurrent)
        self._timestamps: list[float] = []
        self._bg_timestamps: list[float] = []
        self._lock = asyncio.Lock()
        self._total_calls = 0
        self._throttled_calls = 0
        self._bg_total_calls = 0
        self._bg_throttled_calls = 0
        self._interactive_waiting = 0
        self._config_ts: float = 0.0
        self._warm_task: Optional[asyncio.Task] = None

    def _channel(self, background: bool) -> tuple[list[float], int, float]:
        if background:
            return self._bg_timestamps, self._background_rate_limit, self._background_period
        return self._timestamps, self._rate_limit, self._period

    @asynccontextmanager
    async def limited(self, caller: str = "", background: bool = False) -> AsyncGenerator[None, None]:
        async with (self._bg_semaphore if background else self._semaphore):
            await self._wait_for_rate_limit(caller, background)
            if background:
                self._bg_total_calls += 1
            else:
                self._total_calls += 1
            yield

    async def acquire(self, background: bool = False) -> None:
        await (self._bg_semaphore if background else self._semaphore).acquire()
        await self._wait_for_rate_limit(background=background)
        if background:
            self._bg_total_calls += 1
        else:
            self._total_calls += 1

    def release(self, background: bool = False) -> None:
        (self._bg_semaphore if background else self._semaphore).release()

    async def _yield_to_interactive(self, caller: str) -> None:
        logged = False
        while self._interactive_waiting > 0:
            if not logged:
                logger.info("后台调用让行交互队列: caller=%s, 交互等待=%d", caller, self._interactive_waiting)
                logged = True
            await asyncio.sleep(_BG_YIELD_SECONDS)

    async def _wait_for_rate_limit(self, caller: str = "", background: bool = False) -> None:
        if background:
            await self._yield_to_interactive(caller)
        timestamps, rate_limit, period = self._channel(background)
        is_waiting = False
        try:
            while True:
                wait_time = 0.0
                async with self._lock:
                    now = time.monotonic()
                    timestamps[:] = [t for t in timestamps if now - t < period]
                    if len(timestamps) >= rate_limit:
                        oldest = timestamps[0]
                        wait_time = period - (now - oldest) + 0.1
                        if wait_time > 0:
                            if background:
                                self._bg_throttled_calls += 1
                            else:
                                self._throttled_calls += 1
                if wait_time <= 0:
                    break
                if not background and not is_waiting:
                    is_waiting = True
                    self._interactive_waiting += 1
                logger.info("LLM限流等待: %.1fs (%s次/%ss, caller=%s)",
                            wait_time, rate_limit, period, caller)
                await asyncio.sleep(wait_time)
            async with self._lock:
                timestamps.append(time.monotonic())
        finally:
            if is_waiting:
                self._interactive_waiting -= 1

    def get_stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "throttled_calls": self._throttled_calls,
            "rate_limit": f"{self._rate_limit}/{self._period}s",
            "max_concurrent": self._max_concurrent,
            "interactive_waiting": self._interactive_waiting,
            "background_total_calls": self._bg_total_calls,
            "background_throttled_calls": self._bg_throttled_calls,
            "background_rate_limit": f"{self._background_rate_limit}/{self._background_period}s",
            "background_max_concurrent": self._background_max_concurrent,
        }

    def maybe_refresh_config(self) -> None:
        """TTL 到期重读配置（lion 侧只读缓存零 IO）； lion 配额缓存由异步预热任务打通。"""
        if time.monotonic() - self._config_ts < _CONFIG_REFRESH_TTL:
            return
        self._config_ts = time.monotonic()
        rate, period, concurrent = _resolve_config()
        bg_rate, bg_period, bg_concurrent = _resolve_background_config(rate, period)
        if rate != self._rate_limit or period != self._period:
            logger.info("LLM限流配置热更新: %s次/%ss -> %s次/%ss",
                        self._rate_limit, self._period, rate, period)
            self._rate_limit, self._period = rate, period
        if bg_rate != self._background_rate_limit or bg_period != self._background_period:
            logger.info("LLM限流后台通道配置热更新: %s次/%ss -> %s次/%ss",
                        self._background_rate_limit, self._background_period, bg_rate, bg_period)
            self._background_rate_limit, self._background_period = bg_rate, bg_period
        if concurrent != self._max_concurrent:
            logger.warning("LLM限流 max_concurrent 变更 %s->%s 需重启生效（信号量结构参数）",
                           self._max_concurrent, concurrent)
        if bg_concurrent != self._background_max_concurrent:
            logger.warning("LLM限流后台 max_concurrent 变更 %s->%s 需重启生效（信号量结构参数）",
                           self._background_max_concurrent, bg_concurrent)
        self._schedule_business_warm()

    def _schedule_business_warm(self) -> None:
        """异步预热 llm_quota：get_business_config_sync 只读缓存，无预热则 lion 配置恒空。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        self._warm_task = loop.create_task(self._warm_business_config())

    async def _warm_business_config(self) -> None:
        try:
            from nexus.lion import get_lion
            lion = get_lion()
            if hasattr(lion, "get_business_config"):
                await lion.get_business_config("llm_quota")
        except Exception:
            logger.debug("llm_quota 预热跳过（lion 未配置或不可达）")


_rate_limiter: Optional[LLMRateLimiter] = None
_rate_limiter_lock = threading.Lock()
_CONFIG_REFRESH_TTL: float = 60.0


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


def _resolve_background_config(rate: int, period: float) -> tuple[int, float, int]:
    import os
    bg_rate = int(os.environ.get("LLM_BG_RATE_LIMIT", str(rate)))
    bg_period = float(os.environ.get("LLM_BG_RATE_PERIOD", str(period)))
    bg_concurrent = int(os.environ.get("LLM_BG_MAX_CONCURRENT", "1"))
    try:
        from nexus.lion import get_lion
        lion = get_lion()
        cfg = lion.get_business_config_sync("llm_quota") if hasattr(lion, "get_business_config_sync") else None
        if cfg:
            bg_rate = int(cfg.get("background_rate_limit", bg_rate))
            bg_period = float(cfg.get("background_rate_period", bg_period))
            bg_concurrent = int(cfg.get("background_max_concurrent", bg_concurrent))
    except Exception:
        pass
    return bg_rate, bg_period, bg_concurrent


def get_llm_rate_limiter() -> LLMRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        with _rate_limiter_lock:
            if _rate_limiter is None:
                rate, period, concurrent = _resolve_config()
                bg_rate, bg_period, bg_concurrent = _resolve_background_config(rate, period)
                _rate_limiter = LLMRateLimiter(rate, period, concurrent,
                                               bg_rate, bg_period, bg_concurrent)
                _rate_limiter._config_ts = time.monotonic()
                logger.info("LLM限流器初始化: %s次/%ss, 最大并发=%s, 后台=%s次/%ss并发%s",
                            rate, period, concurrent, bg_rate, bg_period, bg_concurrent)
    else:
        _rate_limiter.maybe_refresh_config()
    return _rate_limiter
