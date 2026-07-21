from __future__ import annotations

import hmac
import os
from typing import Awaitable, Callable, List, Optional, Tuple

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from nexus.logging import get_logger

DEFAULT_WHITELIST_PATHS: Tuple[str, ...] = (
    "/health", "/api/health", "/docs", "/openapi.json", "/redoc", "/",
)

DEFAULT_PUBLIC_API_PREFIXES: Tuple[str, ...] = (
    "/api/auth/login", "/api/auth/register", "/api/auth/refresh",
    "/api/auth/config", "/api/auth/login-page-config", "/api/auth/uc/config",
    "/api/vip/levels", "/api/invite-codes/validate", "/api/discovery",
    "/.well-known",
    "/api/gateway/healthz",
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
        service_token: Optional[str] = None,
    ) -> None:
        super().__init__(app)
        self._whitelist: Tuple[str, ...] = tuple(whitelist_paths) if whitelist_paths else DEFAULT_WHITELIST_PATHS
        self._public_prefixes: Tuple[str, ...] = tuple(public_api_prefixes) if public_api_prefixes else DEFAULT_PUBLIC_API_PREFIXES
        self._static_exts: Tuple[str, ...] = tuple(static_extensions) if static_extensions else DEFAULT_STATIC_EXTENSIONS
        self._allow_bearer: bool = allow_bearer_passthrough
        self._prefix: str = os.environ.get("PATH_PREFIX", "")
        self._service_token: Optional[str] = service_token
        self._logger = get_logger("nexus.service_auth")

    def _get_service_token(self) -> str:
        if self._service_token is not None:
            return self._service_token
        return os.environ.get("SERVICE_TOKEN", "")

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
        return ""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = self._strip_prefix(request.url.path)
        if self._is_public(path):
            return await call_next(request)

        service_token = self._get_service_token()
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
