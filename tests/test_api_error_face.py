"""NexusError→HTTP 出口段守底：error_code/details 经 X-Error-Code 旁路穿透装饰器+handler 两出口。

背景：2026-11-25 DSH×中间件差距评测 P1-差距2——handle_api_errors 装饰器把 NexusError 压成
HTTPException(detail=字符串) 丢 error_code/details，402 落 "HTTP_ERROR" 泛码，
credits_gateway 精心构造的 wallet_url/recovery 引导链两出口全断。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from nexus.api_decorators import handle_api_errors
from nexus.credits import CreditsInsufficientError
from nexus.errors import NexusError
from nexus.middleware_exception import setup_exception_handlers
from nexus.uc_sdk_helper import standard_err


def _app() -> TestClient:
    app = FastAPI()
    setup_exception_handlers(app)

    @app.get("/nexus-err")
    @handle_api_errors("op")
    async def _nexus_err() -> dict[str, str]:
        raise CreditsInsufficientError(
            "积分余额不足",
            details={"wallet_url": "/user-center.html#wallet", "balance": 3},
        )

    @app.get("/bare-402")
    @handle_api_errors("op")
    async def _bare_402() -> dict[str, str]:
        raise HTTPException(status_code=402, detail="payment required")

    @app.get("/plain-400")
    @handle_api_errors("op")
    async def _plain_400() -> dict[str, str]:
        standard_err(message="参数缺失", status_code=400)

    return TestClient(app, raise_server_exceptions=False)


def test_decorated_nexus_error_keeps_error_code_and_details() -> None:
    resp = _app().get("/nexus-err")
    assert resp.status_code == 402
    body = resp.json()
    assert body["error_code"] == "INSUFFICIENT_CREDITS"
    assert body["message"] == "积分余额不足"
    assert body["details"] == {"wallet_url": "/user-center.html#wallet", "balance": 3}
    assert body["trace_id"]


def test_bare_402_falls_back_to_credits_code() -> None:
    resp = _app().get("/bare-402")
    assert resp.status_code == 402
    assert resp.json()["error_code"] == "INSUFFICIENT_CREDITS"


def test_standard_err_without_error_code_unchanged() -> None:
    resp = _app().get("/plain-400")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "BAD_REQUEST"
    assert body["message"] == "参数缺失"
    assert "details" not in body


def test_standard_err_attaches_headers() -> None:
    with pytest.raises(StarletteHTTPException) as exc_info:
        standard_err(
            message="余额不足",
            status_code=402,
            error_code="INSUFFICIENT_CREDITS",
            details={"wallet_url": "/w"},
        )
    assert exc_info.value.headers is not None
    assert exc_info.value.headers["X-Error-Code"] == "INSUFFICIENT_CREDITS"
    assert json.loads(exc_info.value.headers["X-Error-Detail"]) == {"wallet_url": "/w"}


def test_generic_nexus_error_keeps_class_code() -> None:
    app = FastAPI()
    setup_exception_handlers(app)

    @app.get("/generic")
    @handle_api_errors("op")
    async def _generic() -> dict[str, str]:
        raise NexusError("通用失败", status_code=418)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/generic")
    assert resp.status_code == 418
    body = resp.json()
    assert body["error_code"] == "NEXUS_ERROR"
    assert "details" not in body
