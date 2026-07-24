from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Union

from nexus.logging import get_logger

logger = get_logger("nexus.scheduler")

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED

    _HAS_APSCHEDULER = True
except ImportError:
    _HAS_APSCHEDULER = False
    AsyncIOScheduler = None
    IntervalTrigger = None
    CronTrigger = None
    DateTrigger = None

CoroFunc = Callable[..., Awaitable[object]]
SyncFunc = Callable[..., object]


class NexusScheduler:
    _instance: Optional["NexusScheduler"] = None
    _scheduler: Optional[object] = None

    def __init__(self) -> None:
        self._jobs: dict[str, str] = {}

    @classmethod
    def get_instance(cls) -> "NexusScheduler":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_scheduler(self) -> object:
        if not _HAS_APSCHEDULER:
            raise RuntimeError(
                "APScheduler is not installed. Run: pip install apscheduler"
            )
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler()
            self._scheduler.add_listener(self._on_error, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
        return self._scheduler

    def _on_error(self, event: object) -> None:
        job_id = getattr(event, "job_id", "unknown")
        exception = getattr(event, "exception", None)
        if exception:
            logger.error(f"Job '{job_id}' failed: {exception}", exc_info=exception)
        else:
            logger.warning(f"Job '{job_id}' missed its schedule")

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
        logger.info(
            f"Registered interval job '{job_id}': "
            f"{interval_hours}h {interval_minutes}m {interval_seconds}s"
        )
        return job_id

    def add_cron_job(
        self,
        func: Union[CoroFunc, SyncFunc],
        job_id: str,
        hour: Optional[int] = None,
        minute: Optional[int] = None,
        day_of_week: Optional[str] = None,
        **kwargs: object,
    ) -> str:
        scheduler = self._ensure_scheduler()
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
        logger.info(f"Registered cron job '{job_id}': {trigger_kwargs}")
        return job_id

    def add_date_job(
        self,
        func: Union[CoroFunc, SyncFunc],
        job_id: str,
        run_date: str,
        **kwargs: object,
    ) -> str:
        scheduler = self._ensure_scheduler()
        trigger = DateTrigger(run_date=run_date)
        scheduler.add_job(
            func, trigger=trigger, id=job_id, replace_existing=True, **kwargs
        )
        self._jobs[job_id] = "date"
        logger.info(f"Registered date job '{job_id}': run at {run_date}")
        return job_id

    def remove_job(self, job_id: str) -> bool:
        if self._scheduler is None:
            return False
        try:
            self._scheduler.remove_job(job_id)
            self._jobs.pop(job_id, None)
            logger.info(f"Removed job '{job_id}'")
            return True
        except Exception:
            return False

    def list_jobs(self) -> dict[str, str]:
        return dict(self._jobs)

    def start(self) -> None:
        if self._scheduler is None:
            logger.info("No jobs registered, scheduler not started")
            return
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info(f"Scheduler started with {len(self._jobs)} jobs")
        else:
            logger.info("Scheduler already running")

    def shutdown(self, wait: bool = True) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            logger.info("Scheduler shutdown complete")

    @property
    def running(self) -> bool:
        if self._scheduler is None:
            return False
        return self._scheduler.running


def get_scheduler() -> NexusScheduler:
    return NexusScheduler.get_instance()


def setup_scheduler(
    lifecycle: "AppLifecycle",
) -> NexusScheduler:
    from nexus.fastapi_setup import AppLifecycle as _AppLifecycle

    scheduler = get_scheduler()

    async def _start_scheduler():
        scheduler.start()

    async def _stop_scheduler():
        scheduler.shutdown(wait=False)

    lifecycle.add_startup_hook(_start_scheduler)
    lifecycle.add_shutdown_hook(_stop_scheduler)
    return scheduler
