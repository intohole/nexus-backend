"""S114 资源终局守底: lifespan 停机段必须收编 NexusScheduler(撤修复必红)。

真雷形态: nexus.scheduler 是模块级单例, S112 修复后全舰队 gated 形态接入,
但 create_standard_lifespan / AppLifecycle 两个 lifespan 工厂的停机段都不调用
scheduler.shutdown() —— SIGTERM 优雅退出时 job 正在跑也被硬杀, 且无收编痕迹。
本守底断言: lifespan 退出后 scheduler.running 必须为 False(撤掉修复→恒 True→红)。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.fastapi_setup import AppLifecycle
from nexus.lifespan import create_standard_lifespan
from nexus.scheduler import NexusScheduler, get_scheduler


@pytest.fixture(autouse=True)
def _scheduler_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEDULER_ENABLED", "1")
    monkeypatch.chdir(tmp_path)


def _fresh_scheduler() -> NexusScheduler:
    NexusScheduler._instance = None
    return get_scheduler()


def test_standard_lifespan_shutdown_collects_scheduler():
    sched = _fresh_scheduler()
    sched.add_cron_job(lambda: None, job_id="s114-guard", hour=3, minute=0)

    async def _start(app):
        try:
            sched.start(mode="gated")
        except TypeError:
            sched.start()

    lifespan = create_standard_lifespan(
        app_name="s114guard", use_uc=False, use_llm=False,
        use_notify=False, register_proxy=False, extra_startup=[_start],
    )
    app = FastAPI(lifespan=lifespan)
    try:
        with TestClient(app):
            assert sched.running, "前置失败: gated 启动未生效(SCHEDULER_ENABLED 未设?)"
        assert not sched.running, (
            "S114 守底红线: lifespan 退出后 scheduler 仍在运行 —— 停机段收编缺失回归"
        )
    finally:
        import asyncio

        asyncio.set_event_loop(asyncio.new_event_loop())
        _fresh_scheduler()


def test_app_lifecycle_aexit_collects_scheduler():
    sched = _fresh_scheduler()
    sched.add_cron_job(lambda: None, job_id="s114-guard-2", hour=4, minute=0)

    import asyncio

    async def _drive():
        try:
            sched.start(mode="gated")
        except TypeError:
            sched.start()
        assert sched.running, "前置失败: gated 启动未生效"
        life = AppLifecycle()
        await life.__aenter__()
        await life.__aexit__(None, None, None)

    try:
        asyncio.run(_drive())
        assert not sched.running, (
            "S114 守底红线: AppLifecycle.__aexit__ 后 scheduler 仍在运行 —— 收编缺失回归"
        )
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())
        _fresh_scheduler()
