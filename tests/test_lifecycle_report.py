"""nexus.lifecycle_report 守底测试：接线单源的留痕契约与 URL 解析。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from nexus.lifecycle import LifecyclePolicy
from nexus.lifecycle_report import run_lifecycle_report, sqlite_file_from_url


def _make_db(tmp_path: Path) -> Path:
    path = tmp_path / "sandbox.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE usage_log (id INTEGER PRIMARY KEY, created_at TEXT, payload TEXT)"
    )
    now = datetime.now()
    rows = [
        (i + 1, (now - timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S"), f"row{i}")
        for i in range(30)
    ]
    conn.executemany("INSERT INTO usage_log VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def test_sqlite_file_from_url_forms() -> None:
    assert sqlite_file_from_url("sqlite:////abs/path/app.db") == "/abs/path/app.db"
    assert sqlite_file_from_url("sqlite:///data/app.db") == "data/app.db"
    assert sqlite_file_from_url("sqlite:///C:/x/app.db") == "C:/x/app.db"
    assert sqlite_file_from_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_report_dry_run_returns_dict_without_delete(tmp_path: Path) -> None:
    path = _make_db(tmp_path)
    policy = LifecyclePolicy(table="usage_log", ttl_days=7, min_age_hours=0)
    out = run_lifecycle_report(path, [policy], dry_run=True)
    assert out["total_deleted"] == 0
    assert out["ok"] is True
    assert out["tables"][0]["dry_run"] is True
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0] == 30
    conn.close()


def test_report_deletes_and_logs_info(tmp_path: Path) -> None:
    path = _make_db(tmp_path)
    policy = LifecyclePolicy(table="usage_log", ttl_days=7, min_age_hours=0)
    logger = logging.getLogger("test.lifecycle_report")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    out = run_lifecycle_report(path, [policy], label="使用日志清理",
                               logger=logger, dry_run=False)
    assert out["total_deleted"] > 0
    assert any("[lifecycle] 使用日志清理" in r.getMessage() for r in records)


def test_report_missing_db_logs_warning(tmp_path: Path) -> None:
    logger = logging.getLogger("test.lifecycle_report.missing")
    logger.propagate = False
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    out = run_lifecycle_report(tmp_path / "nope.db", [], logger=logger)
    assert out["ok"] is False
    assert any("存在跳过项" in r.getMessage() for r in records)
