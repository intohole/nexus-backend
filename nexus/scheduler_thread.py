"""线程模式调度器：NexusThreadScheduler（后台线程，不阻塞事件循环）。"""
from __future__ import annotations

from typing import Optional, Union

from nexus.scheduler import CoroFunc, SyncFunc, logger
from nexus.scheduler_jobs import JobManager  # noqa: F401

class NexusThreadScheduler:
    """后台线程调度器：封装 BackgroundScheduler，用于同步/阻塞型周期任务。

    与 NexusScheduler(AsyncIOScheduler) 区分：周期任务含同步阻塞调用
    （socket/DB/子进程等）时必须使用线程调度，避免卡死应用事件循环。
    """

    _instance: Optional["NexusThreadScheduler"] = None

    def __init__(self) -> None:
        self._scheduler: Optional[object] = None
        self._jobs: dict[str, str] = {}

    @classmethod
    def get_instance(cls) -> "NexusThreadScheduler":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_scheduler(self) -> object:
        if not _HAS_APSCHEDULER:
            raise RuntimeError(
                "APScheduler is not installed. Run: pip install apscheduler"
            )
        if self._scheduler is None:
            self._scheduler = BackgroundScheduler()
            self._scheduler.add_listener(self._on_error, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
        return self._scheduler

    def _on_error(self, event: object) -> None:
        job_id = getattr(event, "job_id", "unknown")
        exception = getattr(event, "exception", None)
        if exception:
            logger.error(f"Thread job '{job_id}' failed: {exception}", exc_info=exception)
        else:
            logger.warning(f"Thread job '{job_id}' missed its schedule")

    def add_interval_job(
        self,
        func: Union[CoroFunc, SyncFunc],
        job_id: str,
        minutes: Optional[int] = None,
        hours: Optional[int] = None,
        seconds: Optional[int] = None,
        **kwargs: object,
    ) -> str:
        scheduler = self._ensure_scheduler()
        interval_seconds = seconds or 0
        interval_minutes = minutes or 0
        interval_hours = hours or 0

        if not interval_seconds and not interval_minutes and not interval_hours:
            raise ValueError("At least one of seconds/minutes/hours must be specified")

        trigger = IntervalTrigger(
            seconds=interval_seconds,
            minutes=interval_minutes,
            hours=interval_hours,
        )
        scheduler.add_job(
            func, trigger=trigger, id=job_id, replace_existing=True, **kwargs
        )
        self._jobs[job_id] = "interval"
        return job_id

    def add_cron_job(
        self,
        func: Union[CoroFunc, SyncFunc],
        job_id: str,
        hour: Optional[int] = None,
        minute: Optional[int] = None,
        day_of_week: Optional[str] = None,
        expr: Optional[str] = None,
        timezone: Optional[str] = None,
        **kwargs: object,
    ) -> str:
        scheduler = self._ensure_scheduler()
        if expr is not None:
            trigger = CronTrigger.from_crontab(expr, timezone=timezone or "Asia/Shanghai")
        else:
            trigger_kwargs: dict[str, object] = {}
            if hour is not None:
                trigger_kwargs["hour"] = hour
            if minute is not None:
                trigger_kwargs["minute"] = minute
            if day_of_week is not None:
                trigger_kwargs["day_of_week"] = day_of_week

            trigger = CronTrigger(**trigger_kwargs)
        scheduler.add_job(
            func, trigger=trigger, id=job_id, replace_existing=True, **kwargs
        )
        self._jobs[job_id] = "cron"
        return job_id

    def remove_job(self, job_id: str) -> bool:
        if self._scheduler is None:
            return False
        try:
            self._scheduler.remove_job(job_id)
            self._jobs.pop(job_id, None)
            return True
        except Exception:
            return False

    def list_jobs(self) -> dict[str, str]:
        return dict(self._jobs)

    def start(self) -> None:
        if self._scheduler is None:
            logger.info("No thread jobs registered, scheduler not started")
            return
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info(f"Thread scheduler started with {len(self._jobs)} jobs")
        else:
            logger.info("Thread scheduler already running")

    def shutdown(self, wait: bool = True) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            logger.info("Thread scheduler shutdown complete")

    @property
    def running(self) -> bool:
        if self._scheduler is None:
            return False
        return self._scheduler.running


def get_thread_scheduler() -> NexusThreadScheduler:
    return NexusThreadScheduler.get_instance()

from nexus.scheduler_jobs import JobManager  # noqa: E402

__all__ = ["NexusScheduler", "NexusThreadScheduler", "get_scheduler", "get_thread_scheduler", "JobManager"]
