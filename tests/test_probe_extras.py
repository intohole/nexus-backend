from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from nexus.lifecycle import LifecyclePolicy
from nexus.probe_extras import (
    make_backup_freshness_check,
    classify_jobs,
    jobs_health,
    last_send_outcome,
    make_watermark_check,
    record_send_outcome,
)


def test_send_ledger_roundtrip() -> None:
    record_send_outcome("app-x", ok=True, notify_id=99, detail="sent")
    ledger = last_send_outcome("app-x")
    assert ledger is not None
    assert ledger["notify_id"] == "99"
    assert ledger["ok"] is True
    record_send_outcome("app-x", ok=False, detail="401")
    assert last_send_outcome("app-x")["ok"] is False


def test_classify_jobs_three_states() -> None:
    jobs: dict[str, object] = {
        "ok-job": {"next_run_at": "2026-10-22T08:00:00", "failures": 0},
        "never": {"next_run_at": None},
        "dead": {"next_run_at": "2026-10-22T08:00:00", "failures": 7},
    }
    classes = classify_jobs(jobs)
    assert classes == {"ok-job": "ok", "never": "never_scheduled", "dead": "failing"}
    health = jobs_health(jobs)
    assert health["failing"] == ["dead"]
    assert health["never_scheduled"] == ["never"]
    assert health["no_success_yet"] == ["ok-job", "dead"]


def test_watermark_check_ok_and_overdue(tmp_path: Path) -> None:
    db = tmp_path / "wm.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events (ts TEXT NOT NULL)")
    old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO events (ts) VALUES (?)", (old,))
    conn.commit()
    conn.close()
    policy = LifecyclePolicy(table="events", time_column="ts", ttl_days=7)
    check = make_watermark_check(db, [policy])
    result = asyncio.new_event_loop().run_until_complete(check())
    assert result["status"] == "degraded"
    assert "cleanup overdue: events" in result["detail"]
    assert result["evidence"]["tables"]["events"]["rows"] == 1
    assert result["evidence"]["tables"]["events"]["overdue"] == 1


def test_watermark_check_db_missing(tmp_path: Path) -> None:
    policy = LifecyclePolicy(table="events", time_column="ts", ttl_days=1)
    check = make_watermark_check(tmp_path / "none.db", [policy])
    result = asyncio.new_event_loop().run_until_complete(check())
    assert result["status"] == "fail"


def test_backup_freshness_check_three_states(tmp_path: Path) -> None:
    import os
    import shutil
    import time

    from nexus import backup

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    report = backup.run_backup(src, backup_dir, "app", ["t"])
    assert report["ok"] is True
    check = make_backup_freshness_check(backup_dir, "app", max_age_hours=24.0)
    result = asyncio.new_event_loop().run_until_complete(check())
    assert result["status"] == "ok"
    stale = shutil.copy2(Path(str(report["backup"])), backup_dir / "app_old.db")
    os.utime(stale, (time.time() - 48 * 3600,) * 2)
    Path(str(report["backup"])).unlink()
    result2 = asyncio.new_event_loop().run_until_complete(check())
    assert result2["status"] == "fail"
    assert "age_hours" in result2["detail"] or "stale" in result2["detail"]


def test_backup_freshness_check_no_product(tmp_path: Path) -> None:
    check = make_backup_freshness_check(tmp_path / "backups", "app")
    result = asyncio.new_event_loop().run_until_complete(check())
    assert result["status"] == "fail"
    assert result["evidence"]["error"] == "no backup product"
