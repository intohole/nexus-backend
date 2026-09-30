"""路由级限流：端点装饰器与共享滑窗记账。"""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Callable, Optional, Union

from fastapi import Request

from nexus.rate_limit.keys import resolve_client_id
from nexus.rate_limit.window import SlidingWindow

_LIMIT_UNITS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}


def parse_rate_limit(limit: str) -> tuple[int, int]:
    """解析 "20/minute" 风格限流串为 (max_requests, window_seconds)。

    支持 second/minute/hour/day 及复数形式，非法表达式抛 ValueError。
    """
    count_str, _, unit = limit.partition("/")
    unit = unit.strip().lower().rstrip("s")
    try:
        max_requests: int = int(count_str.strip())
    except ValueError as exc:
        raise ValueError(f"invalid rate limit: {limit!r}") from exc
    if unit not in _LIMIT_UNITS:
        raise ValueError(f"invalid rate limit unit: {limit!r}")
    return max_requests, _LIMIT_UNITS[unit]


class RouteRateLimiter:
    """按 (scope, key) 维度的滑动窗口记账，供限流装饰器复用全局实例。"""

    def __init__(self, max_buckets: int = 10000, cleanup_interval: float = 60.0) -> None:
        self._buckets: dict[tuple[str, str], SlidingWindow] = {}
        self._max_buckets: int = max_buckets
        self._cleanup_interval: float = cleanup_interval
        self._last_cleanup: float = 0.0

    async def check(self, scope: str, key: str, max_requests: int, window_seconds: int) -> int:
        self._cleanup()
        window = self._buckets.get((scope, key))
        if window is None or window.window_seconds != window_seconds:
            window = SlidingWindow(max_requests, window_seconds)
            self._buckets[(scope, key)] = window
        if await window.is_allowed():
            return 0
        return window.retry_after()

    def _cleanup(self) -> None:
        now: float = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        if len(self._buckets) < self._max_buckets:
            return
        stale: list[tuple[str, str]] = [k for k, v in self._buckets.items() if v.is_idle(now)]
        for k in stale:
            self._buckets.pop(k, None)


_route_limiter: RouteRateLimiter = RouteRateLimiter()


def _find_request(args: tuple, kwargs: dict) -> Optional[Request]:
    request = kwargs.get("request")
    if isinstance(request, Request):
        return request
    for value in args:
        if isinstance(value, Request):
            return value
    return None


async def _retry_after_async(request: Request, limit_value: str, key_func: Optional[Callable[[Request], str]], scope_name: str) -> int:
    max_requests, window_seconds = parse_rate_limit(limit_value)
    key = resolve_client_id(request, key_func)
    return await _route_limiter.check(scope_name, key, max_requests, window_seconds)


def _retry_after_sync(request: Request, limit_value: str, key_func: Optional[Callable[[Request], str]], scope_name: str) -> int:
    max_requests, window_seconds = parse_rate_limit(limit_value)
    key = resolve_client_id(request, key_func)
    return asyncio.get_event_loop().run_until_complete(
        _route_limiter.check(scope_name, key, max_requests, window_seconds)
    )


def rate_limit(
    limit: Union[str, Callable[[], str]],
    key_func: Optional[Callable[[Request], str]] = None,
    scope: Optional[str] = None,
    message: str = "请求过于频繁，请稍后重试",
) -> Callable[[Callable], Callable]:
    """FastAPI 端点限流装饰器：滑动窗口 + 自定义限流键。

    被装饰端点须声明 request: Request 参数；limit 支持 str 或每次请求
    求值的 Callable（配合动态配置）；key_func 缺省按客户端 IP，可传
    ip:app 等复合键函数；超限抛 429 HTTPException（带 Retry-After）。
    """
    from fastapi import HTTPException

    def decorator(func: Callable) -> Callable:
        scope_name: str = scope or f"{func.__module__}.{func.__qualname__}"

        @functools.wraps(func)
        async def async_wrapper(*args: object, **kwargs: object):
            request = _find_request(args, kwargs)
            if request is None:
                return await func(*args, **kwargs)
            limit_value: str = limit() if callable(limit) else limit
            if not limit_value:
                return await func(*args, **kwargs)
            retry_after: int = await _retry_after_async(request, limit_value, key_func, scope_name)
            if retry_after > 0:
                raise HTTPException(
                    status_code=429,
                    detail=message,
                    headers={"Retry-After": str(retry_after)},
                )
            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args: object, **kwargs: object):
            request = _find_request(args, kwargs)
            if request is None:
                return func(*args, **kwargs)
            limit_value: str = limit() if callable(limit) else limit
            if not limit_value:
                return func(*args, **kwargs)
            retry_after: int = _retry_after_sync(request, limit_value, key_func, scope_name)
            if retry_after > 0:
                raise HTTPException(
                    status_code=429,
                    detail=message,
                    headers={"Retry-After": str(retry_after)},
                )
            return func(*args, **kwargs)

        async_wrapper.__nexus_rate_limited__ = True
        sync_wrapper.__nexus_rate_limited__ = True
        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator
