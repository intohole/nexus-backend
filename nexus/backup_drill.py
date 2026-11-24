"""恢复演练原语（S159 DE4）：restore 到临时库+integrity+行数/max id 业务级对账.

业界 fire drill 共识：备份验证的唯一诚实形态是 restore 演练——产物级验证
（VACUUM INTO+integrity+行数对账）由 backup.run_backup 内建，本模块补
「定期把最新产物 restore 到临时库真打开+对账」面。陈旧度（age_hours/
max id gap）如实报告不进 ok 判定——备份落后活库是 6h interval 预期态，
新鲜度告警属 DE3 探针职责，演练只定谳「这份备份可用吗」。
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from nexus.backup import _verify_sqlite_integrity, table_count

logger = logging.getLogger(__name__)

DRILL_JOB_ID = "restore-drill"


def latest_backup(backup_dir: Path, prefix: str) -> Optional[Path]:
    if not backup_dir.is_dir():
        return None
    candidates = sorted(
        backup_dir.glob(f"{prefix}_*.db"),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    return candidates[-1] if candidates else None


def _max_id(db_path: Path, table: str) -> Optional[int]:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(f'SELECT MAX(id) FROM "{table}"').fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        return None


def restore_drill(
    backup_path: Path,
    source_db: Optional[Path] = None,
    verify_tables: Optional[list[str]] = None,
    tmp_dir: Optional[Path] = None,
) -> dict[str, object]:
    report: dict[str, object] = {"ok": False, "backup": str(backup_path)}
    if not backup_path.exists():
        report["error"] = "backup not found"
        return report
    verify_tables = verify_tables or []
    target_dir = tmp_dir or backup_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    restored = target_dir / f"restore_drill_{backup_path.stem}.db"
    try:
        shutil.copy2(backup_path, restored)
        report["restored_to"] = str(restored)
        report["integrity"] = "ok" if _verify_sqlite_integrity(restored) else "failed"
        src_live = bool(source_db and source_db.exists())
        checks = []
        for table in verify_tables:
            check: dict[str, object] = {
                "table": table,
                "backup_rows": table_count(restored, table),
                "backup_max_id": _max_id(restored, table),
                "source_rows": table_count(source_db, table) if src_live else None,
                "source_max_id": _max_id(source_db, table) if src_live else None,
            }
            check["ok"] = check["backup_rows"] is not None
            checks.append(check)
        report["table_checks"] = checks
        report["age_hours"] = round(
            (datetime.now().timestamp() - backup_path.stat().st_mtime) / 3600.0, 1
        )
        report["ok"] = report["integrity"] == "ok" and all(c["ok"] for c in checks)
        return report
    finally:
        restored.unlink(missing_ok=True)


def register_restore_drill_job(
    backup_dir: Path,
    prefix: str,
    source_db: Optional[Path] = None,
    verify_tables: Optional[list[str]] = None,
    interval_hours: int = 24,
    job_id: str = DRILL_JOB_ID,
    tmp_dir: Optional[Path] = None,
):
    from nexus.scheduler import get_scheduler

    def _job() -> None:
        bk = latest_backup(backup_dir, prefix)
        if bk is None:
            logger.error(
                "restore drill skipped: no backup product in %s (prefix=%s)", backup_dir, prefix
            )
            return
        report = restore_drill(bk, source_db=source_db, verify_tables=verify_tables, tmp_dir=tmp_dir)
        if not report.get("ok"):
            logger.error("restore drill failed: %s", report)
        else:
            logger.info(
                "restore drill ok: backup=%s age_hours=%s", bk.name, report.get("age_hours")
            )

    get_scheduler().add_interval_job(_job, job_id=job_id, hours=interval_hours)
    return get_scheduler()


def backup_freshness(backup_dir: Path, prefix: str, max_age_hours: float = 24.0) -> dict[str, object]:
    report: dict[str, object] = {"prefix": prefix, "backup_dir": str(backup_dir)}
    bk = latest_backup(backup_dir, prefix)
    if bk is None:
        report["ok"] = False
        report["error"] = "no backup product"
        return report
    age_hours = (datetime.now().timestamp() - bk.stat().st_mtime) / 3600.0
    report["latest"] = bk.name
    report["age_hours"] = round(age_hours, 1)
    report["ok"] = age_hours <= max_age_hours
    return report
