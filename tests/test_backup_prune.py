import os
import time
from pathlib import Path

from nexus import backup




def _seed_backups_with_ages(tmp_path: Path, ages_days: list[float]) -> Path:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True)
    now = time.time()
    for idx, age in enumerate(ages_days):
        path = backup_dir / f"gz_seed{idx:03d}.db"
        path.write_bytes(b"x")
        stamp = now - age * 86400
        os.utime(path, (stamp, stamp))
    return backup_dir


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


def test_prune_age_cap_respects_keep_floor(tmp_path: Path) -> None:
    import os
    import time

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = time.time()
    ages = [10, 9, 8, 6.5, 1, 0]
    for idx, age in enumerate(ages):
        path = backup_dir / f"gz_2026010{idx}_000000.db"
        path.write_bytes(b"x")
        stamp = now - age * 86400
        os.utime(path, (stamp, stamp))
    pruned = backup.prune_backups(backup_dir, "gz", keep=2, max_age_days=7)
    remaining = sorted(p.name for p in backup_dir.glob("gz_*.db"))
    assert len(pruned) == 3
    assert len(remaining) == 3
    assert "gz_20260103_000000.db" in remaining


def test_prune_total_size_cap_prunes_oldest_first(tmp_path: Path) -> None:
    import os
    import time

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = time.time()
    for idx in range(5):
        path = backup_dir / f"gz_2026010{idx}_000000.db"
        path.write_bytes(b"x" * 10)
        stamp = now - (5 - idx) * 3600
        os.utime(path, (stamp, stamp))
    pruned = backup.prune_backups(backup_dir, "gz", keep=1, max_total_bytes=25)
    remaining = sorted(p.name for p in backup_dir.glob("gz_*.db"))
    assert len(pruned) == 3
    assert remaining == ["gz_20260103_000000.db", "gz_20260104_000000.db"]


def test_prune_age_cap_never_below_keep(tmp_path: Path) -> None:
    import os
    import time

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = time.time()
    for idx in range(6):
        path = backup_dir / f"gz_old_{idx}.db"
        path.write_bytes(b"x")
        stamp = now - 30 * 86400
        os.utime(path, (stamp, stamp))
    pruned = backup.prune_backups(backup_dir, "gz", keep=3, max_age_days=7)
    remaining = sorted(p.name for p in backup_dir.glob("gz_*.db"))
    assert len(pruned) == 3
    assert len(remaining) == 3


def test_prune_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for idx in range(3):
        (backup_dir / f"gz_dry_{idx}.db").write_bytes(b"x")
    planned = backup.prune_backups(backup_dir, "gz", keep=1, dry_run=True)
    assert len(planned) == 2
    assert len(list(backup_dir.glob("gz_*.db"))) == 3


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


def test_prune_elder_none_keeps_legacy_behavior(tmp_path: Path) -> None:
    ages = [20, 15, 10, 5, 2, 0]
    backup_dir = _seed_backups_with_ages(tmp_path, ages)
    pruned = backup.prune_backups(backup_dir, "gz", keep=2)
    remaining = sorted(p.name for p in backup_dir.glob("gz_*.db"))
    assert len(pruned) == 4
    assert len(remaining) == 2


def test_prune_elder_sparse_retention(tmp_path: Path) -> None:
    ages = [float(20 - i) for i in range(21)]
    backup_dir = _seed_backups_with_ages(tmp_path, ages)
    pruned = backup.prune_backups(
        backup_dir, "gz", keep=3, keep_elder=2, elder_interval_days=7
    )
    remaining = {p.name for p in backup_dir.glob("gz_*.db")}
    assert remaining == {
        "gz_seed020.db", "gz_seed019.db", "gz_seed018.db",
        "gz_seed011.db", "gz_seed004.db",
    }
    assert len(pruned) == 16


def test_prune_elder_exempts_age_cutoff(tmp_path: Path) -> None:
    ages = [40, 38, 36, 2, 1, 0]
    backup_dir = _seed_backups_with_ages(tmp_path, ages)
    pruned = backup.prune_backups(
        backup_dir, "gz", keep=2, max_age_days=7, keep_elder=1, elder_interval_days=30
    )
    remaining = {p.name for p in backup_dir.glob("gz_*.db")}
    assert "gz_seed002.db" in remaining
    assert "gz_seed000.db" not in remaining
    assert "gz_seed001.db" not in remaining
    assert len(pruned) == 2


def test_prune_elder_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    ages = [float(20 - i) for i in range(21)]
    backup_dir = _seed_backups_with_ages(tmp_path, ages)
    pruned = backup.prune_backups(
        backup_dir, "gz", keep=3, keep_elder=2, elder_interval_days=7, dry_run=True
    )
    assert len(pruned) == 16
    assert len(list(backup_dir.glob("gz_*.db"))) == 21


def test_prune_elder_zero_or_missing_interval_disables(tmp_path: Path) -> None:
    ages = [20, 15, 10, 5, 2, 0]
    backup_dir = _seed_backups_with_ages(tmp_path, ages)
    backup.prune_backups(backup_dir, "gz", keep=2, keep_elder=0, elder_interval_days=7)
    assert len(list(backup_dir.glob("gz_*.db"))) == 2
    backup_dir2 = _seed_backups_with_ages(tmp_path / "b2", ages)
    backup.prune_backups(backup_dir2, "gz", keep=2, keep_elder=2)
    assert len(list(backup_dir2.glob("gz_*.db"))) == 2
