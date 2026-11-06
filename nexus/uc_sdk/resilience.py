"""UC SDK 弹性原语：请求熔断器、jti 黑名单 TTL 缓存与 org 声明复核节流器。"""
import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 60.0):
        self._failure_count: int = 0
        self._failure_threshold: int = failure_threshold
        self._reset_timeout: float = reset_timeout
        self._open_until: float = 0

    @property
    def is_open(self) -> bool:
        if self._failure_count < self._failure_threshold:
            return False
        if time.time() >= self._open_until:
            self._failure_count = 0
            return False
        return True

    def record_failure(self):
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._open_until = time.time() + self._reset_timeout
            logger.warning(f"断路器打开: failure_count={self._failure_count}, reset_in={self._reset_timeout}s")

    def record_success(self):
        self._failure_count = 0


class BlacklistCache:
    def __init__(self, ttl: float = 30.0, max_size: int = 10000):
        self._cache: Dict[str, tuple] = {}
        self._ttl: float = ttl
        self._max_size: int = max_size
        self._last_sync_at: float = 0
        self._sync_interval: float = 60.0
        self._lock = None

    def _get_lock(self):
        if self._lock is None:
            import asyncio
            self._lock = asyncio.Lock()
        return self._lock

    async def is_blacklisted(self, jti: str) -> bool | None:
        async with self._get_lock():
            if jti not in self._cache:
                return None
            is_blacklisted, cached_at = self._cache[jti]
            if time.time() - cached_at > self._ttl:
                del self._cache[jti]
                return None
            return is_blacklisted

    async def mark_blacklisted(self, jti: str):
        async with self._get_lock():
            self._cache[jti] = (True, time.time())
            if len(self._cache) > self._max_size:
                self._evict_expired()

    def needs_sync(self) -> bool:
        return time.time() - self._last_sync_at > self._sync_interval

    def mark_synced(self):
        self._last_sync_at = time.time()

    def _evict_expired(self):
        now = time.time()
        expired = [k for k, (_, t) in self._cache.items() if now - t > self._ttl]
        for k in expired:
            del self._cache[k]


class OrgRecheckCache:
    """org 声明低频复核节流器：jti -> 上次复核时间戳。

    本地验签路径（JWKS/HS256）的 org 字段来自 token 声明，被移出/降级后
    在 access TTL 内漂移；本缓存把对权威端点的复核压到每 jti 每 TTL 至多
    一次。复核失败（UC 不可达）同样 mark：fail-open 保留本地声明的同时
    进入退避，不因 UC 故障打爆 validate 端点。方法全同步——check+mark
    之间无 await，事件循环内天然原子，单进程无需加锁。
    """

    def __init__(self, ttl: float = 60.0, max_size: int = 10000):
        self._cache: Dict[str, float] = {}
        self._ttl: float = ttl
        self._max_size: int = max_size

    def needs_recheck(self, jti: str) -> bool:
        checked_at = self._cache.get(jti)
        if checked_at is None:
            return True
        return time.time() - checked_at > self._ttl

    def mark_rechecked(self, jti: str):
        self._cache[jti] = time.time()
        if len(self._cache) > self._max_size:
            self._evict_expired()

    def _evict_expired(self):
        now = time.time()
        expired = [k for k, t in self._cache.items() if now - t > self._ttl]
        for k in expired:
            del self._cache[k]
