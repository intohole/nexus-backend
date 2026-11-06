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


def test_detailed_exposes_scheduler_snapshot() -> None:
    """S112 三口径观测位: /health/detailed 必带 scheduler 快照三键(mode/running/jobs)。

    job 断链静默性最强, 注册面全绿≠在跑; 此面生产只读 GET 即可判定 gated 开闸态。
    撤销 boot._scheduler_snapshot 接线本断言必红。
    """
    data = _client({"db": _ok}).get("/health/detailed").json()
    snap = data.get("scheduler")
    assert isinstance(snap, dict), "health/detailed 缺 scheduler 快照(S112 接线丢失)"
    assert set(snap.keys()) >= {"mode", "running", "jobs"}
    assert isinstance(snap["jobs"], list)


def test_scheduler_snapshot_inert_on_uninitialized() -> None:
    """调度器未初始化的轻量应用: 快照 fail-open 返回 inert, 不抛错不判 degraded。"""
    from nexus.boot import _scheduler_snapshot

    snap = _scheduler_snapshot()
    assert snap["mode"] in {"auto", "gated", "off", "inert"}
    assert isinstance(snap["running"], bool)
