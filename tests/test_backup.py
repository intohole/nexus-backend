"""backup 原语单元测试（r190 收归：S107 五护栏单源）."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from nexus import backup
from nexus import backup_drill


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


def test_register_backup_job_custom_job_id(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            captured["job_id"] = job_id

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    backup.register_backup_job(
        src_db, tmp_path / "backups", "mb", ["activities"], keep=5, interval_hours=6, job_id="memoria_backup"
    )
    assert captured["job_id"] == "memoria_backup"


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


def test_vacuum_falls_back_to_backup_api_on_old_sqlite(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "backups" / "gz_x.db"
    target.parent.mkdir()
    monkeypatch.setattr(backup.sqlite3, "sqlite_version_info", (3, 26, 0))
    calls: dict = {}

    class SrcConn:
        def execute(self, sql: str):
            calls["sql"] = sql
            raise sqlite3.OperationalError('near "INTO": syntax error')

        def backup(self, dst, **kw):
            calls["backup_api"] = True
            target.write_bytes(src_db.read_bytes())
            dst.commit()

        def close(self) -> None:
            pass

    class DstConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            self.commit()

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

    def fake_connect(path, *a, **kw):
        if str(path) == str(src_db):
            return SrcConn()
        return DstConn()

    monkeypatch.setattr(backup.sqlite3, "connect", fake_connect)
    backup._vacuum_into(src_db, target)
    assert calls.get("backup_api") is True
    assert target.stat().st_size > 0


def test_vacuum_raises_through_on_new_sqlite_syntax_error(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup.sqlite3, "sqlite_version_info", (3, 40, 0))
    called: dict = {}

    class SrcConn:
        def execute(self, sql: str):
            called["executed"] = True
            raise sqlite3.OperationalError("near INTO: syntax error")

        def close(self) -> None:
            pass

    monkeypatch.setattr(backup.sqlite3, "connect", lambda path, *a, **kw: SrcConn())
    with pytest.raises(sqlite3.OperationalError):
        backup._vacuum_into(src_db, tmp_path / "backups" / "gz_x.db")
    assert called.get("executed") is True


def _make_backup(src: Path, backup_dir: Path, prefix: str = "app") -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    report = backup.run_backup(src, backup_dir, prefix, ["activities"])
    assert report["ok"] is True, report
    return Path(str(report["backup"]))


def test_restore_drill_green_path(src_db: Path, tmp_path: Path) -> None:
    bk = _make_backup(src_db, tmp_path / "backups")
    report = backup_drill.restore_drill(bk, source_db=src_db, verify_tables=["activities"])
    assert report["ok"] is True, report
    assert report["integrity"] == "ok"
    assert report["table_checks"][0]["backup_rows"] == 5
    assert report["table_checks"][0]["source_rows"] == 5
    assert report["table_checks"][0]["backup_max_id"] == 4
    assert "restored_to" in report
    assert not Path(str(report["restored_to"])).exists()


def test_restore_drill_without_source(src_db: Path, tmp_path: Path) -> None:
    bk = _make_backup(src_db, tmp_path / "backups")
    report = backup_drill.restore_drill(bk, verify_tables=["activities"])
    assert report["ok"] is True, report
    assert report["table_checks"][0]["source_rows"] is None
    assert report["table_checks"][0]["source_max_id"] is None


def test_restore_drill_uuid_id_table_not_fatal(tmp_path: Path) -> None:
    """WisePath 判例：TEXT UUID 主键表 MAX(id) 不可 int 化——演练照常 ok，max_id 如实 None。"""
    src = tmp_path / "uuid.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE activities (id TEXT PRIMARY KEY, name TEXT)")
    conn.executemany(
        "INSERT INTO activities VALUES (?, ?)",
        [
            ("fd15a329-0682-43c7-9a53-62682b4a2816", "u1"),
            ("0b2e6d05-1111-4111-8111-111111111111", "u2"),
        ],
    )
    conn.commit()
    conn.close()
    bk = _make_backup(src, tmp_path / "backups")
    report = backup_drill.restore_drill(bk, source_db=src, verify_tables=["activities"])
    assert report["ok"] is True, report
    assert report["table_checks"][0]["backup_rows"] == 2
    assert report["table_checks"][0]["backup_max_id"] is None


def test_restore_drill_missing_backup(tmp_path: Path) -> None:
    report = backup_drill.restore_drill(tmp_path / "backups" / "app_missing.db", verify_tables=["t"])
    assert report["ok"] is False
    assert report["error"] == "backup not found"


def test_restore_drill_corrupt_backup(src_db: Path, tmp_path: Path) -> None:
    bk = _make_backup(src_db, tmp_path / "backups")
    bk.write_bytes(b"garbage not a sqlite file" * 8)
    report = backup_drill.restore_drill(bk, verify_tables=["activities"])
    assert report["ok"] is False
    assert report["integrity"] != "ok"


def test_restore_drill_table_missing_in_backup(src_db: Path, tmp_path: Path) -> None:
    bk = _make_backup(src_db, tmp_path / "backups")
    report = backup_drill.restore_drill(bk, verify_tables=["no_such_table"])
    assert report["ok"] is False
    assert report["table_checks"][0]["backup_rows"] is None


def test_restore_drill_age_reported(src_db: Path, tmp_path: Path) -> None:
    bk = _make_backup(src_db, tmp_path / "backups")
    import os
    import time

    old = time.time() - 48 * 3600
    os.utime(bk, (old, old))
    report = backup_drill.restore_drill(bk, verify_tables=["activities"])
    assert report["ok"] is True
    assert report["age_hours"] > 47.0


def test_latest_backup_selects_newest(src_db: Path, tmp_path: Path) -> None:
    import os
    import shutil
    import time

    backup_dir = tmp_path / "backups"
    older = _make_backup(src_db, backup_dir)
    newest = backup_dir / "app_20990101_000000.db"
    shutil.copy2(older, newest)
    os.utime(older, (time.time() - 3600,) * 2)
    os.utime(newest, (time.time(), time.time()))
    assert backup_drill.latest_backup(backup_dir, "app") == newest


def test_latest_backup_absent_returns_none(tmp_path: Path) -> None:
    assert backup_drill.latest_backup(tmp_path / "backups", "app") is None


def test_register_drill_job_wires_scheduler(src_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            captured["job_id"] = job_id
            captured["hours"] = hours

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    backup_dir = tmp_path / "backups"
    _make_backup(src_db, backup_dir)
    sched = backup_drill.register_restore_drill_job(
        backup_dir, "app", source_db=src_db, verify_tables=["activities"], interval_hours=24
    )
    assert isinstance(sched, FakeScheduler)
    assert captured["job_id"] == "restore-drill"
    assert captured["hours"] == 24


def test_register_drill_job_no_product_logs_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    class FakeScheduler:
        def add_interval_job(self, job, job_id: str, hours: int) -> None:
            job()

    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: FakeScheduler())
    with caplog.at_level(logging.ERROR, logger="nexus.backup_drill"):
        sched = backup_drill.register_restore_drill_job(
            tmp_path / "backups", "app", verify_tables=["t"], interval_hours=24
        )
    assert isinstance(sched, FakeScheduler)
    assert any("no backup product" in r.message for r in caplog.records)


def test_backup_freshness_ok_and_stale(src_db: Path, tmp_path: Path) -> None:
    import os
    import time

    backup_dir = tmp_path / "backups"
    bk = _make_backup(src_db, backup_dir)
    report = backup_drill.backup_freshness(backup_dir, "app", max_age_hours=24.0)
    assert report["ok"] is True
    assert report["latest"] == bk.name
    assert report["age_hours"] < 24.0
    os.utime(bk, (time.time() - 48 * 3600,) * 2)
    stale = backup_drill.backup_freshness(backup_dir, "app", max_age_hours=24.0)
    assert stale["ok"] is False
    assert stale["age_hours"] > 47.0


def test_backup_freshness_no_product(tmp_path: Path) -> None:
    report = backup_drill.backup_freshness(tmp_path / "backups", "app")
    assert report["ok"] is False
    assert report["error"] == "no backup product"


def test_run_backup_multi_list_prefix_prunes_each_prefix(
    src_db: Path, tmp_path: Path
) -> None:
    second = tmp_path / "second.db"
    conn = sqlite3.connect(str(second))
    conn.execute("CREATE TABLE negotiations (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO negotiations VALUES (1)")
    conn.commit()
    conn.close()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True)
    import os
    import time

    now = time.time()
    for idx in range(4):
        for stem in ("src", "second"):
            path = backup_dir / f"aa_{stem}_old{idx}.db"
            path.write_bytes(b"x")
            stamp = now - (idx + 1) * 86400
            os.utime(path, (stamp, stamp))
    report = backup.run_backup(
        [src_db, second], backup_dir, ["aa", "bb"], ["activities", "negotiations"], keep=2
    )
    assert report["ok"] is True
    remaining = sorted(p.name for p in backup_dir.glob("aa_*.db"))
    assert len(remaining) == 2
    assert sum(1 for n in remaining if "old" in n) <= 1
    bb_remaining = list(backup_dir.glob("bb_*.db"))
    assert len(bb_remaining) == 1
