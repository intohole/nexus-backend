"""进程内缓存原语：异步 MemoryCache 与线程安全同步 SyncTTLCache。"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Hashable, Optional

_MISSING: object = object()


class MemoryCache:
    def __init__(self, max_size: int = 1000) -> None:
        self._cache: OrderedDict[str, tuple[object, float, float, int]] = OrderedDict()
        self._max_size: int = max_size
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get(self, key: str) -> object:
        async with self._lock:
            if key not in self._cache:
                return _MISSING
            value, expire_at, _created_at, _ttl = self._cache[key]
            if expire_at > 0 and time.time() > expire_at:
                del self._cache[key]
                return _MISSING
            self._cache.move_to_end(key)
            return value

    async def get_or_none(self, key: str) -> object:
        value: object = await self.get(key)
        return None if value is _MISSING else value

    async def set(self, key: str, value: object, ttl: int = 0) -> None:
        async with self._lock:
            now: float = time.time()
            expire_at: float = now + ttl if ttl > 0 else 0
            self._cache[key] = (value, expire_at, now, ttl)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    async def delete(self, key: str) -> bool:
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()

    async def exists(self, key: str) -> bool:
        async with self._lock:
            if key not in self._cache:
                return False
            _, expire_at, _created_at, _ttl = self._cache[key]
            if expire_at > 0 and time.time() > expire_at:
                del self._cache[key]
                return False
            return True

    async def peek(self, key: str) -> object:
        async with self._lock:
            if key not in self._cache:
                return _MISSING
            value, expire_at, _created_at, _ttl = self._cache[key]
            if expire_at > 0 and time.time() > expire_at:
                del self._cache[key]
                return _MISSING
            return value

    async def get_entry(self, key: str) -> Optional[dict[str, object]]:
        async with self._lock:
            if key not in self._cache:
                return None
            value, expire_at, created_at, ttl = self._cache[key]
            if expire_at > 0 and time.time() > expire_at:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return {
                "value": value,
                "created_at": created_at,
                "expires_at": expire_at,
                "ttl": ttl,
            }

    async def cleanup_expired(self) -> int:
        async with self._lock:
            now: float = time.time()
            expired: list[str] = [k for k, v in self._cache.items() if v[1] > 0 and now > v[1]]
            for k in expired:
                del self._cache[k]
            return len(expired)

    @property
    def size(self) -> int:
        return len(self._cache)

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[object]] | Callable[[], object],
        ttl: int = 0,
    ) -> object:
        """获取缓存，如果不存在则调用factory生成并缓存"""
        cached: object = await self.get(key)
        if cached is not _MISSING:
            return cached
        if asyncio.iscoroutinefunction(factory):
            value: object = await factory()
        else:
            value = factory()
        await self.set(key, value, ttl=ttl)
        return value


class SyncTTLCache:
    """线程安全同步缓存：逐键 TTL + LRU 容量上限。

    与 MemoryCache（asyncio.Lock，限事件循环）不同构：本类用 threading.Lock
    与 monotonic 时钟，供同步路径、线程池与测试上下文使用。get 未命中与
    已过期均返回 default；容量满时先清过期条目再按 LRU 淘汰。
    """

    def __init__(self, default_ttl: float = 30.0, max_size: int = 5000) -> None:
        self._default_ttl: float = default_ttl
        self._max_size: int = max_size
        self._data: OrderedDict[Hashable, tuple[object, float]] = OrderedDict()
        self._lock: threading.Lock = threading.Lock()
        self._hits: int = 0
        self._misses: int = 0

    def get(self, key: Hashable, default: object = None) -> object:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return default
            value, expires = item
            if expires <= time.monotonic():
                del self._data[key]
                self._misses += 1
                return default
            self._data.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: Hashable, value: object, ttl: Optional[float] = None) -> None:
        expires = time.monotonic() + (self._default_ttl if ttl is None else ttl)
        with self._lock:
            if key not in self._data and len(self._data) >= self._max_size:
                self._evict_expired_locked()
            self._data[key] = (value, expires)
            self._data.move_to_end(key)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def delete(self, key: Hashable) -> bool:
        with self._lock:
            if key in self._data:
                del self._data[key]
                return True
            return False

    def invalidate(self, prefix: Optional[str] = None) -> int:
        """前缀批量失效；prefix 为 None 时清空全量。返回清除条数。"""
        with self._lock:
            if prefix is None:
                count: int = len(self._data)
                self._data.clear()
                return count
            stale: list[Hashable] = [k for k in self._data if isinstance(k, str) and k.startswith(prefix)]
            for k in stale:
                del self._data[k]
            return len(stale)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def cleanup(self) -> int:
        """主动清扫全部过期条目，返回清除条数。"""
        with self._lock:
            return self._evict_expired_locked()

    def get_or_set(self, key: Hashable, factory: Callable[[], object], ttl: Optional[float] = None) -> object:
        cached: object = self.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        value: object = factory()
        self.set(key, value, ttl=ttl)
        return value

    def size(self) -> int:
        with self._lock:
            return len(self._data)

    @property
    def stats(self) -> dict[str, object]:
        with self._lock:
            total: int = self._hits + self._misses
            return {
                "size": len(self._data),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / total if total > 0 else 0.0,
            }

    def _evict_expired_locked(self) -> int:
        now: float = time.monotonic()
        expired: list[Hashable] = [k for k, (_, e) in self._data.items() if e <= now]
        for k in expired:
            del self._data[k]
        return len(expired)
