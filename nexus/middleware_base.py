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
