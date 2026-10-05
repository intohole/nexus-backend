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

    自愈与可观测（travelMate r13 生产 cron 全天未触发、无异常日志的教训）：
    - fire_timeout: 单任务执行超时墙钟；挂死任务被 wait_for 打断并推进节拍，
      不再冻结整个 loop（fired 日志在 func 返回后才打——挂死=无日志无报错的观测死角）
    - ensure_alive(): task 缺失/已死时重建，适合挂在 health check 上做看门狗
    - status()/last_tick_at: loop 活性与各任务 next_run_at 可随时内省
    """

    def __init__(
        self,
        tick_seconds: int = 30,
        *,
        tz: ZoneInfo = CHINA_TZ,
        fire_timeout: float = 300.0,
    ) -> None:
        self._tick_seconds = tick_seconds
        self._tz = tz
        self._fire_timeout = fire_timeout
        self._task: Optional[asyncio.Task] = None
        self._cron_jobs: dict[str, dict[str, object]] = {}
        self._scanners: dict[str, ScanHandler] = {}
        self._running_jobs: set[str] = set()
        self.last_tick_at: Optional[datetime] = None

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

    def status(self) -> dict[str, object]:
        """调度器内省：loop 活性、最近 tick、各任务下次运行时间。"""
        return {
            "alive": self._task is not None and not self._task.done(),
            "last_tick_at": self.last_tick_at.isoformat(timespec="seconds") if self.last_tick_at else None,
            "jobs": {
                jid: {"expr": str(job["expr"]), "next_run_at": str(job.get("next_run_at"))}
                for jid, job in self._cron_jobs.items()
            },
        }

    def ensure_alive(self) -> bool:
        """loop 守护：task 缺失或已死时重建。返回是否触发了重建。

        适合挂到 health check 等高频入口当看门狗——create_task 后 task
        静默死亡（无异常日志）在生产出现过，靠外部心跳发现并自愈。
        """
        if self._task is not None and not self._task.done():
            return False
        logger.warning("crontab loop not running (task=%r), restarting", self._task)
        self.start()
        return True

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
            self.last_tick_at = datetime.now(self._tz).replace(tzinfo=None)
            try:
                await self._tick()
            except Exception as exc:
                logger.error("crontab scheduler tick failed: %s", exc)
            await asyncio.sleep(self._tick_seconds)

    async def _tick(self) -> None:
        for scanner in list(self._scanners.values()):
            try:
                await asyncio.wait_for(scanner(), timeout=self._fire_timeout)
            except asyncio.TimeoutError:
                logger.error(
                    "crontab scanner %s timed out after %ss — tick continues",
                    scanner, self._fire_timeout,
                )
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
        if job_id in self._running_jobs:
            return
        self._running_jobs.add(job_id)
        func = job["func"]
        try:
            await asyncio.wait_for(func(), timeout=self._fire_timeout)
        except asyncio.TimeoutError:
            logger.error(
                "crontab job %s timed out after %ss — advancing schedule, this run skipped",
                job_id, self._fire_timeout,
            )
            job["next_run_at"] = next_run_at(str(job["expr"]), base=datetime.now(self._tz).replace(tzinfo=None), tz=self._tz)
            return
        finally:
            self._running_jobs.discard(job_id)
        job["next_run_at"] = next_run_at(str(job["expr"]), base=datetime.now(self._tz).replace(tzinfo=None), tz=self._tz)
        logger.info("crontab job %s fired, next run at %s", job_id, job["next_run_at"])

