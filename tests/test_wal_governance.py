"""WAL 治理原语守底（S159 DE5）：TRUNCATE checkpoint+阈值门+调度接线.

夹具契约：生产面=WAL 活库+连接池常开连接（最后连接干净关闭会删 wal 文件，
测试必须持 holder 常开模拟连接池，否则 wal 不存在=测了个空集）。
"""
import sqlite3
from pathlib import Path

import pytest

from nexus import wal_governance as wg


def _make_wal_db(tmp_path: Path, rows: int = 2000, name: str = "app.db"):
    db = tmp_path / name
    db.parent.mkdir(parents=True, exist_ok=True)
    holder = sqlite3.connect(str(db))
    holder.execute("PRAGMA journal_mode=wal")
    holder.execute("CREATE TABLE t (v TEXT)")
    writer = sqlite3.connect(str(db))
    writer.executemany("INSERT INTO t VALUES (?)", [("x" * 1024,)] * rows)
    writer.commit()
    writer.close()
    return db, holder


def test_truncate_resets_grown_wal(tmp_path: Path) -> None:
    db, holder = _make_wal_db(tmp_path)
    assert (wal := wg.wal_size(db)) is not None and wal > 0
    report = wg.wal_checkpoint_truncate(db)
    assert report["ok"] is True
    assert report["busy"] is False
    assert report["wal_before"] == wal
    assert report["wal_after"] == 0
    holder.close()


def test_truncate_reports_busy_under_active_reader(tmp_path: Path) -> None:
    db, holder = _make_wal_db(tmp_path)
    holder.execute("BEGIN")
    holder.execute("SELECT COUNT(*) FROM t").fetchone()
    report = wg.wal_checkpoint_truncate(db)
    assert report["ok"] is False
    assert report["busy"] is True
    assert report["wal_after"] == report["wal_before"]
    holder.rollback()
    report = wg.wal_checkpoint_truncate(db)
    assert report["ok"] is True
    assert report["wal_after"] == 0
    holder.close()


def test_truncate_non_wal_db_honest_report(tmp_path: Path) -> None:
    db = tmp_path / "plain.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()
    report = wg.wal_checkpoint_truncate(db)
    assert report["ok"] is False
    assert report["journal_mode"] != "wal"
    assert "error" in report


def test_truncate_missing_db_creates_nothing(tmp_path: Path) -> None:
    ghost = tmp_path / "ghost.db"
    report = wg.wal_checkpoint_truncate(ghost)
    assert report["ok"] is False
    assert not ghost.exists()


def test_wal_size_missing_returns_none(tmp_path: Path) -> None:
    assert wg.wal_size(tmp_path / "nope.db") is None
    db, holder = _make_wal_db(tmp_path)
    assert wg.wal_size(db) > 0
    holder.close()


def test_run_governance_threshold_gate(tmp_path: Path) -> None:
    grown, grown_holder = _make_wal_db(tmp_path, name="grown.db")
    small, small_holder = _make_wal_db(tmp_path, rows=1, name="small.db")
    wg.wal_checkpoint_truncate(small)
    report = wg.run_wal_governance([grown, small], min_wal_bytes=64 * 1024)
    by_db = {Path(p["db"]).name: p for p in report["parts"]}
    assert report["ok"] is True
    assert by_db["grown.db"]["ok"] is True
    assert by_db["grown.db"]["wal_after"] == 0
    assert by_db["grown.db"].get("skipped") is None
    assert by_db["small.db"]["skipped"] is True
    assert by_db["small.db"]["reason"] == "below_threshold"
    grown_holder.close()
    small_holder.close()


def test_run_governance_missing_db_part(tmp_path: Path) -> None:
    report = wg.run_wal_governance([tmp_path / "ghost.db"])
    assert report["ok"] is False
    assert report["parts"][0]["error"] == "db not found"


def test_run_governance_single_path(tmp_path: Path) -> None:
    db, holder = _make_wal_db(tmp_path)
    report = wg.run_wal_governance(db, min_wal_bytes=1024)
    assert report["ok"] is True
    assert report["parts"][0]["wal_after"] == 0
    holder.close()


def test_register_wal_checkpoint_job_wires_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            captured["job"] = job
            captured["job_id"] = job_id
            captured["hours"] = hours

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    db, holder = _make_wal_db(tmp_path)
    result = wg.register_wal_checkpoint_job(db, interval_hours=6)
    assert isinstance(result, FakeScheduler)
    assert captured["job_id"] == wg.WAL_JOB_ID
    assert captured["hours"] == 6
    captured["job"]()
    holder.close()


def test_register_job_custom_id_and_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            captured["job_id"] = job_id

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    wg.register_wal_checkpoint_job(
        [tmp_path / "a.db", tmp_path / "b.db"], interval_hours=12, job_id="wal-truemirror"
    )
    assert captured["job_id"] == "wal-truemirror"
