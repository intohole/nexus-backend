"""标准 lifespan 工厂：启动/关闭钩子的统一装配。"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from nexus.database import init_db, close_db
from nexus.uc_sdk_helper import init_uc_sdk_from_lion, close_uc_sdk
from nexus.ironman import startup as startup_ironman
from nexus.notify import async_init_notify_client, register_notify_proxy

logger = logging.getLogger("nexus.lifespan")

StartupHook = Callable[[FastAPI], Awaitable[None]]
ShutdownHook = Callable[[FastAPI], Awaitable[None]]
DbInit = Callable[[], Awaitable[None]]
DbClose = Callable[[], Awaitable[None]]


def create_standard_lifespan(
    *,
    app_name: str,
    init_db_fn: Optional[DbInit] = None,
    close_db_fn: Optional[DbClose] = None,
    use_uc: bool = True,
    use_llm: bool = True,
    use_notify: bool = True,
    register_proxy: bool = True,
    extra_startup: Optional[list[StartupHook]] = None,
    extra_shutdown: Optional[list[ShutdownHook]] = None,
):
    """标准启动引导模板：init_db → UC → ironman → notify → proxy。

    业务仅需传入 app_name 与项目特定钩子，消除各项目 lifespan 样板。
    init_db_fn/close_db_fn 默认为 nexus 全局 DB；传入项目自建管理器时覆盖。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_init = init_db_fn or init_db
        db_close = close_db_fn or close_db
        try:
            await db_init()
            if use_uc:
                await init_uc_sdk_from_lion()
            if use_llm:
                await startup_ironman(app_name)
            if use_notify:
                await async_init_notify_client()
            if register_proxy:
                register_notify_proxy(app)
            for hook in extra_startup or []:
                await hook(app)
            yield
        finally:
            for hook in reversed(extra_shutdown or []):
                try:
                    await hook(app)
                except Exception as exc:
                    logger.warning("shutdown hook %s failed: %s", getattr(hook, "__name__", hook), exc)
            if use_uc:
                await close_uc_sdk()
            await db_close()

    return lifespan
