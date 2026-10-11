from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus import register_health_endpoints


def _client(checks: dict | None = None) -> TestClient:
    app = FastAPI()
    register_health_endpoints(app, app_name="demo", app_version="1.2.3", checks=checks)
    return TestClient(app)


def test_health_liveness_shape() -> None:
    data = _client().get("/health").json()
    assert data == {"status": "ok", "app": "demo", "version": "1.2.3"}


def test_health_without_version_omits_key() -> None:
    app = FastAPI()
    register_health_endpoints(app, app_name="bare")
    data = TestClient(app).get("/health").json()
    assert data == {"status": "ok", "app": "bare"}


def test_health_without_checks_detailed_empty() -> None:
    data = _client().get("/health/detailed").json()
    assert data["status"] == "ok"
    assert data["checks"] == {}


async def _ok() -> str:
    return "ok"


async def _bad() -> str:
    return "scheduler stopped"


async def _boom() -> str:
    raise RuntimeError("x")


def test_detailed_all_ok() -> None:
    data = _client({"db": _ok, "scheduler": _ok}).get("/health/detailed").json()
    assert data["status"] == "ok"
    assert data["checks"] == {"db": "ok", "scheduler": "ok"}


def test_detailed_degraded_on_non_ok() -> None:
    data = _client({"db": _ok, "scheduler": _bad}).get("/health/detailed").json()
    assert data["status"] == "degraded"
    assert data["checks"]["scheduler"] == "scheduler stopped"


def test_detailed_error_on_raise() -> None:
    data = _client({"db": _boom}).get("/health/detailed").json()
    assert data["status"] == "degraded"
    assert data["checks"] == {"db": "error"}


def test_liveness_unaffected_by_failing_checks() -> None:
    client = _client({"db": _boom})
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["status"] == "ok"
