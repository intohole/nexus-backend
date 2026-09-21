"""通用 crontab 定时任务组件：标准 cron 表达式解析 + 到期扫描调度器。

- next_run_at : 标准 crontab 表达式（分 时 日 月 周）→ 下次运行时间（默认 Asia/Shanghai 墙钟，naive）
- CronScheduler : 通用调度器，支持两种注册方式
  - add_cron_job    : 静态注册 cron 表达式任务（事件循环内执行，内部维护 next_run_at）
  - register_scanner: DB 驱动到期扫描（每 tick 调用 handler，由业务扫描到期任务并执行）

不参与数据库行为：到期任务查询与持久化由消费项目承担。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from nexus.logging import get_logger

try:
    from croniter import croniter

    _HAS_CRONITER = True
except ImportError:
    _HAS_CRONITER = False

logger = get_logger("nexus.crontab")

CHINA_TZ = ZoneInfo("Asia/Shanghai")

CoroFunc = Callable[[], Awaitable[object]]
ScanHandler = CoroFunc


def next_run_at(
    expr: str,
    base: Optional[datetime] = None,
    *,
    tz: ZoneInfo = CHINA_TZ,
) -> Optional[datetime]:
    """计算标准 crontab 表达式的下一次运行时间。

    base 为 naive 时视为 tz 时区墙钟时间（默认 Asia/Shanghai）；返回 tz 墙钟 naive。
    表达式非法或 croniter 不可用时返回 None。
    """
    if not _HAS_CRONITER or not expr or not expr.strip():
        return None
    if base is not None and base.tzinfo is not None:
        base_naive = base.astimezone(tz).replace(tzinfo=None)
    else:
        base_naive = base if base is not None else datetime.now(tz).replace(tzinfo=None)
    try:
        return croniter(expr.strip(), base_naive).get_next(datetime)
    except Exception:
        return None


class CronScheduler:
    """通用 crontab 调度器。

    周期 tick 扫描：静态注册的 cron 任务到期触发；DB 驱动扫描器每次 tick 被调用。
    tick 循环内单任务异常隔离，不影响其他任务。
    """

    def __init__(self, tick_seconds: int = 30, *, tz: ZoneInfo = CHINA_TZ) -> None:
        self._tick_seconds = tick_seconds
        self._tz = tz
        self._task: Optional[asyncio.Task] = None
        self._cron_jobs: dict[str, dict[str, object]] = {}
        self._scanners: dict[str, ScanHandler] = {}

    def add_cron_job(
        self,
        job_id: str,
        expr: str,
        func: CoroFunc,
    ) -> None:
        self._cron_jobs[job_id] = {
            "expr": expr,
            "func": func,
            "next_run_at": next_run_at(expr, tz=self._tz),
        }

    def remove_cron_job(self, job_id: str) -> bool:
        return self._cron_jobs.pop(job_id, None) is not None

    def register_scanner(self, name: str, handler: ScanHandler) -> None:
        self._scanners[name] = handler

    def unregister_scanner(self, name: str) -> bool:
        return self._scanners.pop(name, None) is not None

    def list_jobs(self) -> dict[str, str]:
        return {jid: "cron" for jid in self._cron_jobs} | {name: "scan" for name in self._scanners}

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("crontab scheduler tick failed: %s", exc)
            await asyncio.sleep(self._tick_seconds)

    async def _tick(self) -> None:
        for scanner in list(self._scanners.values()):
            try:
                await scanner()
            except Exception as exc:
                logger.error("crontab scanner failed: %s", exc)
        for job_id, job in list(self._cron_jobs.items()):
            try:
                await self._maybe_fire(job_id, job)
            except Exception as exc:
                logger.error("crontab job %s failed: %s", job_id, exc)

    async def _maybe_fire(self, job_id: str, job: dict[str, object]) -> None:
        due_at = job.get("next_run_at")
        if due_at is None:
            return
        now_naive = datetime.now(self._tz).replace(tzinfo=None)
        if due_at > now_naive:
            return
        func = job["func"]
        await func()
        job["next_run_at"] = next_run_at(str(job["expr"]), base=now_naive, tz=self._tz)
        logger.info("crontab job %s fired, next run at %s", job_id, job["next_run_at"])


_scheduler: Optional[CronScheduler] = None


def get_cron_scheduler() -> CronScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = CronScheduler()
    return _scheduler
