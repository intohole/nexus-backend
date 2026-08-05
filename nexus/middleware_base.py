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


class StaticAssetsCacheMiddleware(BaseHTTPMiddleware):
    """静态资源分级缓存，替代旧的强制 no-store 策略。

    修复前端二次加载每次都全量下载的性能问题：
    - *.html  -> no-cache + ETag 每次强校验(304 只传头)
    - immutable 标记路径(nexus-ui 自托管/带版本号) -> immutable, max-age=1年
    - 其余 js/css/img -> public, max-age=1天 + ETag 增量校验
    """

    def __init__(
        self,
        app,
        path_prefix: str = "/static",
        immutable_age: int = 31536000,
        asset_age: int = 86400,
        immutable_markers: Optional[tuple[str, ...]] = None,
    ) -> None:
        super().__init__(app)
        self._path_prefix: str = path_prefix
        self._immutable_age: int = immutable_age
        self._asset_age: int = asset_age
        self._immutable_markers: tuple[str, ...] = immutable_markers or (
            "/nexus-ui/",
            "/vendor/",
            "/lib/",
        )

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response: Response = await call_next(request)
        ctype: str = response.headers.get("content-type", "")
        if "text/html" in ctype:
            for header in ("Pragma", "Expires"):
                if header in response.headers:
                    del response.headers[header]
            response.headers["Cache-Control"] = "no-cache"
            return response
        if not request.url.path.startswith(self._path_prefix):
            return response
        for header in ("Pragma", "Expires"):
            if header in response.headers:
                del response.headers[header]
        if any(m in request.url.path for m in self._immutable_markers):
            response.headers["Cache-Control"] = (
                f"public, max-age={self._immutable_age}, immutable"
            )
        else:
            response.headers["Cache-Control"] = f"public, max-age={self._asset_age}"
        return response


class NoCacheMiddleware(StaticAssetsCacheMiddleware):
    """兼容旧名：统一走分级缓存策略，不再强制 no-store。"""

    def __init__(self, app, path_prefix: str = "/static") -> None:
        super().__init__(app, path_prefix=path_prefix)


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
