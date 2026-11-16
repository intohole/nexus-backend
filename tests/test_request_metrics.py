from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.metrics_store import MetricsStore
from nexus.middleware_metrics import RequestMetricsMiddleware
from nexus.request_metrics import RequestMetrics, get_request_metrics


def test_store_window_roundtrip(tmp_path):
    store = MetricsStore(str(tmp_path / "m.db"))
    store.save_window("app1", "2026-11-17T02:00:00", "2026-11-17T02:05:00", 10, 2, {"50": 8, "250": 4})
    store.save_window("app1", "2026-11-17T02:05:00", "2026-11-17T02:10:00", 5, 1, {"250": 5})
    rows = store.read_windows("app1", "2026-11-17T02:00:00")
    assert len(rows) == 2
    assert rows[0]["requests"] == 10 and rows[0]["errors"] == 2
    assert rows[0]["buckets"] == {"50": 8, "250": 4}


def test_store_totals_roundtrip(tmp_path):
    store = MetricsStore(str(tmp_path / "m.db"))
    assert store.load_totals("llm") is None
    store.save_totals("llm", {"calls": 7, "errors": 1})
    assert store.load_totals("llm") == {"calls": 7, "errors": 1}


def test_store_prune(tmp_path):
    store = MetricsStore(str(tmp_path / "m.db"))
    store.save_window("app1", "2026-11-01T02:00:00", "2026-11-01T02:05:00", 1, 0, {})
    store.save_window("app1", "2026-11-17T02:00:00", "2026-11-17T02:05:00", 2, 0, {})
    store.prune("2026-11-10T00:00:00")
    rows = store.read_windows("app1", "2026-11-01T00:00:00")
    assert len(rows) == 1
    assert rows[0]["requests"] == 2


def test_record_and_snapshot_total(tmp_path):
    m = RequestMetrics(str(tmp_path / "m.db"))
    m.record("GET", 200, 30.0)
    m.record("GET", 500, 120.0)
    m.record("POST", 201, 700.0)
    snap = m.snapshot_total()
    assert snap["requests"] == 3
    assert snap["errors"] == 1
    assert snap["error_rate"] == round(1 / 3, 4)
    assert snap["avg_ms"] == round((30.0 + 120.0 + 700.0) / 3, 2)
    assert 25 <= snap["p50_ms"] <= 250
    assert 500 <= snap["p95_ms"] <= 5000


def test_flush_writes_delta_window(tmp_path):
    m = RequestMetrics(str(tmp_path / "m.db"))
    m.record("GET", 200, 30.0)
    m.flush()
    m.record("GET", 500, 120.0)
    m.record("GET", 200, 60.0)
    m.flush()
    rows = m.store.read_windows("app", "2000-01-01T00:00:00")
    assert len(rows) == 2
    assert rows[0]["requests"] == 1 and rows[0]["errors"] == 0
    assert rows[1]["requests"] == 2 and rows[1]["errors"] == 1
    totals = m.store.load_totals("requests")
    assert totals["requests"] == 3 and totals["errors"] == 1


def test_restart_restore(tmp_path):
    db = str(tmp_path / "m.db")
    first = RequestMetrics(db)
    first.record("GET", 200, 30.0)
    first.record("GET", 500, 60.0)
    first.flush()
    second = RequestMetrics(db)
    snap = second.snapshot_total()
    assert snap["requests"] == 2
    assert snap["errors"] == 1


def test_window_reads_from_store(tmp_path):
    m = RequestMetrics(str(tmp_path / "m.db"))
    m.record("GET", 200, 30.0)
    m.flush()
    w = m.window(hours=24)
    assert w["requests"] >= 1
    assert "error_rate" in w and "p95_ms" in w


def test_singleton_returns_shared():
    a = get_request_metrics()
    b = get_request_metrics()
    assert a is b


def test_middleware_counts_requests():
    m = RequestMetrics()
    m.reset_for_test()
    app = FastAPI()
    app.add_middleware(RequestMetricsMiddleware, metrics=m)

    @app.get("/api/thing")
    async def thing():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("x")

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/api/thing")
    client.get("/boom")
    snap = m.snapshot_total()
    assert snap["requests"] == 2
    assert snap["errors"] == 1


def test_middleware_excludes_internal_paths():
    m = RequestMetrics()
    m.reset_for_test()
    app = FastAPI()
    app.add_middleware(RequestMetricsMiddleware, metrics=m)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/static/x.js")
    async def static_file():
        return {"ok": True}

    client = TestClient(app)
    client.get("/health")
    client.get("/static/x.js")
    assert m.snapshot_total()["requests"] == 0


def test_lazy_flush_interval(tmp_path):
    m = RequestMetrics(str(tmp_path / "m.db"))
    m.flush_interval_s = 0
    m.record("GET", 200, 30.0)
    time.sleep(0.01)
    m.record("GET", 200, 30.0)
    rows = m.store.read_windows("app", "2000-01-01T00:00:00")
    assert len(rows) >= 1
