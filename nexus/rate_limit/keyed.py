"""按 key 分桶的限流外壳：dict[key] → SlidingWindow 的通用容器。

覆盖「按用户/IP/域名分桶 + 惰性建桶 + 过期回收」的重复样板；
桶原语仍为 SlidingWindow，等待/拒绝两种消费模式由调用方选择方法。
"""
from __future__ import annotations

import asyncio

from nexus.utils.cache import SyncTTLCache

from .window import SlidingWindow


class KeyedRateLimiter:
    """按 key 分桶的滑动窗口限流器。

    - ``is_allowed``：拒绝式，布尔判定（超限即拒）；
    - ``acquire``：等待式，阻塞至放行；
    桶按 key 惰性创建，同 key 窗口参数变化时重建；桶闲置一个窗口后由
    SyncTTLCache（逐键 ttl + LRU 上限）回收。
    """

    def __init__(self, max_keys: int = 4096) -> None:
        self._buckets = SyncTTLCache(default_ttl=3600, max_size=max_keys)

    def _bucket(self, key: str, max_requests: int, window_seconds: int) -> SlidingWindow:
        bucket = self._buckets.get(key)
        if bucket is None or bucket.window_seconds != window_seconds:
            bucket = SlidingWindow(max_requests, window_seconds)
            self._buckets.set(key, bucket, ttl=window_seconds)
        return bucket

    async def is_allowed(self, key: str, max_requests: int, window_seconds: int = 60) -> bool:
        return await self._bucket(key, max_requests, window_seconds).is_allowed()

    async def acquire(self, key: str, max_requests: int, window_seconds: int = 60) -> None:
        bucket = self._bucket(key, max_requests, window_seconds)
        while not await bucket.is_allowed():
            await asyncio.sleep(bucket.retry_after())

    def retry_after(self, key: str, max_requests: int, window_seconds: int = 60) -> int:
        return self._bucket(key, max_requests, window_seconds).retry_after()

    def reset(self) -> int:
        """清空全部桶（配置热重载场景），返回清除的桶数。"""
        return self._buckets.invalidate(None)
