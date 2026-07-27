from __future__ import annotations

import time
import uuid
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from nexus.config import NexusConfig, get_settings
from nexus.context import set_request_context
from nexus.logging import get_logger

REQUEST_ID_HEADER: str = "X-Request-ID"


def setup_cors(
    app: FastAPI,
    config: Optional[NexusConfig] = None,
    *,
    origins: Optional[list[str]] = None,
    methods: Optional[list[str]] = None,
    headers: Optional[list[str]] = None,
    credentials: bool = True,
) -> None:
    if origins is not None:
        allow_origins: list[str] = origins
        allow_credentials: bool = credentials
        if "*" in allow_origins and allow_credentials:
            allow_credentials = False
        allow_methods: list[str] = methods or ["*"]
        allow_headers: list[str] = headers or ["*"]
    else:
        cfg: NexusConfig = config or get_settings()
        cors_cfg = cfg.cors
        allow_origins = cors_cfg.allow_origins
        allow_credentials = cors_cfg.allow_credentials
        if "*" in allow_origins and allow_credentials:
            allow_credentials = False
        allow_methods = cors_cfg.allow_methods
        allow_headers = cors_cfg.allow_headers

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=allow_methods,
        allow_headers=allow_headers,
    )


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id: str = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        set_request_context(request_id=request_id)
        response: Response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class NoCacheMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, path_prefix: str = "/static") -> None:
        super().__init__(app)
        self._path_prefix: str = path_prefix

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response: Response = await call_next(request)
        if request.url.path.startswith(self._path_prefix):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        logger = get_logger("nexus.request")
        start_time: float = time.time()
        request_id: str = getattr(request.state, "request_id", "-")

        logger.info(
            "Request started: %s %s [req_id=%s]",
            request.method,
            request.url.path,
            request_id,
        )

        try:
            response: Response = await call_next(request)
            duration_ms: float = (time.time() - start_time) * 1000
            logger.info(
                "Request completed: %s %s -> %d [%.2fms, req_id=%s]",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                request_id,
            )
            return response
        except Exception as exc:
            duration_ms: float = (time.time() - start_time) * 1000
            logger.error(
                "Request failed: %s %s [%.2fms, req_id=%s]: %s",
                request.method,
                request.url.path,
                duration_ms,
                request_id,
                str(exc),
            )
            raise


class NotFoundCheckMiddleware(BaseHTTPMiddleware):
    """路径未匹配时返回统一 JSON 404，避免 FastAPI 默认 HTML 404。

    消除 lion / promptManager / usercenter 三项目重复实现的 NotFoundCheck 逻辑。
    在路由阶段遍历 app.routes 检查路径是否匹配，未匹配则返回 JSONResponse({"detail":"Not Found"})。
    """

    def __init__(
        self,
        app,
        api_prefix: str = "/api",
        exclude_prefixes: Optional[list[str]] = None,
    ) -> None:
        super().__init__(app)
        self._api_prefix: str = api_prefix
        self._exclude_prefixes: list[str] = exclude_prefixes or ["/static", "/health", "/readiness"]

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path: str = request.url.path
        if not path.startswith(self._api_prefix):
            return await call_next(request)
        if any(path.startswith(p) for p in self._exclude_prefixes):
            return await call_next(request)

        from starlette.routing import Match, Mount

        scope = request.scope
        route_found: bool = False
        for route in request.app.routes:
            if isinstance(route, Mount):
                continue
            if hasattr(route, "path") and "{path:path}" in route.path:
                continue
            if not hasattr(route, "matches"):
                continue
            match, _ = route.matches(scope)
            if match in (Match.FULL, Match.PARTIAL):
                route_found = True
                break

        if not route_found:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=404,
                content={"detail": "Not Found", "path": path},
            )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """注入 HTTP 安全响应头，提升 Web 安全防护基线。

    消除 WisePath / fastRPC 各自实现的安全头注入逻辑。
    默认注入：X-Content-Type-Options / X-Frame-Options / X-XSS-Protection /
    Referrer-Policy / Strict-Transport-Security。
    """

    DEFAULT_HEADERS: dict[str, str] = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    }

    def __init__(self, app, headers: Optional[dict[str, str]] = None) -> None:
        super().__init__(app)
        self._headers: dict[str, str] = {**self.DEFAULT_HEADERS, **(headers or {})}

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response: Response = await call_next(request)
        for key, value in self._headers.items():
            if key not in response.headers:
                response.headers[key] = value
        return response
