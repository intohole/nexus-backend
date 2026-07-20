from __future__ import annotations

import hmac
import os
import time
import uuid
from typing import Awaitable, Callable, List, Optional, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError, HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from nexus.config import NexusConfig, get_settings
from nexus.context import set_request_context, get_request_id
from nexus.errors import NexusError
from nexus.logging import get_logger

REQUEST_ID_HEADER: str = "X-Request-ID"


def setup_cors(app: FastAPI, config: Optional[NexusConfig] = None) -> None:
    cfg: NexusConfig = config or get_settings()
    cors_cfg = cfg.cors
    allow_origins: list[str] = cors_cfg.allow_origins
    allow_credentials: bool = cors_cfg.allow_credentials
    if "*" in allow_origins and allow_credentials:
        allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=cors_cfg.allow_methods,
        allow_headers=cors_cfg.allow_headers,
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


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id: str = get_request_id() or getattr(request.state, "request_id", "-")
        logger = get_logger("nexus.error")
        try:
            return await call_next(request)
        except NexusError as exc:
            logger.warning(
                "Business error [req_id=%s]: %s (%s)",
                request_id,
                exc.message,
                exc.error_code,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "code": exc.status_code,
                    "message": exc.message,
                    "error_code": exc.error_code,
                    "trace_id": request_id,
                    "details": exc.details,
                },
            )
        except Exception as exc:
            logger.error(
                "Unhandled exception [req_id=%s]: %s",
                request_id,
                str(exc),
                exc_info=True,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "code": 500,
                    "message": "Internal server error",
                    "trace_id": request_id,
                },
            )


DEFAULT_WHITELIST_PATHS: Tuple[str, ...] = (
    "/health", "/api/health", "/docs", "/openapi.json", "/redoc", "/",
)

DEFAULT_PUBLIC_API_PREFIXES: Tuple[str, ...] = (
    "/api/auth/login", "/api/auth/register", "/api/auth/refresh",
    "/api/auth/config", "/api/auth/login-page-config", "/api/auth/uc/config",
    "/api/vip/levels", "/api/invite-codes/validate", "/api/discovery",
    "/.well-known",
    # P6: 公开网关健康检查端点，供 Prometheus/Uptime Kuma 等监控工具无鉴权访问
    "/api/gateway/healthz",
    # A4: 内部监控端点（metrics/circuit/reload），仅 localhost 访问，无需 service token
    "/api/_internal",
)

DEFAULT_STATIC_EXTENSIONS: Tuple[str, ...] = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".html",
)


class ServiceAuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        whitelist_paths: Optional[List[str]] = None,
        public_api_prefixes: Optional[List[str]] = None,
        static_extensions: Optional[List[str]] = None,
        allow_bearer_passthrough: bool = True,
    ) -> None:
        super().__init__(app)
        self._whitelist: Tuple[str, ...] = tuple(whitelist_paths) if whitelist_paths else DEFAULT_WHITELIST_PATHS
        self._public_prefixes: Tuple[str, ...] = tuple(public_api_prefixes) if public_api_prefixes else DEFAULT_PUBLIC_API_PREFIXES
        self._static_exts: Tuple[str, ...] = tuple(static_extensions) if static_extensions else DEFAULT_STATIC_EXTENSIONS
        self._allow_bearer: bool = allow_bearer_passthrough
        self._prefix: str = os.environ.get("PATH_PREFIX", "")
        self._logger = get_logger("nexus.service_auth")

    def _strip_prefix(self, path: str) -> str:
        if self._prefix and path.startswith(self._prefix):
            return path[len(self._prefix):]
        return path

    def _is_public(self, path: str) -> bool:
        if path in self._whitelist or any(path.startswith(p + "/") for p in self._whitelist):
            return True
        if any(path == p or path.startswith(p + "/") for p in self._public_prefixes):
            return True
        if any(path.endswith(ext) for ext in self._static_exts):
            return True
        return False

    def _extract_token(self, request: Request) -> str:
        header_token = request.headers.get("X-Service-Token", "")
        if header_token:
            return header_token
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        cookie_token = request.cookies.get("service_token", "")
        if cookie_token:
            return cookie_token
        query_token = request.query_params.get("token", "")
        if query_token:
            return query_token
        return ""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = self._strip_prefix(request.url.path)
        if self._is_public(path):
            return await call_next(request)

        service_token = os.environ.get("SERVICE_TOKEN", "")
        if not service_token:
            client_ip = request.client.host if request.client else "unknown"
            self._logger.warning(
                "SERVICE_TOKEN not set, denying non-public request: path=%s method=%s ip=%s",
                path, request.method, client_ip,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Service auth not configured"},
            )

        token = self._extract_token(request)
        if token and hmac.compare_digest(token, service_token):
            return await call_next(request)

        if self._allow_bearer:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        self._logger.warning(
            "Service auth denied: path=%s method=%s ip=%s",
            path, request.method, client_ip,
        )
        return JSONResponse(status_code=401, content={"detail": "Invalid service token"})


def setup_exception_handlers(app: FastAPI) -> None:
    logger = get_logger("nexus.exception")

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id: str = get_request_id() or getattr(request.state, "request_id", "-")
        logger.info(
            "HTTP exception [req_id=%s]: %s %s -> %d",
            request_id, request.method, request.url.path, exc.status_code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.status_code,
                "message": str(exc.detail),
                "trace_id": request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id: str = get_request_id() or getattr(request.state, "request_id", "-")
        logger.warning(
            "Validation error [req_id=%s]: %s %s: %s",
            request_id, request.method, request.url.path, str(exc.errors())[:200],
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
                "message": "Request validation error",
                "trace_id": request_id,
                "errors": exc.errors(),
            },
        )

    @app.exception_handler(ValueError)
    async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        request_id: str = get_request_id() or getattr(request.state, "request_id", "-")
        logger.info(
            "ValueError [req_id=%s]: %s %s -> 400: %s",
            request_id, request.method, request.url.path, str(exc)[:200],
        )
        return JSONResponse(
            status_code=400,
            content={
                "code": 400,
                "message": str(exc),
                "trace_id": request_id,
            },
        )
