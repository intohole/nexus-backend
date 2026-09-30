"""滑动窗口计数器（HTTP 层 canonical 实现）。

平行实现互查：nexus.llm_rate_limiter.LLMRateLimiter（LLM 调用维度）、
ironman.middleware.rate_limit_mw.SlidingWindowLimiter（ironman 管道内 rpm 单例，
依赖方向 nexus→ironman 禁止反向收归）。改动计数/窗口语义时三处同步。
"""
from __future__ import annotations

import asyncio
import time


class SlidingWindow:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max_requests: int = max_requests
        self._window_seconds: int = window_seconds
        self._timestamps: list[float] = []
        self._lock: asyncio.Lock = asyncio.Lock()

    async def is_allowed(self) -> bool:
        async with self._lock:
            now: float = time.time()
            cutoff: float = now - self._window_seconds
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self._max_requests:
                return False
            self._timestamps.append(now)
            return True

    def is_exceeded(self) -> bool:
        now: float = time.time()
        cutoff: float = now - self._window_seconds
        return len([t for t in self._timestamps if t > cutoff]) >= self._max_requests

    def current_count(self) -> int:
        now: float = time.time()
        cutoff: float = now - self._window_seconds
        return len([t for t in self._timestamps if t > cutoff])

    def retry_after(self) -> int:
        if not self._timestamps:
            return 0
        now: float = time.time()
        oldest: float = self._timestamps[0]
        return max(1, int(oldest + self._window_seconds - now))

    @property
    def max_requests(self) -> int:
        return self._max_requests

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    def is_idle(self, now: float, factor: float = 2.0) -> bool:
        return not self._timestamps or (now - self._timestamps[-1]) > factor * self._window_seconds
