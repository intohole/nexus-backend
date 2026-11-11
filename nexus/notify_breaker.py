"""通知出口连接断路器：closed→open→half-open 三态，进程内内存态零依赖。

只吃连接级失败（ConnectError/ConnectTimeout/PoolTimeout=服务不可达证据）；
读/写超时与 HTTP 状态错误意味着 TCP 已通（服务在，可能已投递，S62 非幂等
语义）不计开断、且视为连接健康证据清零连败。open 期请求零出网快速失败，
冷却期满放行 half-open 探测：成功即 close，连接失败即重开并刷新冷却计时。
参数口径（业界对标 Saleor webhooks breaker 冷却 2min/高吞吐，本舰队通知
低频取半）：连败阈值 2、冷却 60s、half-open 探测无严格单发锁（宽松版
resilience4j half-open 语义）。
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

from nexus.logging import get_logger

logger = get_logger("nexus.notify_breaker")

CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
DEFAULT_THRESHOLD = 2
DEFAULT_COOLDOWN_S = 60.0


class BreakerOpenError(Exception):
    """open 期快速失败哨兵：调用方按 failed(notify_center_breaker_open) 分型。"""


class ConnectBreaker:
    def __init__(
        self,
        threshold: int = DEFAULT_THRESHOLD,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
    ) -> None:
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._consecutive = 0
        self._opened_at: Optional[float] = None

    def state(self, now: float) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if now - self._opened_at >= self._cooldown_s else "open"

    def allow(self, now: float) -> bool:
        return self.state(now) != "open"

    def on_success(self) -> None:
        self._consecutive = 0
        self._opened_at = None

    def on_reachable(self) -> None:
        self._consecutive = 0

    def on_connect_failure(self, now: float) -> None:
        if self.state(now) == "half_open":
            self._opened_at = now
            self._consecutive = 0
            logger.warning("notify breaker re-opened: half-open probe connect failure")
            return
        self._consecutive += 1
        if self._consecutive >= self._threshold and self._opened_at is None:
            self._opened_at = now
            logger.warning(
                "notify breaker opened: %d consecutive connect failures, cooldown %.0fs",
                self._consecutive, self._cooldown_s,
            )


def note_exception(breaker: ConnectBreaker, exc: Exception) -> None:
    if isinstance(exc, CONNECT_ERRORS):
        breaker.on_connect_failure(time.monotonic())
    elif isinstance(exc, httpx.TransportError):
        breaker.on_reachable()
