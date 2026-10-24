"""数据生命周期仓内接线单源（S29 样板族收归）。

nexus.lifecycle 承载执行器与护栏；本模块承载各业务仓 lifecycle_service 的
同构接线体——11 仓逐字重复的 run_lifecycle（执行+留痕契约：跳过项 warning、
删行 info、返回 report.as_dict()）与 sqlite URL→文件路径解析片段。

仓侧直替形态：
    def run_lifecycle(dry_run: bool = False) -> dict[str, object]:
        return run_lifecycle_report(_db_file_path(), LIFECYCLE_POLICIES,
                                    label="环境快照清理", logger=logger, dry_run=dry_run)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from nexus.lifecycle import LifecyclePolicy, enforce_lifecycle


def sqlite_file_from_url(url: str) -> str:
    """SQLite URL → 库文件路径；非 sqlite URL（如 postgresql://）原样返回。"""
    if url.startswith("sqlite"):
        return url.split("///")[-1] if "///" in url else url.split("://")[-1]
    return url


def run_lifecycle_report(
    db_path: str | Path,
    policies: Sequence[LifecyclePolicy],
    *,
    label: str = "数据生命周期",
    logger: logging.Logger | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行生命周期策略并落地统一留痕契约（enforce_lifecycle 的仓内接线壳）。"""
    log = logger or logging.getLogger("nexus.lifecycle")
    report = enforce_lifecycle(db_path, policies, dry_run=dry_run)
    if not report.ok:
        log.warning("[lifecycle] %s存在跳过项: %s",
                    label, [t.skipped_reason for t in report.tables if t.skipped_reason])
    if report.total_deleted:
        log.info("[lifecycle] %s %s 行 (dry_run=%s)", label, report.total_deleted, dry_run)
    return report.as_dict()


__all__ = ["run_lifecycle_report", "sqlite_file_from_url"]
