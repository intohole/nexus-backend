"""一次性任务状态机：JobManager（注册/超时/异常兜底/SSE 订阅广播）。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Callable

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
