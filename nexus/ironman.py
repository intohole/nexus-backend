"""ironman Bootstrap 生命周期：init/reload/ensure 与状态查询。

三职责拆分：配置加载见 ironman_config.py，调用链插桩见 ironman_instrument.py，
本模块只持有 Bootstrap 状态与生命周期编排。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from nexus.ironman_config import (
    ConfigLoader,
    IronmanConfigError,
    default_config_loader,
    is_gateway_mode,
)
from nexus.logging import get_logger

logger = get_logger("nexus.ironman")

__all__ = [
    "ConfigLoader",
    "IronmanConfigError",
    "default_config_loader",
    "is_gateway_mode",
    "init_ironman",
    "reload_ironman",
    "get_bootstrap",
    "is_ironman_available",
    "get_init_app_name",
    "startup",
    "ensure_ironman",
]

_bootstrap: Optional[object] = None
_init_app_name: Optional[str] = None
_lock: asyncio.Lock = asyncio.Lock()

# P0: 配置热更新 - Bootstrap TTL 自动重载（5分钟过期，下次调用时重建）
_BOOTSTRAP_TTL: float = 300.0
_bootstrap_ts: float = 0.0

_ENSURE_RETRY_INITIAL: float = 30.0
_ENSURE_RETRY_MAX: float = 300.0
_ensure_task: Optional[asyncio.Task] = None


async def init_ironman(
    app_name: str,
    config_loader: Optional[ConfigLoader] = None,
    middleware: str = "production",
) -> object:
    global _bootstrap, _init_app_name, _bootstrap_ts
    # P0: Bootstrap TTL 过期检查，过期则重置（下次调用时重建）
    if _bootstrap is not None and _bootstrap_ts > 0:
        age = time.monotonic() - _bootstrap_ts
        if age > _BOOTSTRAP_TTL:
            logger.info("ironman Bootstrap TTL expired (%.0fs > %.0fs), reloading...", age, _BOOTSTRAP_TTL)
            await reload_ironman()

    if _bootstrap is not None:
        return _bootstrap

    async with _lock:
        if _bootstrap is not None:
            return _bootstrap

        from ironman import Bootstrap

        loader = config_loader or default_config_loader
        bootstrap = await Bootstrap.create(
            app_name=app_name,
            config_loader=loader,
            middleware=middleware,
        )
        if not bootstrap.is_available():
            raise IronmanConfigError(
                f"ironman Bootstrap 配置不完整（app={app_name}），api_key/base_url 缺失，"
                "拒绝降级启动。请检查 LION_NAMESPACE 对应 llm/chat 配置。"
            )

        _bootstrap = bootstrap
        _init_app_name = app_name
        _bootstrap_ts = time.monotonic()

        # A4: Bootstrap 创建后立即插桩（包装 ironman 模块级函数）
        from nexus.ironman_instrument import _instrument_ironman
        _instrument_ironman(app_name)

        # 统一标记 ironman 已配置，避免各项目重复调用 mark_ironman_configured()
        from nexus.llm_config import mark_ironman_configured
        mark_ironman_configured()

        logger.info(
            "ironman Bootstrap initialized (app=%s, middleware=%s, via_gateway=%s)",
            app_name,
            middleware,
            is_gateway_mode(),
        )
        return _bootstrap


async def reload_ironman() -> None:
    """P0: 关闭并重置 ironman Bootstrap，下次调用 init_ironman 时重建。

    用于配置热更新：lion 配置变更后，调用此函数重置 Bootstrap，
    下次 LLM 调用时会用新配置重建 Bootstrap。也可通过
    /api/_internal/reload-llm 端点手动触发。

    注意：_init_app_name 不重置——应用名是静态属性，不随配置变更。
    """
    global _bootstrap, _bootstrap_ts
    async with _lock:
        if _bootstrap is not None:
            try:
                close_fn = getattr(_bootstrap, "close", None)
                if close_fn and asyncio.iscoroutinefunction(close_fn):
                    await close_fn()
                elif close_fn:
                    close_fn()
            except Exception as exc:
                logger.warning("ironman Bootstrap close error: %s", exc)
        _bootstrap = None
        _bootstrap_ts = 0.0
        logger.info("ironman Bootstrap reset, will reload on next call")


def get_bootstrap() -> Optional[object]:
    return _bootstrap


def is_ironman_available() -> bool:
    return _bootstrap.is_available() if _bootstrap else False


def get_init_app_name() -> Optional[str]:
    return _init_app_name


async def startup(
    app_name: str,
    config_loader: Optional[ConfigLoader] = None,
    middleware: str = "production",
) -> dict[str, object]:
    """一站式启动 ironman：初始化 + 返回状态字典。

    LLM 配置缺失（IronmanConfigError / LionConfigError）时直接抛出，
    拒绝静默降级掩盖问题，让部署健康检查与回滚机制兜底。
    返回字典字段：
        app: 应用名
        available: ironman 是否可用
        via_gateway: 是否走网关模式
        degraded: 是否处于降级模式（恒为 False，失败即抛错）
    """
    await init_ironman(
        app_name=app_name,
        config_loader=config_loader,
        middleware=middleware,
    )
    return {
        "app": app_name,
        "available": is_ironman_available(),
        "via_gateway": is_gateway_mode(),
        "degraded": False,
    }


async def ensure_ironman(
    app_name: str,
    config_loader: Optional[ConfigLoader] = None,
    middleware: str = "production",
) -> None:
    """初始化 ironman；配置暂时不可用时不抛错，转入后台指数退避重试。

    与 startup() 的区别：startup 失败即抛错（供部署健康检查兜底）；
    ensure 适用于 Lion 短暂不可达不应阻断应用启动的场景，
    恢复后 ironman 自动可用，无需人工干预。幂等，可重复调用。
    """
    global _ensure_task
    if is_ironman_available():
        return
    try:
        await init_ironman(app_name=app_name, config_loader=config_loader, middleware=middleware)
        return
    except Exception as e:
        logger.error("ironman 初始化失败（app=%s），转入后台自动重试: %s", app_name, e)
    if _ensure_task is None or _ensure_task.done():
        _ensure_task = asyncio.get_running_loop().create_task(
            _ensure_retry(app_name, config_loader, middleware)
        )


async def _ensure_retry(
    app_name: str,
    config_loader: Optional[ConfigLoader],
    middleware: str,
) -> None:
    delay: float = _ENSURE_RETRY_INITIAL
    while True:
        await asyncio.sleep(delay)
        try:
            await init_ironman(app_name=app_name, config_loader=config_loader, middleware=middleware)
            if is_ironman_available():
                logger.info("ironman 初始化已自动恢复 (app=%s)", app_name)
                return
        except Exception as e:
            logger.warning("ironman 自动重试失败（app=%s），%.0fs 后继续: %s", app_name, delay, e)
        delay = min(delay * 2, _ENSURE_RETRY_MAX)
