"""SQLite 业务库持续备份原语（S107 五护栏单源：VACUUM INTO/integrity+行数对账/keep-N/白名单清理/失败即删）.

业界口径（2025 共识）：WAL 活库禁裸 cp，VACUUM INTO 与 backup API 均安全——
本原语取 VACUUM INTO（跨进程读锁不阻塞写+产物压缩去碎片）。仓内配置面
（db 路径/prefix/VERIFY_TABLES）由各仓薄 shim 声明，行为全部单源于此。
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Union

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 6
DEFAULT_KEEP = 10
JOB_ID = "db-continuous-backup"

PathLike = Union[Path, list[Path]]
PrefixLike = Union[str, list[str], None]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _vacuum_into(source: Path, target: Path) -> None:
    conn = sqlite3.connect(str(source))
    try:
        escaped = str(target).replace("'", "''")
        try:
            conn.execute(f"VACUUM INTO '{escaped}'")
        except sqlite3.OperationalError as exc:
            if sqlite3.sqlite_version_info >= (3, 27, 0) or "INTO" not in str(exc):
                raise
            logger.warning("VACUUM INTO unsupported (sqlite %s), falling back to backup API", sqlite3.sqlite_version)
            dst = sqlite3.connect(str(target))
            try:
                with dst:
                    conn.backup(dst)
            finally:
                dst.close()
    finally:
        conn.close()


def _verify_sqlite_integrity(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute("PRAGMA integrity_check")
            row = cur.fetchone()
            return bool(row and row[0] == "ok")
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def table_count(db_path: Path, table: str) -> Optional[int]:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
            row = cur.fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


_table_count = table_count


def _backup_one(db_path: Path, target: Path, verify_tables: list[str], skip_missing_tables: bool = False) -> dict[str, object]:
    part: dict[str, object] = {"db": str(db_path), "ok": False}
    if not db_path.exists():
        part["error"] = "db not found"
        return part
    _vacuum_into(db_path, target)
    if not target.exists() or target.stat().st_size == 0:
        part["error"] = "backup file empty"
        target.unlink(missing_ok=True)
        return part
    if not _verify_sqlite_integrity(target):
        part["error"] = "integrity_check failed"
        target.unlink(missing_ok=True)
        return part
    checks = []
    for table in verify_tables:
        src_n = _table_count(db_path, table)
        if skip_missing_tables and src_n is None:
            continue
        bak_n = _table_count(target, table)
        ok = src_n is not None and bak_n is not None and bak_n >= src_n
        checks.append({"table": table, "src": src_n, "backup": bak_n, "ok": ok})
    part["backup"] = str(target)
    part["size_bytes"] = target.stat().st_size
    part["integrity"] = "ok"
    part["table_checks"] = checks
    part["ok"] = all(c["ok"] for c in checks) if checks else True
    if not part["ok"]:
        target.unlink(missing_ok=True)
    return part


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def prune_files(
    directory: Path,
    patterns: list[str],
    keep: int = 0,
    max_age_days: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
    dry_run: bool = False,
) -> list[str]:
    if not directory.exists():
        return []
    candidates: list[tuple[float, Path]] = []
    for pattern in patterns:
        for path in directory.glob(pattern):
            if not path.is_file():
                continue
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    total = 0
    for _, path in candidates:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    protected_count = min(keep, len(candidates))
    remainder = [path for _, path in candidates[protected_count:]]
    cutoff = (datetime.now().timestamp() - max_age_days * 86400) if max_age_days else None
    pruned: list[str] = []

    def remove(path: Path) -> None:
        nonlocal total
        try:
            size = path.stat().st_size
            if not dry_run:
                path.unlink()
        except OSError:
            return
        total -= size
        pruned.append(path.name)

    if cutoff is None and max_total_bytes is None:
        for path in remainder:
            remove(path)
        return pruned
    if cutoff is not None:
        for path in list(remainder):
            try:
                if path.stat().st_mtime < cutoff:
                    remove(path)
                    remainder.remove(path)
            except OSError:
                continue
    if max_total_bytes is not None and total > max_total_bytes:
        for path in sorted(remainder, key=_safe_mtime):
            if total <= max_total_bytes:
                break
            remove(path)
    return pruned


def prune_backups(
    backup_dir: Path,
    prefix: str,
    keep: int,
    max_age_days: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
    dry_run: bool = False,
) -> list[str]:
    return prune_files(
        backup_dir, [f"{prefix}_*.db"], keep, max_age_days, max_total_bytes, dry_run
    )


def run_backup(
    db_paths: PathLike,
    backup_dir: Path,
    prefix: PrefixLike,
    verify_tables: Union[str, list[str]],
    keep: int = DEFAULT_KEEP,
    max_age_days: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
) -> dict[str, object]:
    if isinstance(verify_tables, str):
        verify_tables = [verify_tables]
    if isinstance(db_paths, list):
        prefixes = prefix if isinstance(prefix, list) else None
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = _stamp()
        parts = []
        for idx, db_path in enumerate(db_paths):
            stem = db_path.stem
            part_prefix = prefixes[idx] if prefixes else f"{prefix}_{stem}"
            target = backup_dir / f"{part_prefix}_{stamp}.db"
            if target.exists():
                parts.append({"db": str(db_path), "ok": False, "error": "collision"})
                continue
            parts.append(_backup_one(db_path, target, list(verify_tables), skip_missing_tables=True))
        report: dict[str, object] = {
            "ok": all(p.get("ok") for p in parts) if parts else False,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "parts": parts,
        }
        if report["ok"]:
            report["pruned"] = prune_backups(
                backup_dir, str(prefix), keep, max_age_days, max_total_bytes
            )
        return report
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    target = backup_dir / f"{prefix}_{stamp}.db"
    if target.exists():
        return {"ok": False, "ts": datetime.now().isoformat(timespec="seconds"), "error": "collision"}
    report = _backup_one(db_paths, target, list(verify_tables))
    report["ts"] = datetime.now().isoformat(timespec="seconds")
    if not report.get("ok"):
        return report
    report["pruned"] = prune_backups(backup_dir, str(prefix), keep, max_age_days, max_total_bytes)
    return report


def register_backup_job(
    db_paths: PathLike,
    backup_dir: Union[Path, Callable[[], Path]],
    prefix: PrefixLike,
    verify_tables: Union[str, list[str]],
    keep: int = DEFAULT_KEEP,
    interval_hours: int = DEFAULT_INTERVAL_HOURS,
    job_id: str = JOB_ID,
    max_age_days: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
):
    from nexus.scheduler import get_scheduler

    def _job() -> None:
        try:
            report = run_backup(
                db_paths, backup_dir, prefix, verify_tables, keep, max_age_days, max_total_bytes
            )
            if not report.get("ok"):
                logger.error("backup failed: %s", report)
        except Exception:
            logger.exception("continuous backup job crashed")

    get_scheduler().add_interval_job(_job, job_id=job_id, hours=interval_hours)
    return get_scheduler()


def backup_cli(
    db_path_fn: Callable[[], PathLike],
    backup_dir_fn: Callable[[], Path],
    prefix_fn: Callable[[], PrefixLike],
    verify_tables: Union[str, list[str]],
    keep: int = DEFAULT_KEEP,
    interval_hours: int = DEFAULT_INTERVAL_HOURS,
    argv: Optional[list[str]] = None,
    max_age_days: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run and not args.execute:
        db_path = db_path_fn()
        plan = {
            "dry_run": True,
            "db": str(db_path),
            "exists": db_path.exists() if isinstance(db_path, Path) else all(p.exists() for p in db_path),
            "backup_dir": str(backup_dir_fn()),
            "keep": keep,
            "interval_hours": interval_hours,
            "verify_tables": verify_tables,
            "max_age_days": max_age_days,
            "max_total_bytes": max_total_bytes,
        }
        print(json.dumps(plan, ensure_ascii=False, indent=1) if args.json else plan)
        return 0
    report = run_backup(
        db_path_fn(), backup_dir_fn(), prefix_fn(), verify_tables, keep, max_age_days, max_total_bytes
    )
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else report)
    return 0 if report.get("ok") else 1
