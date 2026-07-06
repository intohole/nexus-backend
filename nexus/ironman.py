from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional

from nexus.lion import get_chat_config, get_embed_config
from nexus.logging import get_logger

logger = get_logger("nexus.ironman")

ConfigLoader = Callable[[str], Awaitable[dict[str, object]]]

_bootstrap: Optional[object] = None
_init_app_name: Optional[str] = None
_lock: asyncio.Lock = asyncio.Lock()


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
    chat_cfg = await get_chat_config(prefer_gateway=True)
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
    global _bootstrap, _init_app_name
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

        if _bootstrap.is_available():
            logger.info(
                "ironman Bootstrap initialized (app=%s, middleware=%s)",
                app_name,
                middleware,
            )
        else:
            logger.warning(
                "ironman Bootstrap in degraded mode (app=%s, config missing or incomplete)",
                app_name,
            )
        return _bootstrap


def get_bootstrap() -> Optional[object]:
    return _bootstrap


def is_ironman_available() -> bool:
    return _bootstrap.is_available() if _bootstrap else False


def get_init_app_name() -> Optional[str]:
    return _init_app_name
