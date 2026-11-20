from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from nexus.lifecycle import LifecyclePolicy
from nexus.probe_extras import (
    make_backup_freshness_check,
    make_deps_drift_check,
    make_memory_anchor_check,
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


def test_deps_drift_check_all_satisfied(tmp_path: Path) -> None:
    from importlib import metadata

    (tmp_path / "requirements.txt").write_text(
        f"pytest=={metadata.version('pytest')}\n", encoding="utf-8"
    )
    check = make_deps_drift_check(tmp_path)
    out = asyncio.run(check())
    assert out["status"] == "ok"
    assert out["evidence"]["missing"] == []
    assert out["evidence"]["drifted"] == []
    assert out["evidence"]["declared"] == 1


def test_deps_drift_check_missing_and_drifted(tmp_path: Path) -> None:
    from importlib import metadata

    (tmp_path / "requirements.txt").write_text(
        f"zz-no-such-dist-s173==1.0.0\npytest==0.0.1\n", encoding="utf-8"
    )
    check = make_deps_drift_check(tmp_path)
    out = asyncio.run(check())
    assert out["status"] == "fail"
    assert any("zz-no-such-dist-s173" in m for m in out["evidence"]["missing"])
    drifted_line = out["evidence"]["drifted"][0]
    assert drifted_line.startswith("pytest ")
    assert metadata.version("pytest") in drifted_line
    assert "0.0.1" in drifted_line


def test_deps_drift_check_drift_only_degraded(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest==0.0.1\n", encoding="utf-8")
    check = make_deps_drift_check(tmp_path)
    out = asyncio.run(check())
    assert out["status"] == "degraded"
    assert out["evidence"]["missing"] == []


def test_deps_drift_check_no_requirements_skipped(tmp_path: Path) -> None:
    check = make_deps_drift_check(tmp_path)
    out = asyncio.run(check())
    assert out["status"] == "skipped"
    assert out["evidence"]["reason"]
    assert out["detail"]


def _make_anchor_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, created_at TEXT)")
    conn.execute(
        "INSERT INTO memories (content, created_at) VALUES ('a', datetime('now','-1 day'))"
    )
    conn.execute(
        "INSERT INTO memories (content, created_at) VALUES ('b', datetime('now','-30 day'))"
    )
    conn.commit()
    conn.close()


def test_memory_anchor_check_ok(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_anchor_db(db)
    check = make_memory_anchor_check(
        db,
        [
            ("total", "SELECT COUNT(*) FROM memories"),
            ("created_7d", "SELECT COUNT(*) FROM memories WHERE created_at >= datetime('now','-7 days')"),
        ],
    )
    out = asyncio.run(check())
    assert out["status"] == "ok"
    assert out["evidence"]["total"] == 2
    assert out["evidence"]["created_7d"] == 1
    assert out["latency_ms"] >= 0


def test_memory_anchor_check_db_missing_fail(tmp_path: Path) -> None:
    check = make_memory_anchor_check(tmp_path / "ghost.db", [("total", "SELECT COUNT(*) FROM memories")])
    out = asyncio.run(check())
    assert out["status"] == "fail"
    assert "evidence" in out


def test_memory_anchor_check_bad_sql_fail(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_anchor_db(db)
    check = make_memory_anchor_check(db, [("total", "SELECT COUNT(*) FROM no_such_table")])
    out = asyncio.run(check())
    assert out["status"] == "fail"


def test_memory_anchor_check_zero_is_ok(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_anchor_db(db)
    check = make_memory_anchor_check(db, [("empty", "SELECT COUNT(*) FROM memories WHERE id > 999")])
    out = asyncio.run(check())
    assert out["status"] == "ok"
    assert out["evidence"]["empty"] == 0
