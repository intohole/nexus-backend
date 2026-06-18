from __future__ import annotations

import time
import uuid
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from nexus.config import NexusConfig, get_settings
from nexus.context import set_request_context, get_request_id
from nexus.errors import NexusError
from nexus.logging import get_logger


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
        request_id: str = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        set_request_context(request_id=request_id)
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
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
