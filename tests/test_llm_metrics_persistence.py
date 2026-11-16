from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.internal_endpoints import register_internal_endpoints
from nexus.llm_metrics import get_llm_metrics


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_METRICS_DB", str(tmp_path / "metrics.db"))
    get_llm_metrics().reset()
    get_llm_metrics().configure_store(str(tmp_path / "metrics.db"))
    return get_llm_metrics()


def test_llm_metrics_survives_restart(tmp_path, monkeypatch):
    m = _fresh(tmp_path, monkeypatch)
    m.record("appA", "modelX", 0.5, tokens=100, error=None, cost_usd=0.01)
    m.record("appA", "modelX", 0.25, tokens=50, error="TimeoutError", cost_usd=0.0)
    m.flush_to_store()

    m2 = _fresh(tmp_path, monkeypatch)
    snap = m2.snapshot()
    assert snap["total_calls"] == 2
    assert snap["total_errors"] == 1
    assert snap["total_tokens"] == 150
    assert snap["by_app"]["appA"]["calls"] == 2
    assert snap["by_model"]["modelX"]["errors"] == 1


def test_llm_metrics_snapshot_contract_unchanged(tmp_path, monkeypatch):
    m = _fresh(tmp_path, monkeypatch)
    m.record("appA", "modelX", 0.5, tokens=100, cost_usd=0.01, cached_tokens=40)
    snap = m.snapshot()
    for key in (
        "total_calls", "total_errors", "total_tokens", "total_cost_usd",
        "cached_tokens", "cache_hit_rate", "avg_latency_ms", "error_rate",
        "by_app", "by_model", "by_status",
    ):
        assert key in snap
    assert snap["cache_hit_rate"] == 0.4


def test_internal_metrics_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_METRICS_DB", str(tmp_path / "metrics.db"))
    get_llm_metrics().reset()
    get_llm_metrics().configure_store(str(tmp_path / "metrics.db"))

    app = FastAPI()
    register_internal_endpoints(app)
    client = TestClient(app)

    resp = client.get("/api/_internal/metrics")
    assert resp.status_code == 401

    client.headers.update({"X-Service-Token": "test-token"})
    monkeypatch.setenv("SERVICE_TOKEN", "test-token")
    resp = client.get("/api/_internal/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "app" in data
    assert "requests" in data
    assert {"window_1h", "window_24h", "total"} <= set(data["requests"])
    assert "llm" in data
