"""同步令牌桶：按时间比例补充的出站限速/配额原语。"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    """线程安全令牌桶（sync）。

    与 SlidingWindow（async、HTTP 请求滑窗计数）不同构：本类面向出站调用
    节流与配额控制，容量内即通过，令牌按 refill_rate（个/秒）随时间比例
    回补。等待式消费由调用方组合 consume + retry_after 实现。
    """

    def __init__(self, capacity: float, refill_rate: float) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")
        self._capacity: float = float(capacity)
        self._refill_rate: float = float(refill_rate)
        self._tokens: float = float(capacity)
        self._last: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

    def consume(self, amount: float = 1.0) -> bool:
        """尝试扣减令牌，充足返回 True（扣减），不足返回 False（不扣减）。"""
        if amount <= 0:
            raise ValueError("amount must be positive")
        with self._lock:
            self._refill_locked()
            if self._tokens >= amount:
                self._tokens -= amount
                return True
            return False

    def retry_after(self, amount: float = 1.0) -> float:
        """距 amount 个令牌可用的秒数；当前已充足时为 0.0。"""
        with self._lock:
            self._refill_locked()
            if self._tokens >= amount:
                return 0.0
            return (amount - self._tokens) / self._refill_rate

    def refund(self, amount: float = 1.0) -> None:
        """归还已扣额度（失败补偿），不超过容量。"""
        with self._lock:
            self._tokens = min(self._capacity, self._tokens + amount)

    def remaining(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def refill_rate(self) -> float:
        return self._refill_rate

    def _refill_locked(self) -> None:
        now: float = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._refill_rate)
        self._last = now
