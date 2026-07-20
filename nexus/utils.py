from __future__ import annotations

import asyncio
import functools
import hashlib
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable, Optional, TypeVar, cast

import httpx

T = TypeVar("T")

_MISSING: object = object()


class TimeUtils:
    CHINA_TZ: timezone = timezone(timedelta(hours=8))
    UTC_TZ: timezone = timezone.utc

    @classmethod
    def now(cls) -> datetime:
        return datetime.now(cls.CHINA_TZ)

    @classmethod
    def now_utc(cls) -> datetime:
        return datetime.now(cls.UTC_TZ)

    @classmethod
    def to_cn(cls, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=cls.UTC_TZ)
        return dt.astimezone(cls.CHINA_TZ)

    @classmethod
    def to_utc(cls, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=cls.CHINA_TZ)
        return dt.astimezone(cls.UTC_TZ)

    @classmethod
    def to_iso(cls, dt: datetime) -> str:
        return cls.to_cn(dt).isoformat()

    @classmethod
    def from_iso(cls, iso_str: str) -> datetime:
        return datetime.fromisoformat(iso_str)

    @classmethod
    def format(cls, dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        return cls.to_cn(dt).strftime(fmt)

    @classmethod
    def now_naive(cls) -> datetime:
        return cls.now().replace(tzinfo=None)

    @classmethod
    def ensure_naive(cls, dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
        return dt

    @classmethod
    def parse(cls, dt_str: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> datetime:
        dt = datetime.strptime(dt_str, fmt)
        return dt.replace(tzinfo=cls.CHINA_TZ)


def clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    return min(max(value, min_val), max_val)


def safe_float(value: object, default: float = 0.0, min_val: float = -float("inf"), max_val: float = float("inf")) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return default
    if result != result:
        return default
    return clamp(result, min_val, max_val)


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


class MemoryCache:
    def __init__(self, max_size: int = 1000) -> None:
        self._cache: OrderedDict[str, tuple[object, float]] = OrderedDict()
        self._max_size: int = max_size
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get(self, key: str) -> object:
        async with self._lock:
            if key not in self._cache:
                return _MISSING
            value, expire_at = self._cache[key]
            if expire_at > 0 and time.time() > expire_at:
                del self._cache[key]
                return _MISSING
            self._cache.move_to_end(key)
            return value

    async def set(self, key: str, value: object, ttl: int = 0) -> None:
        async with self._lock:
            expire_at: float = time.time() + ttl if ttl > 0 else 0
            self._cache[key] = (value, expire_at)
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
            _, expire_at = self._cache[key]
            if expire_at > 0 and time.time() > expire_at:
                del self._cache[key]
                return False
            return True

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


_cache: MemoryCache = MemoryCache()


def cached(ttl: int = 300) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> T:
            key_parts: list[str] = [func.__name__]
            for arg in args:
                key_parts.append(str(arg))
            for k, v in sorted(kwargs.items()):
                key_parts.append(f"{k}={v}")
            key: str = hashlib.md5("|".join(key_parts).encode()).hexdigest()

            cached_result: object = await _cache.get(key)
            if cached_result is not _MISSING:
                return cast(T, cached_result)

            result = await func(*args, **kwargs)
            await _cache.set(key, result, ttl)
            return result

        return wrapper

    return decorator


class HttpClient:
    def __init__(
        self,
        base_url: str = "",
        timeout: float = 30.0,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self._base_url: str = base_url
        self._timeout: float = timeout
        self._headers: dict[str, str] = headers or {}
        self._client: Optional[httpx.AsyncClient] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def init(self) -> None:
        if self._client is not None:
            return
        async with self._lock:
            if self._client is not None:
                return
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=self._headers,
            )

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> httpx.Response:
        if self._client is None:
            await self.init()
        if self._client is None:
            raise RuntimeError("HttpClient initialization failed.")
        return await self._client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, dot / (norm_a * norm_b))


def batch_cosine_similarity(query: list[float], candidates: list[list[float]]) -> list[float]:
    if not candidates:
        return []
    return [cosine_similarity(query, c) for c in candidates]


class HealthRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, Callable[[], Awaitable[bool]]] = {}

    def register(self, name: str, check_func: Callable[[], Awaitable[bool]]) -> None:
        self._checks[name] = check_func

    async def _run_check(
        self, name: str, check_func: Callable[[], Awaitable[bool]]
    ) -> tuple[str, bool]:
        try:
            result: bool = await check_func()
            return name, result
        except Exception:
            return name, False

    async def run_all(self) -> dict[str, bool]:
        if not self._checks:
            return {}
        tasks: list[Awaitable[tuple[str, bool]]] = [
            self._run_check(name, func)
            for name, func in self._checks.items()
        ]
        resultsList: list[tuple[str, bool]] = await asyncio.gather(*tasks)
        return dict(resultsList)

    async def is_healthy(self) -> bool:
        results: dict[str, bool] = await self.run_all()
        return all(results.values())


def paginate_from_skip(items: list[object], skip: int, limit: int) -> dict[str, object]:
    """从skip/limit参数生成分页响应"""
    total: int = len(items)
    page_items: list[object] = items[skip:skip + limit]
    return {
        "items": page_items,
        "total": total,
        "skip": skip,
        "limit": limit,
        "has_more": skip + limit < total,
    }
