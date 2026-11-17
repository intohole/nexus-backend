"""WAL 治理原语（S159 DE5，r223 五节点观测供料）：checkpoint 饥饿档定期 TRUNCATE.

业界口径（sqlite.org/wal.html §6 + 2025 生产实践共识）：autocheckpoint（默认
1000 页≈4MB）由 commit 线程触发 PASSIVE 检查点，读者重叠不断档时永不完成
=饥饿档，WAL 无界增长且每个读者检查成本∝WAL 大小；治理形态=低峰窗
wal_checkpoint(TRUNCATE)（跑完才返回；读者持快照时返回 busy=1 诚实失败）。
TRUNCATE 有写放大代价（flash endurance），低于阈值不动作——阈值取默认
autocheckpoint 档：低于它说明 autocheckpoint 本身健康，治理不插手。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 6
DEFAULT_MIN_WAL_BYTES = 4 * 1024 * 1024
WAL_JOB_ID = "wal-governance"

PathLike = Union[Path, list[Path]]


def wal_size(db_path: Path) -> Optional[int]:
    try:
        return Path(str(db_path) + "-wal").stat().st_size
    except OSError:
        return None


def wal_checkpoint_truncate(db_path: Path, busy_timeout_ms: int = 2000) -> dict[str, object]:
    report: dict[str, object] = {"db": str(db_path), "ok": False}
    db_path = Path(db_path)
    if not db_path.exists():
        report["error"] = "db not found"
        return report
    wal_path = Path(str(db_path) + "-wal")
    report["wal_before"] = wal_path.stat().st_size if wal_path.exists() else 0
    conn = sqlite3.connect(str(db_path), timeout=busy_timeout_ms / 1000)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        report["journal_mode"] = mode[0] if mode else None
        if report["journal_mode"] != "wal":
            report["error"] = f"journal_mode={report['journal_mode']} not wal"
            return report
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        report["busy"] = bool(row and row[0])
        report["log_frames"] = row[1] if row else None
        report["checkpointed_frames"] = row[2] if row else None
    except sqlite3.Error as exc:
        report["error"] = str(exc)
        return report
    finally:
        conn.close()
    report["wal_after"] = wal_path.stat().st_size if wal_path.exists() else 0
    report["ok"] = not report.get("busy") and report["wal_after"] == 0
    return report


def run_wal_governance(
    db_paths: PathLike,
    min_wal_bytes: int = DEFAULT_MIN_WAL_BYTES,
) -> dict[str, object]:
    paths = db_paths if isinstance(db_paths, list) else [db_paths]
    parts: list[dict[str, object]] = []
    for db_path in paths:
        db_path = Path(db_path)
        part: dict[str, object] = {"db": str(db_path)}
        if not db_path.exists():
            part["error"] = "db not found"
            parts.append(part)
            continue
        size = wal_size(db_path)
        part["wal_before"] = size or 0
        if size is None:
            part["skipped"] = True
            part["reason"] = "no wal file"
            parts.append(part)
            continue
        if size < min_wal_bytes:
            part["skipped"] = True
            part["reason"] = "below_threshold"
            parts.append(part)
            continue
        parts.append({**part, **wal_checkpoint_truncate(db_path)})
    return {
        "ok": all(p.get("ok") or p.get("skipped") for p in parts) if parts else True,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "parts": parts,
    }


def _mb(n: object) -> str:
    try:
        return f"{int(n) / 1048576:.1f}MB"
    except (TypeError, ValueError):
        return "?"


def register_wal_checkpoint_job(
    db_paths: PathLike,
    interval_hours: int = DEFAULT_INTERVAL_HOURS,
    min_wal_bytes: int = DEFAULT_MIN_WAL_BYTES,
    job_id: str = WAL_JOB_ID,
):
    from nexus.scheduler import get_scheduler

    def _job() -> None:
        try:
            report = run_wal_governance(db_paths, min_wal_bytes)
            for part in report.get("parts", []):
                if part.get("skipped"):
                    continue
                if part.get("ok"):
                    logger.info(
                        "wal governance ok: %s %s -> %s",
                        Path(str(part["db"])).name,
                        _mb(part.get("wal_before")),
                        _mb(part.get("wal_after")),
                    )
                else:
                    logger.warning("wal governance incomplete: %s", part)
        except Exception:
            logger.exception("wal governance job crashed")

    get_scheduler().add_interval_job(_job, job_id=job_id, hours=interval_hours)
    return get_scheduler()
