"""调度器抽象：AsyncIOScheduler(事件循环内) 与 BackgroundScheduler(线程) 两种模式。

- NexusScheduler       : async 任务调度（asyncio 事件循环内执行）
- NexusThreadScheduler : 同步/阻塞型周期任务调度（后台线程，不阻塞事件循环）
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Union

from nexus.logging import get_logger

if TYPE_CHECKING:
    from nexus.fastapi_setup import AppLifecycle

logger = get_logger("nexus.scheduler")

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED

    _HAS_APSCHEDULER = True
except ImportError:
    _HAS_APSCHEDULER = False
    AsyncIOScheduler = None
    BackgroundScheduler = None
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


def setup_scheduler(
    lifecycle: "AppLifecycle",
) -> NexusScheduler:
    scheduler = get_scheduler()

    async def _start_scheduler():
        scheduler.start()

    async def _stop_scheduler():
        scheduler.shutdown(wait=False)

    lifecycle.add_startup_hook(_start_scheduler)
    lifecycle.add_shutdown_hook(_stop_scheduler)
    return scheduler


class JobManager:
    """一次性任务状态机 + SSE 订阅广播。

    统一承担任务注册/超时/异常兜底/订阅广播/events 迭代，
    业务仅需提供 runner（事件异步生成器工厂）与 on_event（持久化回调）。
    timeout_seconds=0 表示不设超时。
    """

    def __init__(self, timeout_seconds: float = 0) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._users: dict[int, str] = {}
        self._subscribers: dict[int, set] = {}
        self._timeout_seconds = timeout_seconds

    def is_running(self, job_id: int) -> bool:
        task = self._tasks.get(job_id)
        return bool(task and not task.done())

    def submit(
        self,
        job_id: int,
        user_id: str,
        runner: Callable[[], AsyncIterator[dict[str, object]]],
        on_event: Callable[[int, str, dict[str, object]], Awaitable[None]],
        *,
        terminal_types: tuple[str, ...] = ("done", "error"),
        timeout: Optional[float] = None,
        timeout_message: str = "任务超时，请稍后重试",
    ) -> bool:
        if self.is_running(job_id):
            return False
        self._users[job_id] = user_id
        task = asyncio.create_task(
            self._run(job_id, user_id, runner, on_event, terminal_types, timeout, timeout_message)
        )
        self._tasks[job_id] = task
        return True

    async def _run(
        self,
        job_id: int,
        user_id: str,
        runner: Callable[[], AsyncIterator[dict[str, object]]],
        on_event: Callable[[int, str, dict[str, object]], Awaitable[None]],
        terminal_types: tuple[str, ...],
        timeout: Optional[float],
        timeout_message: str,
    ) -> None:
        limit = timeout if timeout is not None else self._timeout_seconds
        try:
            if limit > 0:
                async with asyncio.timeout(limit):
                    await self._loop(job_id, user_id, runner, on_event, terminal_types)
            else:
                await self._loop(job_id, user_id, runner, on_event, terminal_types)
        except TimeoutError:
            logger.error("job %s timed out", job_id)
            await self._fail(job_id, user_id, on_event, {"type": "error", "message": timeout_message})
        except Exception as exc:
            logger.error("job %s failed: %s", job_id, exc)
            await self._fail(job_id, user_id, on_event, {"type": "error", "message": str(exc)})
        finally:
            self._tasks.pop(job_id, None)

    async def _loop(
        self,
        job_id: int,
        user_id: str,
        runner: Callable[[], AsyncIterator[dict[str, object]]],
        on_event: Callable[[int, str, dict[str, object]], Awaitable[None]],
        terminal_types: tuple[str, ...],
    ) -> None:
        async for evt in runner():
            await on_event(job_id, user_id, evt)
            await self._broadcast(job_id, evt)
            if evt.get("type") in terminal_types:
                break

    async def _fail(
        self,
        job_id: int,
        user_id: str,
        on_event: Callable[[int, str, dict[str, object]], Awaitable[None]],
        err: dict[str, object],
    ) -> None:
        try:
            await on_event(job_id, user_id, err)
        except Exception:
            pass
        await self._broadcast(job_id, err)

    def subscribe(self, job_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(job_id, set()).add(queue)
        return queue

    def unsubscribe(self, job_id: int, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(job_id)
        if subs:
            subs.discard(queue)
            if not subs:
                self._subscribers.pop(job_id, None)

    async def _broadcast(self, job_id: int, evt: dict[str, object]) -> None:
        for queue in list(self._subscribers.get(job_id, set())):
            await queue.put(evt)

    async def events(
        self,
        job_id: int,
        *,
        ping_seconds: float = 15,
        terminal_types: tuple[str, ...] = ("done", "error", "clarify"),
    ) -> AsyncIterator[dict[str, object]]:
        queue = self.subscribe(job_id)
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=ping_seconds)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
                    continue
                yield evt
                if evt.get("type") in terminal_types:
                    break
        finally:
            self.unsubscribe(job_id, queue)
