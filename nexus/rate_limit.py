from __future__ import annotations

import time
from collections import defaultdict
from typing import Awaitable, Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from nexus.config import NexusConfig, get_settings
from nexus.logging import get_logger


class SlidingWindow:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max_requests: int = max_requests
        self._window_seconds: int = window_seconds
        self._timestamps: list[float] = []

    def is_allowed(self) -> bool:
        now: float = time.time()
        cutoff: float = now - self._window_seconds
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self._max_requests:
            return False
        self._timestamps.append(now)
        return True

    def retry_after(self) -> int:
        if not self._timestamps:
            return 0
        now: float = time.time()
        oldest: float = self._timestamps[0]
        return max(1, int(oldest + self._window_seconds - now))


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        config: Optional[NexusConfig] = None,
        requests_per_minute: Optional[int] = None,
        requests_per_hour: Optional[int] = None,
        exclude_paths: Optional[list[str]] = None,
    ) -> None:
        super().__init__(app)
        cfg: NexusConfig = config or get_settings()
        rl_cfg = cfg.rate_limit
        self._rpm: int = requests_per_minute or rl_cfg.requests_per_minute
        self._rph: int = requests_per_hour or rl_cfg.requests_per_hour
        self._exclude_paths: list[str] = exclude_paths or rl_cfg.exclude_paths
        self._minute_buckets: dict[str, SlidingWindow] = defaultdict(
            lambda: SlidingWindow(self._rpm, 60)
        )
        self._hour_buckets: dict[str, SlidingWindow] = defaultdict(
            lambda: SlidingWindow(self._rph, 3600)
        )
        self._logger = get_logger("nexus.rate_limit")

    def _get_client_id(self, request: Request) -> str:
        forwarded: Optional[str] = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _is_excluded(self, path: str) -> bool:
        for exclude in self._exclude_paths:
            if path.startswith(exclude):
                return True
        return False

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        path: str = request.url.path
        if self._is_excluded(path):
            return await call_next(request)

        client_id: str = self._get_client_id(request)

        minute_window: SlidingWindow = self._minute_buckets[client_id]
        if not minute_window.is_allowed():
            retry_after: int = minute_window.retry_after()
            self._logger.warning(
                "Rate limit exceeded (minute): %s on %s", client_id, path
            )
            return JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "message": "Too many requests",
                    "error_code": "RATE_LIMIT_EXCEEDED",
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self._rpm),
                    "X-RateLimit-Window": "60",
                },
            )

        hour_window: SlidingWindow = self._hour_buckets[client_id]
        if not hour_window.is_allowed():
            retry_after = hour_window.retry_after()
            self._logger.warning(
                "Rate limit exceeded (hour): %s on %s", client_id, path
            )
            return JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "message": "Hourly rate limit exceeded",
                    "error_code": "RATE_LIMIT_EXCEEDED",
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self._rph),
                    "X-RateLimit-Window": "3600",
                },
            )

        return await call_next(request)
