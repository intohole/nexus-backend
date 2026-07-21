from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from nexus.context import get_request_id
from nexus.errors import NexusError
from nexus.logging import get_logger


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
