"""BackupShim 装配器守底（r228 收归）：配置面三件 + 行为单源透传."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.backup_shim import BackupShim


@pytest.fixture()
def src_db(tmp_path: Path) -> Path:
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE activities (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO activities VALUES (?, ?)", [(i, f"u{i}") for i in range(5)])
    conn.commit()
    conn.close()
    return src


def _make_shim(db: Path, tmp: Path, **kwargs) -> BackupShim:
    defaults: dict = {
        "db_paths": db,
        "prefix": "ap",
        "verify_tables": ["activities"],
        "keep": 10,
        "interval_hours": 6,
    }
    defaults.update(kwargs)
    return BackupShim(**defaults)


def test_create_backup_creates_verified_copy(src_db: Path, tmp_path: Path) -> None:
    shim = _make_shim(src_db, tmp_path)
    report = shim.create_backup()
    assert report["ok"] is True, report
    target = Path(str(report["backup"]))
    assert target.name.startswith("ap_")
    assert target.parent == src_db.parent / "backups"


def test_create_backup_missing_db_returns_error(tmp_path: Path) -> None:
    shim = _make_shim(tmp_path / "ghost.db", tmp_path)
    report = shim.create_backup()
    assert report["ok"] is False
    assert "db not found" in str(report.get("error", ""))


def test_multi_db_prefix_per_part(src_db: Path, tmp_path: Path) -> None:
    second = tmp_path / "second.db"
    conn = sqlite3.connect(str(second))
    conn.execute("CREATE TABLE activities (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    shim = BackupShim(
        db_paths=[src_db, second], prefix=["ap", "sc"], verify_tables=["activities"], keep=10
    )
    report = shim.create_backup()
    assert report["ok"] is True, report
    parts = report["parts"]
    assert [Path(p["backup"]).name.split("_")[0] for p in parts] == ["ap", "sc"]


def test_lazy_db_path_callable_resolved_at_call(src_db: Path, tmp_path: Path) -> None:
    holder: dict = {"db": src_db}
    shim = BackupShim(
        db_paths=lambda: holder["db"], prefix="ap", verify_tables=["activities"], keep=10
    )
    assert shim.create_backup()["ok"] is True
    holder["db"] = tmp_path / "ghost.db"
    assert shim.create_backup()["ok"] is False


def test_cleanup_old_backups_uses_first_prefix(src_db: Path, tmp_path: Path) -> None:
    backup_dir = src_db.parent / "backups"
    backup_dir.mkdir(parents=True)
    for stamp in ("20260101_000000", "20260102_000000", "20260103_000000"):
        (backup_dir / f"ap_{stamp}.db").write_bytes(b"x")
    shim = _make_shim(src_db, tmp_path, keep=2)
    removed = shim.cleanup_old_backups()
    assert len(removed) == 1
    assert sorted(p.name for p in backup_dir.glob("ap_*.db")) == ["ap_20260102_000000.db", "ap_20260103_000000.db"]


def test_cleanup_max_age_only_applied_when_declared(src_db: Path, tmp_path: Path) -> None:
    backup_dir = src_db.parent / "backups"
    backup_dir.mkdir(parents=True)
    for stamp in ("20260101_000000", "20260102_000000", "20260103_000000", "20260104_000000"):
        (backup_dir / f"ap_{stamp}.db").write_bytes(b"x")
    beyond_keep = backup_dir / "ap_20260101_000000.db"
    plain = _make_shim(src_db, tmp_path, keep=3)
    plain.cleanup_old_backups()
    assert not beyond_keep.exists()
    beyond_keep.write_bytes(b"x")
    aged = _make_shim(src_db, tmp_path, keep=3, cleanup_max_age_days=30)
    aged.cleanup_old_backups()
    assert beyond_keep.exists()


def test_register_backup_wires_scheduler(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            captured["job"] = job
            captured["job_id"] = job_id
            captured["hours"] = hours

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    shim = _make_shim(src_db, tmp_path)
    result = shim.register_backup()
    assert isinstance(result, FakeScheduler)
    assert captured["job_id"] == "db-continuous-backup"
    assert captured["hours"] == 6
    captured["job"]()


def test_register_drill_wires_job(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            captured["job_id"] = job_id
            captured["hours"] = hours

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    shim = _make_shim(src_db, tmp_path)
    shim.register_drill()
    assert captured["job_id"] == "restore-drill"
    assert captured["hours"] == 24


def test_register_wal_checkpoint_wires_job(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            captured["job_id"] = job_id
            captured["hours"] = hours

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    shim = _make_shim(src_db, tmp_path)
    shim.register_wal_checkpoint(interval_hours=6)
    assert captured["job_id"] == "wal-governance"
    assert captured["hours"] == 6


def test_main_cli_dry_run(src_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    shim = _make_shim(src_db, tmp_path)
    code = shim.main(["--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert str(src_db) in out
