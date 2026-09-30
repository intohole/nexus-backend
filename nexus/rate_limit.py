"""限流中间件：滑动窗口算法与按 IP/维度限流、路径前缀分档、路由级限流装饰器。"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import defaultdict
from typing import Awaitable, Callable, NamedTuple, Optional, Sequence

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from nexus.config import NexusConfig, get_settings
from nexus.logging import get_logger


class SlidingWindow:
    """异步滑窗计数器（HTTP 层 canonical 实现）。

    平行实现互查：nexus.llm_rate_limiter.LLMRateLimiter（LLM 调用维度）、
    ironman.middleware.rate_limit_mw.SlidingWindowLimiter（ironman 管道内 rpm 单例，
    依赖方向 nexus→ironman 禁止反向收归）。改动计数/窗口语义时三处同步。
    """

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


class PathRule(NamedTuple):
    """路径前缀分档限额：最长前缀优先匹配，未命中任何规则走全局档。"""

    prefix: str
    requests_per_minute: int
    requests_per_hour: int = 0


class LimitInfo(NamedTuple):
    """限流命中信息：limit_response 钩子据此构造自定义 429 响应。"""

    retry_after: int
    limit: int
    remaining: int
    window_seconds: int
    scope: str
    path: str


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        config: Optional[NexusConfig] = None,
        requests_per_minute: Optional[int] = None,
        requests_per_hour: Optional[int] = None,
        exclude_paths: Optional[list[str]] = None,
        key_func: Optional[Callable[[Request], str]] = None,
        route_rules: Optional[Sequence[PathRule]] = None,
        limit_response: Optional[Callable[[Request, LimitInfo], Response]] = None,
        exclude_ips: Optional[Sequence[str]] = None,
    ) -> None:
        """按客户端维度的滑动窗口限流。

        key_func 提供时用其返回值作限流键（支持 ip:app 等复合键），
        缺省按 X-Forwarded-For/客户端 IP；requests_per_hour 传 0 关闭小时窗。
        route_rules 提供路径前缀分档（最长前缀优先），未命中走全局档；
        limit_response 钩子可完全接管 429 响应体（缺省 JSON + 标准头）；
        exclude_ips 提供客户端 IP 前缀豁免（如内网健康检查 "10.100.0."/"127.0.0.1"），
        按 startswith 匹配 XFF 解析结果与直连地址，任一命中即豁免。
        """
        super().__init__(app)
        cfg: NexusConfig = config or get_settings()
        rl_cfg = cfg.rate_limit
        self._rpm: int = (
            requests_per_minute if requests_per_minute is not None else rl_cfg.requests_per_minute
        )
        self._rph: int = (
            requests_per_hour if requests_per_hour is not None else rl_cfg.requests_per_hour
        )
        self._exclude_paths: list[str] = (
            exclude_paths if exclude_paths is not None else rl_cfg.exclude_paths
        )
        self._key_func: Optional[Callable[[Request], str]] = key_func
        self._rules: list[PathRule] = sorted(
            route_rules or [], key=lambda r: len(r.prefix), reverse=True
        )
        self._limit_response = limit_response
        self._exclude_ips: tuple[str, ...] = tuple(exclude_ips or ())
        self._minute_buckets: dict[tuple[str, str], SlidingWindow] = {}
        self._hour_buckets: dict[tuple[str, str], SlidingWindow] = defaultdict(
            lambda: SlidingWindow(self._rph, 3600)
        )
        self._logger = get_logger("nexus.rate_limit")
        self._last_cleanup: float = time.time()
        self._cleanup_interval: int = 300
        self._max_buckets: int = 10000

    def _match_rule(self, path: str) -> Optional[PathRule]:
        for rule in self._rules:
            if path.startswith(rule.prefix):
                return rule
        return None

    def _bucket(self, scope_key: str, client_id: str, max_requests: int) -> SlidingWindow:
        key: tuple[str, str] = (scope_key, client_id)
        window: Optional[SlidingWindow] = self._minute_buckets.get(key)
        if window is None or window.max_requests != max_requests:
            window = SlidingWindow(max_requests, 60)
            self._minute_buckets[key] = window
        return window

    def _get_client_id(self, request: Request) -> str:
        if self._key_func is not None:
            try:
                return self._key_func(request)
            except Exception as exc:
                self._logger.warning("rate limit key_func failed: %s", exc)
                return "unknown"
        return self._client_ip(request)

    @staticmethod
    def _client_ip(request: Request) -> str:
        forwarded: Optional[str] = request.headers.get("X-Forwarded-For")
        if forwarded:
            ips: list[str] = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
            for ip in reversed(ips):
                try:
                    parsed = ipaddress.ip_address(ip)
                    if not parsed.is_private and not parsed.is_loopback:
                        return ip
                except ValueError:
                    continue
            if ips:
                return ips[-1]
        return request.client.host if request.client else "unknown"

    def _is_excluded(self, path: str, request: Request) -> bool:
        for exclude in self._exclude_paths:
            if path.startswith(exclude):
                return True
        if self._exclude_ips:
            candidates = [self._client_ip(request)]
            if request.client and request.client.host:
                candidates.append(request.client.host)
            for candidate in candidates:
                for prefix in self._exclude_ips:
                    if candidate.startswith(prefix):
                        return True
        return False

    @staticmethod
    def _route_has_own_limit(request: Request) -> bool:
        """端点已用 rate_limit 装饰器自带限流时跳过全局窗口（与路由级限额共存不叠加）。"""
        from starlette.routing import Match

        app = request.scope.get("app")
        routes = getattr(app, "routes", None)
        if not routes:
            return False
        for route in routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                handler = getattr(route, "endpoint", None)
                return bool(getattr(handler, "__nexus_rate_limited__", False))
        return False

    def _cleanup_expired(self) -> None:
        now: float = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        if len(self._minute_buckets) < self._max_buckets and len(self._hour_buckets) < self._max_buckets:
            return
        self._last_cleanup = now
        minute_keys: list[tuple[str, str]] = [
            k for k, v in self._minute_buckets.items()
            if not v._timestamps or (now - v._timestamps[-1]) > 120
        ]
        for k in minute_keys:
            self._minute_buckets.pop(k, None)
        hour_keys: list[tuple[str, str]] = [
            k for k, v in self._hour_buckets.items()
            if not v._timestamps or (now - v._timestamps[-1]) > 7200
        ]
        for k in hour_keys:
            self._hour_buckets.pop(k, None)

    def _reject(self, request: Request, info: LimitInfo) -> Response:
        if self._limit_response is not None:
            return self._limit_response(request, info)
        return JSONResponse(
            status_code=429,
            content={
                "code": 429,
                "message": (
                    "Too many requests"
                    if info.scope == "minute"
                    else "Hourly rate limit exceeded"
                ),
                "error_code": "RATE_LIMIT_EXCEEDED",
            },
            headers={
                "Retry-After": str(info.retry_after),
                "X-RateLimit-Limit": str(info.limit),
                "X-RateLimit-Remaining": str(info.remaining),
                "X-RateLimit-Window": str(info.window_seconds),
            },
        )

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        path: str = request.url.path
        if self._is_excluded(path, request):
            return await call_next(request)

        if self._route_has_own_limit(request):
            return await call_next(request)

        client_id: str = self._get_client_id(request)

        self._cleanup_expired()

        rule: Optional[PathRule] = self._match_rule(path)
        scope_key: str = rule.prefix if rule else ""
        rpm: int = rule.requests_per_minute if rule else self._rpm
        rph: int = rule.requests_per_hour if rule else self._rph

        if rpm > 0:
            minute_window: SlidingWindow = self._bucket(scope_key, client_id, rpm)
            if not await minute_window.is_allowed():
                retry_after: int = minute_window.retry_after()
                self._logger.warning(
                    "Rate limit exceeded (minute): %s on %s", client_id, path
                )
                return self._reject(
                    request,
                    LimitInfo(
                        retry_after, rpm, max(0, rpm - minute_window.current_count()),
                        60, "minute", path,
                    ),
                )

        if rph > 0:
            hour_key: tuple[str, str] = (scope_key, client_id)
            hour_window: SlidingWindow = self._hour_buckets[hour_key]
            if not await hour_window.is_allowed():
                retry_after = hour_window.retry_after()
                self._logger.warning(
                    "Rate limit exceeded (hour): %s on %s", client_id, path
                )
                return self._reject(
                    request,
                    LimitInfo(
                        retry_after, rph, max(0, rph - hour_window.current_count()),
                        3600, "hour", path,
                    ),
                )

        return await call_next(request)

from nexus.rate_limit_route import RouteRateLimiter, parse_rate_limit, rate_limit  # noqa: E402

__all__ = [
    "SlidingWindow", "PathRule", "LimitInfo", "RateLimitMiddleware",
    "RouteRateLimiter", "parse_rate_limit", "rate_limit",
]
