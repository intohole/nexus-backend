"""nexus.lifecycle 守底测试（S29）：五护栏逐条断言。"""
from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nexus.lifecycle import (
    MODE_ARCHIVE,
    LifecyclePolicy,
    enforce_lifecycle,
    lifecycle_status,
)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "sandbox.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE usage_log (id INTEGER PRIMARY KEY, created_at TEXT, payload TEXT)"
    )
    now = datetime.now()
    rows = []
    for i in range(30):
        age_days = i
        rows.append((i + 1, (now - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S"), f"row{i}"))
    conn.executemany("INSERT INTO usage_log VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def test_dry_run_zero_side_effect(db: Path) -> None:
    policy = LifecyclePolicy(table="usage_log", ttl_days=7, min_age_hours=0)
    before = _count(db)
    report = enforce_lifecycle(db, [policy], dry_run=True)
    assert report.tables[0].dry_run is True
    assert report.tables[0].deleted == 0
    assert report.tables[0].eligible > 0
    assert _count(db) == before


def test_purge_deletes_with_backup(db: Path, tmp_path: Path) -> None:
    policy = LifecyclePolicy(table="usage_log", ttl_days=7, min_age_hours=0)
    report = enforce_lifecycle(db, [policy], dry_run=False, archive_root=tmp_path)
    tr = report.tables[0]
    assert tr.deleted == tr.eligible > 0
    backup = Path(tr.backup_path)
    assert backup.exists() and backup.stat().st_size > 0
    lines = gzip.open(backup, "rt", encoding="utf-8").read().splitlines()
    assert len(lines) == tr.deleted
    assert json.loads(lines[0])["payload"].startswith("row")
    remaining = _rows(db)
    assert all(r[1] >= (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d") for r in remaining)


def test_min_age_guard_keeps_same_day(db: Path, tmp_path: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO usage_log VALUES (999, ?, 'fresh')",
        ((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.commit()
    conn.close()
    policy = LifecyclePolicy(table="usage_log", ttl_days=7, min_age_hours=24)
    report = enforce_lifecycle(db, [policy], dry_run=False, archive_root=tmp_path)
    tr = report.tables[0]
    kept = {r[0] for r in _rows(db)}
    assert 999 not in kept, "30 天前老行受 min_age 护栏外仍应删——本用例 min_age 只保 24h 内"
    fresh_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO usage_log VALUES (1000, ?, 'today')", (fresh_now,))
    conn.commit()
    conn.close()
    report2 = enforce_lifecycle(db, [policy], dry_run=False, archive_root=tmp_path)
    kept2 = {r[0] for r in _rows(db)}
    assert 1000 in kept2, "今天写入的行即使误配 ttl 也必须被 min_age 护栏保住"
    assert report2.tables[0].skipped_reason == ""


def test_keep_n_per_group(tmp_path: Path) -> None:
    path = tmp_path / "snap.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, symbol TEXT, created_at TEXT)")
    now = datetime.now()
    seq = 0
    for sym in ("600519", "000858"):
        for i in range(10):
            seq += 1
            conn.execute(
                "INSERT INTO snapshots VALUES (?,?,?)",
                (seq, sym, (now - timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S")),
            )
    conn.commit()
    conn.close()
    policy = LifecyclePolicy(
        table="snapshots", ttl_days=0, keep_n=3, group_column="symbol", min_age_hours=0,
    )
    report = enforce_lifecycle(path, [policy], dry_run=False, archive_root=tmp_path)
    assert report.total_deleted == 14
    conn = sqlite3.connect(path)
    per_group = dict(conn.execute("SELECT symbol, COUNT(*) FROM snapshots GROUP BY symbol"))
    conn.close()
    assert per_group == {"600519": 3, "000858": 3}


def test_archive_mode_writes_jsonl(db: Path, tmp_path: Path) -> None:
    policy = LifecyclePolicy(
        table="usage_log", ttl_days=7, min_age_hours=0, mode=MODE_ARCHIVE,
    )
    report = enforce_lifecycle(db, [policy], dry_run=False, archive_root=tmp_path)
    tr = report.tables[0]
    assert tr.mode == MODE_ARCHIVE
    assert Path(tr.backup_path).exists()


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        LifecyclePolicy(table="t", ttl_days=0, keep_n=0)
    with pytest.raises(ValueError):
        LifecyclePolicy(table="t", ttl_days=1, mode="shred")


def test_missing_table_and_columns_skipped(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    report = enforce_lifecycle(path, [LifecyclePolicy(table="ghost", ttl_days=1)], dry_run=False)
    assert report.tables[0].skipped_reason == "table missing"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE odd (x INTEGER)")
    conn.commit()
    conn.close()
    report2 = enforce_lifecycle(path, [LifecyclePolicy(table="odd", ttl_days=1)], dry_run=False)
    assert "columns missing" in report2.tables[0].skipped_reason


def test_lifecycle_status_readonly(db: Path) -> None:
    policy = LifecyclePolicy(table="usage_log", ttl_days=7, min_age_hours=0)
    status = lifecycle_status(db, [policy])
    assert status["usage_log"]["rows"] == 30
    assert status["usage_log"]["overdue"] > 0
    assert status["usage_log"]["oldest"] is not None


def test_epoch_int_timestamp_track(tmp_path: Path) -> None:
    path = tmp_path / "epoch.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER)")
    now_epoch = int(datetime.now().timestamp())
    for i in range(20):
        conn.execute("INSERT INTO events VALUES (?,?)", (i + 1, now_epoch - i * 86400))
    conn.commit()
    conn.close()
    policy = LifecyclePolicy(table="events", ttl_days=5, time_column="ts", min_age_hours=0)
    report = enforce_lifecycle(path, [policy], dry_run=False, archive_root=tmp_path)
    assert report.total_deleted == 15


def _count(db: Path) -> int:
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0]
    conn.close()
    return int(n)


def _rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id, created_at FROM usage_log").fetchall()
    conn.close()
    return rows


def test_extra_where_scopes_deletion(tmp_path: Path) -> None:
    path = tmp_path / "scoped.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, created_at TEXT, channel TEXT)")
    now = datetime.now()
    for i in range(20):
        conn.execute(
            "INSERT INTO logs VALUES (?,?,?)",
            (i + 1, (now - timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S"),
             "e2e" if i % 2 == 0 else "prod"),
        )
    conn.commit()
    conn.close()
    policy = LifecyclePolicy(
        table="logs", ttl_days=5, min_age_hours=0, extra_where="channel = 'e2e'",
    )
    report = enforce_lifecycle(path, [policy], dry_run=False, archive_root=tmp_path)
    conn = sqlite3.connect(path)
    survivors = dict(conn.execute("SELECT channel, COUNT(*) FROM logs GROUP BY channel"))
    conn.close()
    assert report.total_deleted == 7
    assert survivors == {"e2e": 3, "prod": 10}
