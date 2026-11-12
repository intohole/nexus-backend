"""lifecycle 周期清理调度接线（r205）：TTL 清理挂 nexus.scheduler cron job 单点。

背景（usercenter 5 天 3000+ 轮 watermark degraded 生产实录）：S29 契约接线点
「startup 幂等或 cron」二选一，13 仓选了 startup-only——清理频率=重启频率，
长驻进程下 TTL 过期行持续堆积直到下次重启才被启动清理顺带走。业界对标
（PostgreSQL autovacuum / MySQL EVENT Scheduler / K8s CronJob）清理全为
周期语义。本原语与 r190 register_backup_job 同构：惰性接线 get_scheduler，
仓内一行注册；startup 幂等清理保留（部署窗即时清），cron job 承担日常驱动。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Sequence, Union

from nexus.lifecycle import LifecyclePolicy, enforce_lifecycle

logger = logging.getLogger("nexus.lifecycle")

LIFECYCLE_JOB_ID = "lifecycle-purge"
LIFECYCLE_CRON = "20 4 * * *"

PathFn = Callable[[], Union[str, Path]]


def register_lifecycle_job(
    policies: Sequence[LifecyclePolicy],
    db_path: Union[str, Path, PathFn],
    job_id: str = LIFECYCLE_JOB_ID,
    cron_expr: str = LIFECYCLE_CRON,
    dry_run: bool = False,
    archive_root: str | Path | None = None,
    auto_start: bool = True,
):
    """注册周期清理 job（默认每日 04:20 低峰，aiPet 判例时刻）。

    db_path 传路径或仓内 _db_file_path 零参函数（延迟解析 settings/env）；
    enforce_lifecycle 五护栏（导出备份/失败跳表/留痕）原样生效，本函数只补
    调度层，不复制任何删除语义。返回 scheduler 供链上断言。
    auto_start=True（默认）：scheduler 未运行则 start——gate 结算走同一份
    NexusScheduler.start(mode=None)：env NEXUS_SCHEDULER_MODE 显式声明优先
    （gated 仓被 env 兜住 fail-closed），缺省 auto 允许（usercenter 既有
    行为）；已有 start 的仓 running 幂等 no-op。传 False 交给宿主 lifespan
    自行 start。
    """
    from nexus.scheduler import get_scheduler

    def _job() -> None:
        try:
            path = db_path() if callable(db_path) else db_path
            report = enforce_lifecycle(path, policies, dry_run=dry_run, archive_root=archive_root)
            logger.info(
                "lifecycle job done: deleted=%s tables=%s",
                report.total_deleted, len(report.tables),
            )
        except Exception:
            logger.exception("lifecycle job crashed")

    scheduler = get_scheduler()
    scheduler.add_cron_job(_job, job_id=job_id, expr=cron_expr)
    if auto_start and not scheduler.running:
        scheduler.start()
    return scheduler
