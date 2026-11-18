"""register_backup_job 注册即首跑守底（S169 F1：重启重置计时死循环根治）."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus import backup


@pytest.fixture()
def src_db(tmp_path: Path) -> Path:
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE activities (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO activities VALUES (?, ?)", [(i, f"u{i}") for i in range(5)])
    conn.commit()
    conn.close()
    return src


def test_register_backup_job_runs_first_backup_immediately(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            return None

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    backup_dir = tmp_path / "backups"
    backup.register_backup_job(src_db, backup_dir, "gz", ["activities"], keep=5, interval_hours=6)
    products = sorted(backup_dir.glob("gz_*.db"))
    assert len(products) == 1
    conn = sqlite3.connect(str(products[0]))
    count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    conn.close()
    assert count == 5


def test_register_backup_job_first_run_failure_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            return None

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    result = backup.register_backup_job(
        tmp_path / "missing.db", tmp_path / "backups", "gz", ["activities"], keep=5, interval_hours=6
    )
    assert isinstance(result, FakeScheduler)
