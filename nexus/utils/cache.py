"""进程内异步缓存：TTL + LRU 容量上限的 MemoryCache。"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

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
