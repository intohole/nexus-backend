from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from nexus.metrics_store import MetricsStore

BUCKET_BOUNDS: list[int] = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000]
DEFAULT_FLUSH_INTERVAL_S: int = 300
DEFAULT_DB_PATH: str = os.path.join("data", "nexus_metrics.db")
EXCLUDED_PREFIXES: tuple[str, ...] = ("/static", "/health", "/readiness", "/api/_internal", "/favicon")


def default_db_path() -> str:
    return os.environ.get("NEXUS_METRICS_DB") or DEFAULT_DB_PATH


def bucket_for(latency_ms: float) -> int:
    for bound in BUCKET_BOUNDS:
        if latency_ms <= bound:
            return bound
    return BUCKET_BOUNDS[-1]


def percentile_from_buckets(buckets: dict[str, int], p: float) -> float:
    total: int = sum(buckets.values())
    if total == 0:
        return 0.0
    target: float = p / 100.0 * total
    cumulative: int = 0
    prev_bound: int = 0
    for bound in sorted(BUCKET_BOUNDS):
        count: int = buckets.get(str(bound), 0)
        if count == 0:
            prev_bound = bound
            continue
        if cumulative + count >= target:
            span: int = bound - prev_bound
            inner: float = (target - cumulative) / count
            return float(prev_bound) + span * inner
        cumulative += count
        prev_bound = bound
    return float(BUCKET_BOUNDS[-1])


def aggregate_buckets(rows: list[dict[str, object]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for row in rows:
        buckets: dict[str, int] = row["buckets"]  # type: ignore[assignment]
        for key, count in buckets.items():
            merged[key] = merged.get(key, 0) + int(count)
    return merged


class RequestMetrics:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.flush_interval_s: int = DEFAULT_FLUSH_INTERVAL_S
        self._store: Optional[MetricsStore] = None
        self._db_path: str = db_path or default_db_path()
        self._call_lock: threading.Lock = threading.Lock()
        self._app_name: str = os.environ.get("NEXUS_APP_NAME") or "app"
        self._requests: int = 0
        self._errors: int = 0
        self._total_latency: float = 0.0
        self._buckets: dict[str, int] = {}
        self._flushed_requests: int = 0
        self._flushed_errors: int = 0
        self._flushed_buckets: dict[str, int] = {}
        self._flushed_latency: float = 0.0
        self._last_flush_at: float = 0.0
        self._last_flush_wall: Optional[datetime] = None
        self._restore()

    @property
    def store(self) -> MetricsStore:
        if self._store is None:
            os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
            self._store = MetricsStore(self._db_path)
        return self._store

    def _restore(self) -> None:
        try:
            totals = self.store.load_totals("requests")
        except Exception:
            return
        if not totals:
            return
        self._requests = int(totals.get("requests", 0))
        self._errors = int(totals.get("errors", 0))
        self._total_latency = float(totals.get("total_latency", 0.0))
        self._buckets = {str(k): int(v) for k, v in totals.get("buckets", {}).items()}
        self._sync_flush_baseline()

    def _sync_flush_baseline(self) -> None:
        self._flushed_requests = self._requests
        self._flushed_errors = self._errors
        self._flushed_buckets = dict(self._buckets)
        self._flushed_latency = self._total_latency

    def record(self, method: str, status: int, latency_ms: float) -> None:
        flush_now: bool = False
        with self._call_lock:
            self._requests += 1
            self._total_latency += latency_ms
            if status >= 500:
                self._errors += 1
            key: str = str(bucket_for(latency_ms))
            self._buckets[key] = self._buckets.get(key, 0) + 1
            now: float = time.monotonic()
            if now - self._last_flush_at >= self.flush_interval_s and self._requests > self._flushed_requests:
                flush_now = True
                self._last_flush_at = now
        if flush_now:
            self.flush()

    def flush(self) -> None:
        with self._call_lock:
            delta_requests: int = self._requests - self._flushed_requests
            delta_errors: int = self._errors - self._flushed_errors
            delta_buckets: dict[str, int] = {}
            for key, count in self._buckets.items():
                base: int = self._flushed_buckets.get(key, 0)
                if count > base:
                    delta_buckets[key] = count - base
            window_end: datetime = datetime.now(timezone.utc)
            window_start: Optional[datetime] = self._last_flush_wall
            if window_start is None:
                window_start = window_end - timedelta(seconds=self.flush_interval_s)
            payload: dict[str, object] = {
                "requests": self._requests,
                "errors": self._errors,
                "total_latency": self._total_latency,
                "buckets": self._buckets,
            }
            self._sync_flush_baseline()
            self._last_flush_wall = window_end
        try:
            if delta_requests > 0:
                self.store.save_window(
                    self._app_name,
                    window_start.isoformat(timespec="seconds"),
                    window_end.isoformat(timespec="seconds"),
                    delta_requests,
                    delta_errors,
                    delta_buckets,
                )
            self.store.save_totals("requests", payload)
        except Exception:
            pass

    def snapshot_total(self) -> dict[str, object]:
        with self._call_lock:
            avg: float = self._total_latency / self._requests if self._requests else 0.0
            return {
                "requests": self._requests,
                "errors": self._errors,
                "error_rate": round(self._errors / self._requests, 4) if self._requests else 0.0,
                "avg_ms": round(avg, 2),
                "p50_ms": round(percentile_from_buckets(self._buckets, 50), 1),
                "p95_ms": round(percentile_from_buckets(self._buckets, 95), 1),
            }

    def window(self, hours: int) -> dict[str, object]:
        since: datetime = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows = self.store.read_windows(self._app_name, since.isoformat(timespec="seconds"))
        requests: int = sum(int(r["requests"]) for r in rows)
        errors: int = sum(int(r["errors"]) for r in rows)
        buckets: dict[str, int] = aggregate_buckets(rows)
        return {
            "requests": requests,
            "errors": errors,
            "error_rate": round(errors / requests, 4) if requests else 0.0,
            "p50_ms": round(percentile_from_buckets(buckets, 50), 1),
            "p95_ms": round(percentile_from_buckets(buckets, 95), 1),
            "windows": len(rows),
        }

    def reset_for_test(self) -> None:
        with self._call_lock:
            self._requests = 0
            self._errors = 0
            self._total_latency = 0.0
            self._buckets = {}
            self._sync_flush_baseline()


_instance: Optional[RequestMetrics] = None
_instance_lock: threading.Lock = threading.Lock()


def get_request_metrics() -> RequestMetrics:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = RequestMetrics()
    return _instance
