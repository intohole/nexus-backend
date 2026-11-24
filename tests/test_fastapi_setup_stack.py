"""fastapi_setup 装配序守底：r222 判据（Logging 先加、RequestId 后加=外层先设 id）
+ stack 序参数化直替面（七仓克隆族零行为变化）。

prove-red 锚：create_app/setup_middleware 默认路径 Logging 三行 req_id="-"
（RequestId 先加=内层，Logging 外层读 state 时 id 未设）。
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.config import get_settings
from nexus.fastapi_setup import create_app, setup_middleware

MW_CLASSES = {
    "RequestIdMiddleware": "nexus.middleware_base.RequestIdMiddleware",
    "LoggingMiddleware": "nexus.middleware_base.LoggingMiddleware",
    "StaticAssetsCacheMiddleware": "nexus.middleware_base.StaticAssetsCacheMiddleware",
    "ErrorHandlerMiddleware": "nexus.middleware_exception.ErrorHandlerMiddleware",
}


def _mw_names(app: FastAPI) -> list[str]:
    return [m.cls.__name__ for m in app.user_middleware]


def _request_log_records(caplog):
    return [r for r in caplog.records if r.name == "nexus.request"]


@pytest.fixture()
def _quiet_nexus_log(caplog):
    caplog.set_level(logging.INFO, logger="nexus.request")
    yield caplog


def test_create_app_logging_lines_carry_req_id(_quiet_nexus_log):
    app = create_app(title="probe-reqid", config=get_settings())

    @app.get("/ping")
    def ping():
        return {"ok": 1}

    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200
    resp_id = resp.headers.get("X-Request-ID", "")
    assert resp_id
    started = [r for r in _request_log_records(_quiet_nexus_log) if "started" in r.getMessage()]
    assert started, "Request started log missing"
    assert f"[req_id={resp_id}]" in started[0].getMessage(), started[0].getMessage()


def test_setup_middleware_default_logging_lines_carry_req_id(_quiet_nexus_log):
    app = FastAPI()
    setup_middleware(app, get_settings())
    hits = []

    @app.get("/ping")
    def ping():
        hits.append(True)
        return {"ok": 1}

    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200 and hits
    rid = resp.headers.get("X-Request-ID", "")
    assert rid
    started = [r for r in _request_log_records(_quiet_nexus_log) if "started" in r.getMessage()]
    assert started
    assert f"[req_id={rid}]" in started[0].getMessage()


def test_request_id_outer_of_logging_default_paths():
    app1 = create_app(title="probe-order", config=get_settings())
    names = _mw_names(app1)
    assert names.index("RequestIdMiddleware") < names.index("LoggingMiddleware"), names

    app2 = FastAPI()
    setup_middleware(app2, get_settings())
    names2 = _mw_names(app2)
    assert names2.index("RequestIdMiddleware") < names2.index("LoggingMiddleware"), names2


def test_stack_mode_exact_outer_to_inner_order():
    app = FastAPI()
    setup_middleware(app, get_settings(), stack=["no_cache", "request_id", "cors"])
    assert _mw_names(app) == [
        "StaticAssetsCacheMiddleware",
        "RequestIdMiddleware",
        "CORSMiddleware",
    ]


def test_stack_mode_with_error_handler():
    app = FastAPI()
    setup_middleware(app, get_settings(), stack=["no_cache", "error_handler", "request_id", "cors"])
    assert _mw_names(app) == [
        "StaticAssetsCacheMiddleware",
        "ErrorHandlerMiddleware",
        "RequestIdMiddleware",
        "CORSMiddleware",
    ]


def test_stack_mode_with_metrics():
    app = FastAPI()
    setup_middleware(app, get_settings(), stack=["metrics", "request_id", "cors"])
    assert _mw_names(app) == [
        "RequestMetricsMiddleware",
        "RequestIdMiddleware",
        "CORSMiddleware",
    ]


def test_stack_mode_with_service_auth():
    app = FastAPI()
    setup_middleware(app, get_settings(), stack=["service_auth", "request_id"])
    assert _mw_names(app) == [
        "ServiceAuthMiddleware",
        "RequestIdMiddleware",
    ]


def test_stack_mode_metrics_missing_graceful(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "nexus.middleware_metrics", None)
    app = FastAPI()
    setup_middleware(app, get_settings(), stack=["metrics", "request_id", "cors"])
    assert "RequestMetricsMiddleware" not in _mw_names(app)
    assert "RequestIdMiddleware" in _mw_names(app)


def test_stack_mode_registers_exception_handlers():
    app = FastAPI()
    setup_middleware(app, get_settings(), stack=["no_cache", "request_id", "cors", "exception_handlers"])
    assert app.exception_handlers
    assert any(
        getattr(h, "__name__", "").startswith("_http_exc_handler")
        for h in app.exception_handlers.values()
    )


def test_stack_mode_empty_and_unknown_component():
    app = FastAPI()
    setup_middleware(app, get_settings(), stack=[])
    assert _mw_names(app) == []
    with pytest.raises(ValueError):
        setup_middleware(app, get_settings(), stack=["no_such_thing"])
