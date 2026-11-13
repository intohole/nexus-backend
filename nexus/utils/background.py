"""fire-and-forget 后台任务原语。

`asyncio.create_task()` 返回的 Task 若无人持有引用，事件循环只持弱引用，任务可能在执行中被 GC
静默回收（教训#9）；全舰队 22 仓 ~90 处裸 create_task，其中 10 仓各自手写「模块级 task set +
add_done_callback(discard)」防 GC——同一形态重复十次即收归。第二宗罪是异常被吞：Task 内未捕获
的异常只在 GC 时打一行 "Task exception was never retrieved"，功能静默没上线。本原语两件事都管。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional, Set

_logger = logging.getLogger("nexus.background")
_TASKS: Set["asyncio.Task[object]"] = set()


def spawn_background(
    coro: Awaitable[object],
    *,
    name: Optional[str] = None,
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> "asyncio.Task[object]":
    """派发协程为后台任务并持有引用直到完成；异常默认 WARNING 留痕，可传 on_error 自定义。
    必须在运行中的事件循环内调用（请求处理/生命周期钩子内）。"""
    loop = asyncio.get_running_loop()
    task = loop.create_task(coro, name=name)
    _TASKS.add(task)

    def _done(t: "asyncio.Task[object]") -> None:
        _TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                _logger.exception("spawn_background on_error callback raised")
            return
        _logger.warning("background task %s failed: %r", t.get_name(), exc)

    task.add_done_callback(_done)
    return task


def pending_background_tasks() -> int:
    """当前仍在飞的后台任务数（健康检查/测试断言用）。"""
    return len(_TASKS)


def running_background_tasks() -> list:
    """仍在飞的任务句柄（关停时主动 cancel 收尾用；只读快照）。"""
    return [t for t in _TASKS if not t.done()]


async def drain_background_tasks(timeout: float = 5.0) -> int:
    """等待在飞任务收尾（关闭钩子/测试用），返回超时后仍未完成的数量。"""
    pending = [t for t in _TASKS if not t.done()]
    if not pending:
        return 0
    _, still = await asyncio.wait(pending, timeout=timeout)
    return len(still)


_PERIODIC_TASKS: Set["asyncio.Task[object]"] = set()


def spawn_periodic(
    tick: Callable[[], Awaitable[object]],
    interval_seconds: float,
    *,
    name: Optional[str] = None,
    immediate: bool = True,
    initial_delay: float = 0.0,
    logger: Optional[logging.Logger] = None,
) -> "asyncio.Task[object]":
    """派发固定间隔周期任务（进程内轮询骨架单点，r211）。

    与 spawn_background 同族互补：那边管一次性 fire-and-forget，这边管
    while 轮询循环。单拍异常 logger.exception 留痕后继续循环（单拍失败不杀
    任务）；CancelledError 直接穿透——stop=持有返回句柄 cancel。业务轮询
    不进 scheduler gate（治理面任务与业务核心任务的分界，r205 判例同源）。
    immediate=True（默认）先拍后睡=启动即首拍；immediate=False 先睡后拍；
    initial_delay 为循环外首睡（延迟首拍等依赖就绪）。

    registry 语义：登记 _PERIODIC_TASKS 防 GC+供观测，但**不进
    drain_background_tasks**——周期任务永命，drain 会卡死关停钩子；
    关停收尾用返回句柄逐个 cancel。必须在运行中的事件循环内调用。
    """
    lg = logger or _logger
    loop = asyncio.get_running_loop()

    async def _loop() -> None:
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        while True:
            if not immediate:
                await asyncio.sleep(interval_seconds)
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                lg.exception("periodic task %s tick failed", name or "unnamed")
            if immediate:
                await asyncio.sleep(interval_seconds)

    task = loop.create_task(_loop(), name=name)
    _PERIODIC_TASKS.add(task)

    def _done(t: "asyncio.Task[object]") -> None:
        _PERIODIC_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            lg.error("periodic task %s exited unexpectedly: %r", t.get_name(), exc)

    task.add_done_callback(_done)
    return task


def periodic_background_tasks() -> list:
    """在跑的周期任务句柄快照（健康检查/关停收尾/测试断言用）。"""
    return [t for t in _PERIODIC_TASKS if not t.done()]
