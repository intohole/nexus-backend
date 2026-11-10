"""backup 原语单元测试（r190 收归：S107 五护栏单源）."""
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


def test_run_backup_creates_verified_copy(src_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    report = backup.run_backup(src_db, backup_dir, "gz", ["activities"], keep=10)
    assert report["ok"] is True
    assert report["integrity"] == "ok"
    target = Path(str(report["backup"]))
    assert target.name.startswith("gz_")
    assert backup.table_count(src_db, "activities") == 5
    assert backup.table_count(target, "activities") == 5
    assert report["pruned"] == []


def test_run_backup_missing_db_returns_error(tmp_path: Path) -> None:
    report = backup.run_backup(tmp_path / "nope.db", tmp_path / "backups", "gz", ["t"], keep=10)
    assert report["ok"] is False
    assert "db not found" in str(report.get("error", ""))


def test_run_backup_empty_file_removed(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup, "_vacuum_into", lambda source, target: target.write_bytes(b""))
    report = backup.run_backup(src_db, backup_dir, "gz", ["activities"], keep=10)
    assert report["ok"] is False
    assert "empty" in str(report.get("error", ""))
    assert not any(backup_dir.glob("gz_*.db"))


def test_run_backup_integrity_failure_removed(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "_verify_sqlite_integrity", lambda db: False)
    report = backup.run_backup(src_db, tmp_path / "backups", "gz", ["activities"], keep=10)
    assert report["ok"] is False
    assert "integrity" in str(report.get("error", ""))


def test_run_backup_table_mismatch_removed(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "_table_count", lambda db, table: 0 if str(db).endswith(".db") and "backups" in str(db) else 5)
    report = backup.run_backup(src_db, tmp_path / "backups", "gz", ["activities"], keep=10)
    assert report["ok"] is False


def test_run_backup_collision_reported(src_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True)
    existing = backup_dir / "gz_20260101_000000.db"
    existing.write_bytes(b"x")
    monkey_stamp = "20260101_000000"
    real = backup._stamp
    monkey = lambda: monkey_stamp  # noqa: E731
    backup._stamp = monkey
    try:
        report = backup.run_backup(src_db, backup_dir, "gz", ["activities"], keep=10)
    finally:
        backup._stamp = real
    assert report["ok"] is False
    assert report.get("error") == "collision"
    assert existing.read_bytes() == b"x"


def test_prune_keeps_newest_n(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for i in range(12):
        (backup_dir / f"gz_20260101_0000{i:02d}.db").write_bytes(b"x")
    newest = backup_dir / "gz_20260202_020202.db"
    newest.write_bytes(b"x")
    pruned = backup.prune_backups(backup_dir, "gz", keep=10)
    remaining = sorted(p.name for p in backup_dir.glob("gz_*.db"))
    assert len(remaining) == 10
    assert remaining[-1] == newest.name
    assert len(pruned) == 3


def test_prune_whitelist_never_touches_foreign_files(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    foreign = backup_dir / "important_notes.txt"
    foreign.write_text("keep me")
    stranger = backup_dir / "other_engine_20260101.db"
    stranger.write_bytes(b"y")
    backup.prune_backups(backup_dir, "gz", keep=0)
    assert foreign.exists()
    assert stranger.exists()


def test_run_backup_multi_db_parts(src_db: Path, tmp_path: Path) -> None:
    second = tmp_path / "second.db"
    conn = sqlite3.connect(str(second))
    conn.execute("CREATE TABLE negotiations (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO negotiations VALUES (1)")
    conn.commit()
    conn.close()
    backup_dir = tmp_path / "backups"
    report = backup.run_backup([src_db, second], backup_dir, "gf", ["activities", "negotiations"], keep=10)
    assert report["ok"] is True
    assert len(report["parts"]) == 2
    names = sorted(Path(p["backup"]).name for p in report["parts"])
    assert names[0].startswith("gf_second_") or names[1].startswith("gf_second_")
    assert any(n.startswith("gf_src_") for n in names)
    backup.prune_backups(backup_dir, "gf", keep=10)


def test_register_backup_job_wires_scheduler(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            captured["job"] = job
            captured["job_id"] = job_id
            captured["hours"] = hours

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    result = backup.register_backup_job(
        src_db, tmp_path / "backups", "gz", ["activities"], keep=5, interval_hours=6
    )
    assert isinstance(result, FakeScheduler)
    assert captured["job_id"] == "db-continuous-backup"
    assert captured["hours"] == 6
    captured["job"]()


def test_backup_cli_dry_run_and_execute(src_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc = backup.backup_cli(
        lambda: src_db, lambda: tmp_path / "backups", lambda: "gz",
        ["activities"], keep=10, interval_hours=6, argv=["--dry-run", "--json"],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert '"dry_run": true' in out
    assert "src.db" in out
    rc = backup.backup_cli(
        lambda: src_db, lambda: tmp_path / "backups", lambda: "gz",
        ["activities"], keep=10, interval_hours=6, argv=["--execute", "--json"],
    )
    assert rc == 0
    assert '"ok": true' in capsys.readouterr().out


def test_backup_cli_execute_failure_exit_one(tmp_path: Path) -> None:
    rc = backup.backup_cli(
        lambda: tmp_path / "missing.db", lambda: tmp_path / "backups", lambda: "gz",
        ["t"], keep=10, interval_hours=6, argv=["--execute", "--json"],
    )
    assert rc == 1
