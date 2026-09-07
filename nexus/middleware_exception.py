from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from nexus.context import get_request_id
from nexus.errors import NexusError
from nexus.logging import get_logger

_NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>页面不存在</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f7fa;color:#1f2d3d;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{text-align:center;padding:48px 32px;background:#fff;border-radius:16px;box-shadow:0 8px 32px rgba(31,45,61,.08);max-width:420px;margin:16px}
.code{font-size:64px;font-weight:700;color:#5b6b7d;line-height:1;margin-bottom:16px}
.tip{font-size:16px;color:#5b6b7d;margin-bottom:24px}
a{display:inline-block;padding:10px 28px;border-radius:8px;background:#2f54a8;color:#fff;text-decoration:none;font-size:14px}
a:hover{background:#27479a}
</style>
</head>
<body>
<div class="card">
  <div class="code">404</div>
  <div class="tip">页面不存在或已下线</div>
  <a href="/">返回首页</a>
</div>
</body>
</html>"""


def _wants_html(request: Request) -> bool:
    accept: str = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


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
                    "message": "服务开小差了，请稍后重试",
                    "error_code": "INTERNAL_ERROR",
                    "trace_id": request_id,
                },
            )


def setup_exception_handlers(app: FastAPI) -> None:
    logger = get_logger("nexus.exception")

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc_handler(request: Request, exc: StarletteHTTPException) -> Response:
        request_id: str = get_request_id() or getattr(request.state, "request_id", "-")
        logger.info(
            "HTTP exception [req_id=%s]: %s %s -> %d",
            request_id, request.method, request.url.path, exc.status_code,
        )
        if exc.status_code == 404 and _wants_html(request):
            return HTMLResponse(content=_NOT_FOUND_HTML, status_code=404)
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
