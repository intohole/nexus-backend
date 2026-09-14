from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Coroutine
from typing import Any

logger = logging.getLogger("nexus.background")


class TaskManager:
    """后台任务管理器：注册/异常兜底/取消/等待，统一裸 asyncio.create_task。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    async def _wrap(self, name: str, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("background task %s failed: %s", name, exc)

    def create(self, name: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(self._wrap(name, coro))
        self._tasks[name] = task
        task.add_done_callback(lambda t: self._tasks.pop(name, None))
        return task

    def cancel(self, name: str) -> None:
        task = self._tasks.get(name)
        if task:
            task.cancel()

    def cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()

    async def wait_all(self) -> None:
        for task in list(self._tasks.values()):
            try:
                await task
            except Exception:
                pass


background_tasks = TaskManager()
