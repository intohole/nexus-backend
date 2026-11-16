from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_windows (
    app TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    latency_buckets TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (app, window_start)
);
CREATE TABLE IF NOT EXISTS metric_totals (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


class MetricsStore:
    def __init__(self, db_path: str) -> None:
        self._db_path: str = db_path
        self._lock: threading.Lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    def save_window(
        self,
        app: str,
        window_start: str,
        window_end: str,
        requests: int,
        errors: int,
        buckets: dict[str, int],
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO metric_windows "
                "(app, window_start, window_end, requests, errors, latency_buckets) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (app, window_start, window_end, requests, errors, json.dumps(buckets)),
            )
            self.conn.commit()

    def read_windows(self, app: str, since: str) -> list[dict[str, object]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT window_start, window_end, requests, errors, latency_buckets "
                "FROM metric_windows WHERE app = ? AND window_end > ? "
                "ORDER BY window_start",
                (app, since),
            ).fetchall()
        result: list[dict[str, object]] = []
        for window_start, window_end, requests, errors, raw_buckets in rows:
            result.append({
                "app": app,
                "window_start": window_start,
                "window_end": window_end,
                "requests": int(requests),
                "errors": int(errors),
                "buckets": json.loads(raw_buckets),
            })
        return result

    def save_totals(self, key: str, payload: dict[str, object]) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO metric_totals (key, payload) VALUES (?, ?)",
                (key, json.dumps(payload)),
            )
            self.conn.commit()

    def load_totals(self, key: str) -> Optional[dict[str, object]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT payload FROM metric_totals WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def prune(self, before: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM metric_windows WHERE window_end < ?", (before,)
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
