"""spawn_background 原语：持引用到完成 + 异常留痕 + on_error 钩子 + 取消不误报。"""
import asyncio
import logging

import pytest

from nexus import drain_background_tasks, pending_background_tasks, spawn_background, spawn_periodic


@pytest.mark.asyncio
async def test_task_held_until_done_and_result_available():
    async def work():
        await asyncio.sleep(0.01)
        return 42
    t = spawn_background(work(), name="w")
    assert pending_background_tasks() >= 1
    assert await t == 42
    await asyncio.sleep(0)
    assert pending_background_tasks() == 0


@pytest.mark.asyncio
async def test_exception_logged_not_swallowed(caplog):
    async def boom():
        raise ValueError("x")
    with caplog.at_level(logging.WARNING, logger="nexus.background"):
        t = spawn_background(boom(), name="boom")
        with pytest.raises(ValueError):
            await t
        await asyncio.sleep(0)
    assert any("boom" in r.getMessage() and "ValueError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_on_error_hook_receives_exception():
    seen = []
    async def boom():
        raise RuntimeError("y")
    t = spawn_background(boom(), on_error=seen.append)
    with pytest.raises(RuntimeError):
        await t
    await asyncio.sleep(0)
    assert len(seen) == 1 and isinstance(seen[0], RuntimeError)


@pytest.mark.asyncio
async def test_cancelled_task_is_quiet(caplog):
    async def forever():
        await asyncio.sleep(10)
    with caplog.at_level(logging.WARNING, logger="nexus.background"):
        t = spawn_background(forever())
        await asyncio.sleep(0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await asyncio.sleep(0)
    assert not [r for r in caplog.records if r.name == "nexus.background"]


@pytest.mark.asyncio
async def test_drain_waits_for_pending():
    async def slow():
        await asyncio.sleep(0.05)
    spawn_background(slow())
    assert await drain_background_tasks(timeout=1.0) == 0


@pytest.mark.asyncio
async def test_running_tasks_snapshot_and_drain_clears_registry():
    async def quick():
        await asyncio.sleep(0.02)
    from nexus.utils.background import running_background_tasks
    spawn_background(quick())
    handles = running_background_tasks()
    assert len(handles) >= 1, "running_background_tasks 必须返回在飞任务句柄（关停 cancel 收尾用）"
    assert await drain_background_tasks(timeout=2.0) == 0
    assert running_background_tasks() == []


@pytest.mark.asyncio
async def test_periodic_immediate_first_tick_and_registry():
    from nexus import periodic_background_tasks
    ticks = []
    async def tick():
        ticks.append(1)
    t = spawn_periodic(tick, 0.05, name="p-immediate")
    await asyncio.sleep(0.01)
    assert ticks, "immediate=True 必须启动即首拍"
    assert periodic_background_tasks(), "运行中的周期任务必须在册（防 GC+观测面）"
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await asyncio.sleep(0)
    assert periodic_background_tasks() == [], "取消后必须出册"


@pytest.mark.asyncio
async def test_periodic_deferred_first_tick():
    from nexus import spawn_periodic
    ticks = []
    async def tick():
        ticks.append(1)
    t = spawn_periodic(tick, 0.2, immediate=False)
    await asyncio.sleep(0.05)
    assert not ticks, "immediate=False 必须先睡一个 interval 再首拍"
    await asyncio.sleep(0.25)
    assert ticks, "interval 到点后必须首拍"
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t


@pytest.mark.asyncio
async def test_periodic_tick_failure_does_not_kill_loop(caplog):
    from nexus import spawn_periodic
    calls = {"n": 0}
    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first tick boom")
    with caplog.at_level(logging.ERROR, logger="p-flaky"):
        lg = logging.getLogger("p-flaky")
        t = spawn_periodic(flaky, 0.02, name="flaky", logger=lg)
        await asyncio.sleep(0.1)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    assert calls["n"] >= 2, "单拍异常必须留痕后继续循环（单拍失败不杀任务）"
    assert any("tick failed" in r.getMessage() for r in caplog.records), "单拍异常必须 logger.exception 留痕"


@pytest.mark.asyncio
async def test_periodic_initial_delay():
    from nexus import spawn_periodic
    ticks = []
    async def tick():
        ticks.append(1)
    t = spawn_periodic(tick, 10.0, initial_delay=0.05)
    await asyncio.sleep(0.01)
    assert not ticks, "initial_delay 期内不得首拍"
    await asyncio.sleep(0.1)
    assert ticks, "initial_delay 到点后必须首拍"
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t


@pytest.mark.asyncio
async def test_periodic_not_in_drain_registry():
    from nexus.utils.background import running_background_tasks, _PERIODIC_TASKS, _TASKS
    async def tick():
        await asyncio.sleep(10)
    t = spawn_periodic(tick, 10.0, name="p-no-drain")
    await asyncio.sleep(0)
    assert _PERIODIC_TASKS and t not in _TASKS, "周期任务只进 _PERIODIC_TASKS"
    assert running_background_tasks() == [], "周期任务不得混入 fire-and-forget registry（drain 会卡死关停钩子）"
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
