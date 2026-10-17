"""spawn_background 原语：持引用到完成 + 异常留痕 + on_error 钩子 + 取消不误报。"""
import asyncio
import logging

import pytest

from nexus import drain_background_tasks, pending_background_tasks, spawn_background


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
