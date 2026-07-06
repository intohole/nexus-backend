from __future__ import annotations

import asyncio
import os
import time
from typing import Awaitable, Callable, Optional

from nexus.lion import get_chat_config, get_embed_config
from nexus.logging import get_logger

logger = get_logger("nexus.ironman")

ConfigLoader = Callable[[str], Awaitable[dict[str, object]]]

_bootstrap: Optional[object] = None
_init_app_name: Optional[str] = None
_lock: asyncio.Lock = asyncio.Lock()

# P0: 配置热更新 - Bootstrap TTL 自动重载（5分钟过期，下次调用时重建）
_BOOTSTRAP_TTL: float = 300.0
_bootstrap_ts: float = 0.0
# P2: 网关模式标志（由 default_config_loader 写入，is_gateway_mode() 读取）
_via_gateway: bool = False


def _is_placeholder(value: str) -> bool:
    return value.startswith("${") and value.endswith("}")


def _clean(value: str, *fallbacks: str) -> str:
    if value and not _is_placeholder(value):
        return value
    for fb in fallbacks:
        if fb and not _is_placeholder(fb):
            return fb
    return ""


async def default_config_loader(app_name: str) -> dict[str, object]:
    global _via_gateway
    chat_cfg = await get_chat_config(prefer_gateway=True)
    # P2: 记录是否通过网关模式（由 is_gateway_mode() 读取，用于重试降级）
    _via_gateway = bool(chat_cfg.get("via_gateway", False))
    embed_cfg = await get_embed_config(prefer_gateway=True)

    api_key = _clean(
        str(chat_cfg.get("api_key", "")),
        os.getenv("PROMPTFORGE_API_KEY", ""),
        os.getenv("LLM_API_KEY", ""),
    )
    base_url = _clean(
        str(chat_cfg.get("base_url", "")),
        os.getenv("PROMPTFORGE_GATEWAY_URL", ""),
        os.getenv("LLM_BASE_URL", ""),
    )
    model = _clean(
        str(chat_cfg.get("model", "")),
        os.getenv("LLM_MODEL", ""),
        "glm-4-flash",
    )
    provider = str(chat_cfg.get("provider", "") or "openai")

    emb_api_key = _clean(
        str(embed_cfg.get("api_key", "")),
        api_key,
    )
    emb_base_url = _clean(
        str(embed_cfg.get("base_url", "")),
        base_url,
    )
    emb_model = _clean(
        str(embed_cfg.get("model", "")),
        "embedding-3",
    )
    emb_provider = str(embed_cfg.get("provider", "") or provider)
    emb_dim = embed_cfg.get("dimensions") or embed_cfg.get("dimension") or 1024

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "provider": provider,
        "embedding_api_key": emb_api_key,
        "embedding_base_url": emb_base_url,
        "embedding_model": emb_model,
        "embedding_provider": emb_provider,
        "embedding_dimensions": int(emb_dim),
    }


async def init_ironman(
    app_name: str,
    config_loader: Optional[ConfigLoader] = None,
    middleware: str = "production",
) -> object:
    global _bootstrap, _init_app_name, _bootstrap_ts, _via_gateway
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
        _bootstrap = await Bootstrap.create(
            app_name=app_name,
            config_loader=loader,
            middleware=middleware,
        )
        _init_app_name = app_name
        _bootstrap_ts = time.monotonic()

        if _bootstrap.is_available():
            logger.info(
                "ironman Bootstrap initialized (app=%s, middleware=%s, via_gateway=%s)",
                app_name,
                middleware,
                _via_gateway,
            )
        else:
            logger.warning(
                "ironman Bootstrap in degraded mode (app=%s, config missing or incomplete)",
                app_name,
            )
        return _bootstrap


async def reload_ironman() -> None:
    """P0: 关闭并重置 ironman Bootstrap，下次调用 init_ironman 时重建。

    用于配置热更新：lion 配置变更后，调用此函数重置 Bootstrap，
    下次 LLM 调用时会用新配置重建 Bootstrap。也可通过
    /api/_internal/reload-llm 端点手动触发。
    """
    global _bootstrap, _init_app_name, _bootstrap_ts
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
        _init_app_name = None
        _bootstrap_ts = 0.0
        logger.info("ironman Bootstrap reset, will reload on next call")


def get_bootstrap() -> Optional[object]:
    return _bootstrap


def is_ironman_available() -> bool:
    return _bootstrap.is_available() if _bootstrap else False


def is_gateway_mode() -> bool:
    """P2: 当前 ironman 是否通过 prompt-manager 网关模式调用。

    由 default_config_loader 写入。用于 LLMService 判断是否降低重试次数
    （网关已有 failover，无需业务层多重试）。
    """
    return _via_gateway


def get_init_app_name() -> Optional[str]:
    return _init_app_name
