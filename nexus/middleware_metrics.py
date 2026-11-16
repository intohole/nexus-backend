from __future__ import annotations

import time
from typing import Awaitable, Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from nexus.request_metrics import EXCLUDED_PREFIXES, RequestMetrics, get_request_metrics


class RequestMetricsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, metrics: Optional[RequestMetrics] = None) -> None:
        super().__init__(app)
        self._metrics: RequestMetrics = metrics or get_request_metrics()

    def _is_excluded(self, path: str) -> bool:
        for prefix in EXCLUDED_PREFIXES:
            if path == prefix or path.startswith(prefix):
                return True
        return False

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._is_excluded(request.url.path):
            return await call_next(request)
        start: float = time.monotonic()
        try:
            response: Response = await call_next(request)
            status_code: int = response.status_code
        except Exception:
            status_code = 500
            self._record_safe(request.method, status_code, (time.monotonic() - start) * 1000.0)
            raise
        self._record_safe(request.method, status_code, (time.monotonic() - start) * 1000.0)
        return response

    def _record_safe(self, method: str, status_code: int, latency_ms: float) -> None:
        try:
            self._metrics.record(method, status_code, latency_ms)
        except Exception:
            pass
