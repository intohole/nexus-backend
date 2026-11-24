"""setup_middleware stack/flags 同传显影+metrics 降级入 health 守底（S200·r243 差距 13）。

差距 13：stack 分支曾静默吞全部 enable_* flags（同传无任何信号）；
metrics ImportError 曾只 warning 不入 health 面（钉版漂移静默降级）。
修复=stack 与显式 flags 同传 warning+模块级 METRICS_DEGRADED 旗标+
readiness metrics_degraded check（缺失显影 503，sync-deps 对表后应绿）。
"""
from __future__ import annotations

import logging
import sys

import pytest
from fastapi import FastAPI

import nexus.fastapi_setup as fs
from nexus.config import get_settings
from nexus.fastapi_setup import setup_middleware
from nexus.internal_endpoints import setup_health_check
from nexus.utils import HealthRegistry


def test_stack_with_explicit_flags_warns(caplog: pytest.LogCaptureFixture) -> None:
    app = FastAPI()
    with caplog.at_level(logging.WARNING, logger="nexus.setup"):
        setup_middleware(app, get_settings(), stack=["logging"], enable_audit=True)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("enable_audit" in w for w in warnings), warnings


def test_stack_alone_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    app = FastAPI()
    with caplog.at_level(logging.WARNING, logger="nexus.setup"):
        setup_middleware(app, get_settings(), stack=["logging"])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("enable_" in w for w in warnings), warnings


def test_metrics_degraded_flag_on_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "nexus.middleware_metrics", None)
    fs.METRICS_DEGRADED = False

    app = FastAPI()
    setup_middleware(app, get_settings(), enable_metrics=True)

    assert fs.METRICS_DEGRADED is True


def test_metrics_healthy_flag_stays_false(monkeypatch: pytest.MonkeyPatch) -> None:
    fs.METRICS_DEGRADED = False

    app = FastAPI()
    setup_middleware(app, get_settings(), enable_metrics=True)

    assert fs.METRICS_DEGRADED is False


def test_readiness_shows_metrics_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setitem(sys.modules, "nexus.middleware_metrics", None)
    fs.METRICS_DEGRADED = False

    app = FastAPI()
    setup_middleware(app, get_settings(), enable_metrics=True)
    setup_health_check(app, HealthRegistry(), get_settings())

    client = TestClient(app)
    resp = client.get("/readiness")

    assert resp.status_code == 503
    assert resp.json()["checks"]["metrics_degraded"] is False


def test_readiness_metrics_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    fs.METRICS_DEGRADED = False

    app = FastAPI()
    setup_middleware(app, get_settings(), enable_metrics=True)
    setup_health_check(app, HealthRegistry(), get_settings())

    client = TestClient(app)
    resp = client.get("/readiness")

    assert resp.status_code == 200
    assert resp.json()["checks"]["metrics_degraded"] is True
